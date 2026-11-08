"""
robot_brain.py  -  Isaac's conversational robot.

Hardware: Raspberry Pi 5, Adafruit Motor HAT, HC-SR04 ultrasonic,
          USB webcam, USB microphone, speaker/amp, MG996R neck servo.
Pi 5 compatible: lgpio, not RPi.GPIO.

  - Chat and speech-to-text are Grok (xAI), via XAI_API_KEY and
    https://api.x.ai/v1. Walk up and talk. The mic stays open, replies
    are spoken, and the chat keeps going across turns. No keyboard.
  - The eyes and the neck look at a face in the camera. With no face,
    they idle. That look does not drive the wheels.
  - The drive wheels stay off while MOTORS_ENABLED is False. The neck
    can still look. Motors are stopped at startup so nothing lurches.
    A phone panel on port 8080 can drive them only while CONTROL is held.
  - Person detection uses YOLOv8n only if it is already installed.
    Otherwise HOG + Haar. YOLO is not required.

Install once on the Pi: bash install_on_pi.sh
Run:
  export XAI_API_KEY="xai-..."
  python3 robot_brain.py
"""

import io
import json
import os
import time
import wave
import threading
from collections import deque

import cv2
import lgpio
import speech_recognition as sr
import pyttsx3
from openai import OpenAI
from adafruit_motorkit import MotorKit

# ---------------- Grok (xAI) settings ----------------
XAI_API_KEY = os.environ.get("XAI_API_KEY", "xai-YOUR_API_KEY_HERE")
BASE_URL    = "https://api.x.ai/v1"
STT_URL     = "https://api.x.ai/v1/stt"
MODEL       = "grok-3-mini"                 # chat model; check console.x.ai
STT_MODEL   = "grok-voice-transcribe-2.0"   # speech-to-text

# Flip this to True when the robot should drive again. Exporting
# MOTORS_ENABLED=1 (or 0) overrides the line below.
MOTORS_ENABLED = False
_motors_env = os.environ.get("MOTORS_ENABLED", "").strip().lower()
if _motors_env in ("1", "true", "yes", "on"):
    MOTORS_ENABLED = True
elif _motors_env in ("0", "false", "no", "off"):
    MOTORS_ENABLED = False

# ---------------- Behavior tuning ----------------
HUMAN_DISTANCE_CM         = 61
OBSTACLE_STOP_CM          = 25
SPEED_NORMAL              = 0.6
SPEED_SLOW                = 0.35
SPEED_TURN                = 0.35
MANUAL_SPEED              = 0.45   # phone hold-to-drive, not full blast
MANUAL_HOLD_SECONDS       = 0.45   # no fresh hold -> stop (release or drop)

LOST_HUMAN_TIMEOUT        = 4.0
CONVERSATION_IDLE_TIMEOUT = 30.0
SEARCH_SPEECH_COOLDOWN    = 8.0    # seconds between "I can't find you"

# Vision
FRAME_W, FRAME_H          = 640, 480
YOLO_CONF_NORMAL          = 0.45   # confidence needed when calm
YOLO_CONF_ALERT           = 0.30   # looser when motion was just seen
DETECT_EVERY_N_NORMAL     = 2      # run detector every Nth frame when calm
DETECT_EVERY_N_ALERT      = 1      # every frame when alert
HITS_NEEDED               = 2      # hits needed in the window...
HIT_WINDOW                = 5      # ...of this many detector runs

# Motion
MOTION_MIN_AREA           = 1500   # pixels (on 320x240) to count as motion
MOTION_ALERT_SECONDS      = 3.0    # how long motion keeps us paying attention
MOTION_WIDTH              = 320
MOTION_HEIGHT             = 240
GAZE_HOLD_SECONDS         = 0.7    # keep the last look target across brief gaps
# Horizontal field of view, used only to cancel our own neck pan in the
# frame-difference image. A rough Pi-camera value is enough.
CAMERA_HFOV_DEG           = 62.0

# Steering: camera servo angle where the body should turn to follow
SERVO_CENTER              = 90.0
SERVO_MIN, SERVO_MAX      = 30.0, 150.0
STEER_DEADBAND_DEG        = 15.0   # within this, sonar points at the person
SERVO_DEG_PER_SEC         = 40.0   # how fast the neck may chase a target

SERVO_PIN                 = 18
TRIG_PIN                  = 23
ECHO_PIN                  = 24

SYSTEM_PROMPT = (
    "You are a small, friendly wheeled robot. "
    "You speak in short sentences - 1 to 3 sentences per reply. "
    "You are curious, warm, and slightly playful. "
    "You refer to people as human. "
    "Your replies are spoken aloud, so never use emoji, lists, or markdown. "
    "Never mention that you are an AI language model."
)

state = {
    "human_visible":        False,
    "human_distance_cm":    999,
    "obstacle_distance_cm": 999,
    "last_human_seen":      0.0,
    "servo_angle":          SERVO_CENTER,
    "alert_until":          0.0,     # motion keeps attention until this time
    "motors_moving":        False,
    "greeted":              False,
    "conversation":         [],
    "speaking":             False,
    "listening":            False,
    "last_heard":           0.0,
    "ignore_listen_until":  0.0,
    "drive_mode":           "automate",  # automate | control
    "manual_dir":           "stop",
    "manual_stamp":         0.0,
    "manual_left":          0.0,
    "manual_right":         0.0,
    "heard_lines":          [],
    "gaze_x":               None,    # 0..1 in the camera frame, or None
    "gaze_y":               None,
    "gaze_until":           0.0,
    "running":              True,
    "thinking":             False,   # waiting on Grok (face can glance up)
    "mood":                 "idle",  # idle / approach / maintain / searching
    "online":               True,    # internet reachable
}

gpio_handle  = None
motor_left   = None
motor_right  = None
tts_engine   = None
ai_client    = None
tts_lock     = threading.Lock()
motor_lock   = threading.Lock()
recognizer   = sr.Recognizer()
_last_stt_log = 0.0


def motors_enabled_setting(code_default, env_value):
    """True only when driving should run. Env overrides the code flag."""
    raw = ("" if env_value is None else str(env_value)).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(code_default)


# ======================= Hardware helpers =======================
_last_servo_move = 0.0

def set_servo_angle(angle):
    global _last_servo_move
    angle = max(SERVO_MIN, min(SERVO_MAX, angle))
    if abs(angle - state["servo_angle"]) > 0.5:
        _last_servo_move = time.time()
    state["servo_angle"] = angle
    pulse_us = int(500 + (angle / 180.0) * 2000)
    lgpio.tx_servo(gpio_handle, SERVO_PIN, pulse_us)


def servo_step_toward(current, frame_x, dt):
    """Ease the neck toward frame_x (0..1, 0.5 is center).

    Image-right lowers the angle, which is the direction the camera
    already turns to face something on the right of the picture.
    """
    error = float(frame_x) - 0.5
    if abs(error) < 0.035:
        return current
    desired = error * 50.0
    cap = SERVO_DEG_PER_SEC * max(0.02, min(float(dt), 0.2))
    step = max(-cap, min(cap, desired))
    return max(SERVO_MIN, min(SERVO_MAX, current - step))


def command_servo(angle):
    if abs(angle - state["servo_angle"]) >= 0.4:
        set_servo_angle(angle)


def measure_distance_cm():
    lgpio.gpio_write(gpio_handle, TRIG_PIN, 0)
    time.sleep(0.002)
    lgpio.gpio_write(gpio_handle, TRIG_PIN, 1)
    time.sleep(0.00001)
    lgpio.gpio_write(gpio_handle, TRIG_PIN, 0)

    deadline = time.time() + 0.03
    while lgpio.gpio_read(gpio_handle, ECHO_PIN) == 0:
        if time.time() > deadline:
            return 999
    pulse_start = time.time()

    deadline = time.time() + 0.03
    while lgpio.gpio_read(gpio_handle, ECHO_PIN) == 1:
        if time.time() > deadline:
            return 999
    pulse_end = time.time()

    return ((pulse_end - pulse_start) * 34300) / 2


def _write_throttles(left, right):
    """Write wheel throttles. Used by hand-driving even when automate is off."""
    left = float(left)
    right = float(right)
    with motor_lock:
        state["manual_left"] = left
        state["manual_right"] = right
        state["motors_moving"] = (left != 0 or right != 0)
        if motor_left is None or motor_right is None:
            return
        motor_left.throttle = left
        motor_right.throttle = right


def _set_motors(left, right):
    # Autonomous path. Zero while MOTORS_ENABLED is false, and never
    # overwrite a person who is holding a direction in CONTROL.
    if state.get("drive_mode") == "control":
        return
    if not MOTORS_ENABLED:
        left, right = 0, 0
    _write_throttles(left, right)


def manual_stop():
    state["manual_dir"] = "stop"
    state["manual_stamp"] = time.time()
    _write_throttles(0, 0)


def normalize_direction(value):
    value = ("" if value is None else str(value)).strip().lower()
    if value == "backward":
        return "back"
    if value in ("forward", "back", "left", "right", "stop"):
        return value
    return None


def set_drive_mode(mode):
    """Switch AUTOMATE / CONTROL. Either change stops the wheels now."""
    mode = "control" if str(mode).strip().lower() == "control" else "automate"
    manual_stop()
    state["drive_mode"] = mode
    manual_stop()
    print("[PANEL] mode {0}".format(mode))
    return mode


def manual_drive(direction):
    """Hold-to-move. Ignored unless CONTROL is on. stop always zeros."""
    direction = normalize_direction(direction)
    if direction is None:
        return None
    if state.get("drive_mode") != "control":
        manual_stop()
        return None
    if direction == "stop":
        manual_stop()
        return "stop"
    if direction == "forward":
        left = right = MANUAL_SPEED
    elif direction == "back":
        left = right = -MANUAL_SPEED
    elif direction == "left":
        left, right = -SPEED_TURN, SPEED_TURN
    else:
        left, right = SPEED_TURN, -SPEED_TURN
    state["manual_dir"] = direction
    state["manual_stamp"] = time.time()
    _write_throttles(left, right)
    return direction


def expire_manual_drive(now=None):
    """Stop if CONTROL lost its hold (finger up or the phone went away)."""
    if state.get("drive_mode") != "control":
        return False
    if state.get("manual_dir") in (None, "", "stop"):
        return False
    now = time.time() if now is None else now
    if now - state.get("manual_stamp", 0.0) <= MANUAL_HOLD_SECONDS:
        return False
    manual_stop()
    return True


def drive_watchdog():
    while state["running"]:
        try:
            expire_manual_drive()
        except Exception as e:
            print("[PANEL] drive watchdog ({0})".format(e))
            manual_stop()
        time.sleep(0.05)


def clean_say_text(text):
    if text is None:
        return None
    text = " ".join(str(text).replace("\x00", " ").split())
    if not text:
        return None
    return text[:300]


def say_typed(text, speaker=None):
    """Speak typed words on the USB speakers. Does not ask Grok."""
    cleaned = clean_say_text(text)
    if not cleaned:
        return None
    # Mark speaking before the audio starts so a listen already in progress
    # is thrown away and the robot does not answer its own voice.
    state["speaking"] = True
    state["ignore_listen_until"] = time.time() + 1.5
    if speaker is not None:
        speaker(cleaned)
        return cleaned
    threading.Thread(target=speak, args=(cleaned,), daemon=True).start()
    return cleaned


def remember_heard(text):
    lines = state.setdefault("heard_lines", [])
    lines.append(str(text))
    del lines[:-12]


def panel_status():
    """Phone status. Never includes env files or API keys."""
    return {
        "mode": state.get("drive_mode", "automate"),
        "speaking": bool(state.get("speaking")),
        "listening": bool(state.get("listening")) and state.get("drive_mode") != "control",
        "heard": list(state.get("heard_lines") or []),
        "motors_enabled": bool(MOTORS_ENABLED),
    }

def motors_stop():                      _set_motors(0, 0)
def motors_forward(speed=SPEED_NORMAL): _set_motors(speed, speed)
def motors_backward(speed=SPEED_SLOW):  _set_motors(-speed, -speed)
def motors_turn_left(speed=SPEED_TURN): _set_motors(-speed, speed)
def motors_turn_right(speed=SPEED_TURN):_set_motors(speed, -speed)


# ======================= Speech =======================
def _new_tts():
    engine = pyttsx3.init()   # espeak-ng on the Pi
    engine.setProperty("rate",   165)
    engine.setProperty("volume", 0.9)
    return engine

def speak(text):
    """Say one line. A fresh espeak engine is made afterward so the next
    line still speaks; pyttsx3 often goes silent after a single runAndWait."""
    global tts_engine
    with tts_lock:
        state["speaking"] = True
        try:
            print(f"[ROBOT] {text}")
            if tts_engine is None:
                tts_engine = _new_tts()
            tts_engine.say(text)
            tts_engine.runAndWait()
        except Exception as e:
            print(f"[TTS] {e}")
            tts_engine = None
        finally:
            if tts_engine is not None:
                try:
                    tts_engine.stop()
                except Exception:
                    pass
                try:
                    tts_engine = _new_tts()
                except Exception as e:
                    print(f"[TTS] could not restart voice ({e})")
                    tts_engine = None
            state["speaking"] = False
            state["ignore_listen_until"] = time.time() + 0.7

def speak_async(text):
    # Don't talk over a sentence already in progress, and don't talk into
    # the open microphone or the robot will answer itself.
    if state["speaking"] or state.get("listening"):
        return
    state["speaking"] = True   # set now so nothing else talks over it
    threading.Thread(target=speak, args=(text,), daemon=True).start()


def _is_camera_input(name):
    """True for a webcam's built-in mic, which is a poor match for a jack."""
    return any(word in name for word in ("camera", "webcam", "uvc"))


def _input_score(device):
    """Rank capture devices. Higher is better.

    A USB audio adapter (the 1/8 inch jack: "USB Audio Device") beats a
    webcam mic. Both names contain "usb", and the camera name also
    contains "audio", so those words alone must not decide it. HDMI and
    monitor sources stay below zero and are not chosen.
    """
    name = (device.get("name") or "").lower()
    if any(word in name for word in ("hdmi", "vc4", "monitor")):
        return -50
    if _is_camera_input(name):
        return 10
    score = 0
    if "usb" in name:
        score += 40
    if any(word in name for word in ("audio", "mic", "microphone", "headset", "earphone")):
        score += 40
    if "pulse" in name or name.strip() == "default":
        score += 5
    return score


def select_input_device(devices, prefer=None):
    """Pick a microphone index, or None to use the system default.

    prefer is a PyAudio index ("1") or a piece of the device name
    ("USB Audio", "hw:3,0"). When prefer is omitted, MIC_DEVICE is used.
    HDMI and monitor sources are never chosen by the automatic ranking.
    """
    inputs = [d for d in devices if int(d.get("max_input_channels") or 0) > 0]
    if not inputs:
        return None
    if prefer is None:
        prefer = os.environ.get("MIC_DEVICE", "")
    prefer = str(prefer).strip()
    if prefer:
        if prefer.isdigit():
            want = int(prefer)
            for device in inputs:
                if int(device.get("index", -1)) == want:
                    return want
        needle = prefer.lower()
        matches = [d for d in inputs if needle in (d.get("name") or "").lower()]
        if matches:
            best = max(matches, key=lambda d: (_input_score(d), -int(d.get("index", 0))))
            return int(best["index"])
        print("[MIC] MIC_DEVICE={0!r} matched no input; choosing automatically".format(prefer))
    best = max(inputs, key=lambda d: (_input_score(d), -int(d.get("index", 0))))
    if _input_score(best) <= 0:
        return None
    return int(best["index"])


def describe_input_devices():
    try:
        import pyaudio
    except Exception as e:
        print(f"[MIC] PyAudio is not available ({e})")
        return []
    found = []
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            found.append({
                "index": int(info.get("index", i)),
                "name": str(info.get("name", "")),
                "max_input_channels": int(info.get("maxInputChannels") or 0),
            })
    finally:
        pa.terminate()
    return found


def open_microphone():
    try:
        devices = describe_input_devices()
    except Exception as e:
        print(f"[MIC] Could not list microphones ({e})")
        devices = []
    prefer = os.environ.get("MIC_DEVICE", "").strip()
    if prefer:
        print("[MIC] MIC_DEVICE={0}".format(prefer))
    for d in devices:
        print("[MIC] device {0}: {1} (inputs={2} score={3})".format(
            d["index"], d["name"], d["max_input_channels"], _input_score(d)))
    index = select_input_device(devices, prefer=prefer)
    chosen = None
    for d in devices:
        if index is not None and int(d["index"]) == index:
            chosen = d["name"]
    print("[MIC] Selected input: {}".format(chosen if chosen else "system default"))
    try:
        mic = sr.Microphone(device_index=index) if index is not None else sr.Microphone()
        with mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.4)
        # Freeze the threshold. The dynamic one drifts down into hiss and
        # then listen() records several seconds of noise before giving up.
        recognizer.energy_threshold = max(float(recognizer.energy_threshold), 280.0)
        recognizer.dynamic_energy_threshold = False
        return mic
    except Exception as e:
        print(f"[MIC] Could not open microphone ({e})")
        return None


def pcm_to_wav(pcm, sample_rate, sample_width):
    """Wrap mono PCM in a WAV container. No resample, so audioop is not required."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(int(sample_width))
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return buf.getvalue()


def wav_bytes_from_audio(audio):
    try:
        data = audio.get_wav_data()
        if data:
            return data
    except Exception as e:
        print(f"[MIC] WAV encode failed ({e})")
    raw = audio.get_raw_data()
    return pcm_to_wav(raw, audio.sample_rate, audio.sample_width)


# Below this, a clip is hiss or a click, not someone talking. Sending it
# to speech-to-text makes up phrases and the reply comes back late.
MIN_SPEECH_SECONDS = 0.30
MIN_SPEECH_RMS = 250.0
MIN_SPEECH_PEAK = 700


def pcm_stats(pcm, sample_width):
    """Return (rms, peak, sample_count) for little-endian PCM."""
    if sample_width != 2 or not pcm or len(pcm) < 2:
        return 0.0, 0, 0
    count = len(pcm) // 2
    total = 0
    peak = 0
    for i in range(count):
        value = int.from_bytes(pcm[i * 2:i * 2 + 2], "little", signed=True)
        magnitude = value if value >= 0 else -value
        if magnitude > peak:
            peak = magnitude
        total += value * value
    return (total / count) ** 0.5, peak, count


def audio_has_speech(pcm, sample_rate, sample_width,
                     min_seconds=MIN_SPEECH_SECONDS,
                     min_rms=MIN_SPEECH_RMS,
                     min_peak=MIN_SPEECH_PEAK):
    """True only when the clip is long enough and loud enough to be speech."""
    rms, peak, count = pcm_stats(pcm, sample_width)
    if count <= 0 or not sample_rate or sample_rate <= 0:
        return False
    if (count / float(sample_rate)) < min_seconds:
        return False
    if rms < min_rms or peak < min_peak:
        return False
    return True


def clean_transcript(text):
    if not text:
        return None
    text = " ".join(str(text).replace("\x00", " ").split())
    letters = [c for c in text if c.isalnum()]
    if len(letters) < 2:
        return None
    return text


def build_stt_body(wav_bytes, boundary):
    """Multipart body for POST /v1/stt. The file field must be last."""
    chunks = []
    for name, value in (("model", STT_MODEL), ("language", "en")):
        chunks.append(
            "--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".format(
                b=boundary, n=name, v=value).encode("utf-8"))
    chunks.append(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n".format(b=boundary).encode("utf-8"))
    chunks.append(wav_bytes)
    chunks.append("\r\n--{b}--\r\n".format(b=boundary).encode("utf-8"))
    return b"".join(chunks)


def transcript_from_stt_payload(payload):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    return clean_transcript(payload.get("text") or "")


def _log_stt(msg):
    global _last_stt_log
    now = time.time()
    if now - _last_stt_log > 15:
        print(msg)
        _last_stt_log = now


def transcribe_with_grok(wav_bytes):
    """Send one utterance to Grok speech-to-text. Returns text or None."""
    import urllib.error
    import urllib.request
    boundary = "IsaacRobot" + os.urandom(8).hex()
    body = build_stt_body(wav_bytes, boundary)
    req = urllib.request.Request(
        STT_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + XAI_API_KEY,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "isaac-robot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = b""
        try:
            detail = e.read(240)
        except Exception:
            pass
        _log_stt("[STT] HTTP {code} {detail!r}".format(code=e.code, detail=detail[:180]))
        return None
    except Exception as e:
        _log_stt("[STT] {0}".format(e))
        return None
    try:
        return transcript_from_stt_payload(raw)
    except Exception as e:
        _log_stt("[STT] Bad response ({0})".format(e))
        return None


def listen_for_speech(mic, timeout=3, phrase_time_limit=8):
    """Block until a phrase ends or timeout. None if nobody spoke.

    Quiet or very short clips are dropped here, so they are not sent to
    Grok and cannot come back as invented sentences.
    """
    state["listening"] = True
    audio = None
    try:
        with mic as source:
            audio = recognizer.listen(source, timeout=timeout,
                                      phrase_time_limit=phrase_time_limit)
    except sr.WaitTimeoutError:
        return None
    finally:
        state["listening"] = False
    if audio is None:
        return None
    try:
        raw = audio.get_raw_data()
        if not audio_has_speech(raw, audio.sample_rate, audio.sample_width):
            rms, _peak, count = pcm_stats(raw, audio.sample_width)
            rate = audio.sample_rate or 1
            _log_stt("[MIC] Ignored non-speech audio ({0:.0f} ms, rms {1:.0f})".format(
                1000.0 * count / float(rate), rms))
            return None
        wav = wav_bytes_from_audio(audio)
    except Exception as e:
        print(f"[MIC] Could not read audio ({e})")
        return None
    if not wav:
        return None
    # Drop the clip if we were talking, or if the phone took the mic for
    # CONTROL, so the robot does not answer its own speaker.
    if state.get("speaking") or state.get("drive_mode") == "control":
        return None
    if time.time() < state.get("ignore_listen_until", 0.0):
        return None
    return transcribe_with_grok(wav)


# ======================= Grok brain =======================
def ai_respond(user_text):
    state["conversation"].append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + state["conversation"]
    state["thinking"] = True
    try:
        response = ai_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=150,
            temperature=0.8,
        )
        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            reply = "Hmm, let me think about that, human."
    except Exception as e:
        reply = "Sorry, my thinking circuit is fuzzy right now."
        print(f"[AI ERROR] {e}")
    state["thinking"] = False
    state["conversation"].append({"role": "assistant", "content": reply})
    if len(state["conversation"]) > 40:
        state["conversation"] = state["conversation"][-40:]
    return reply


def _chat_active(now):
    """True while a spoken exchange is still going, even if vision lost them."""
    if state["speaking"] or state["thinking"]:
        return True
    if state["conversation"] and (now - state.get("last_heard", 0.0)) < CONVERSATION_IDLE_TIMEOUT:
        return True
    return False


# ======================= Person detectors =======================
_HAAR_FILENAME = "haarcascade_frontalface_default.xml"
# Debian/Raspberry Pi OS python3-opencv does not ship cv2.data. opencv-data
# installs the cascades under the opencv4 path; older images used opencv/.
_HAAR_SYSTEM_DIRS = (
    "/usr/share/opencv4/haarcascades",
    "/usr/share/opencv/haarcascades",
    "/usr/local/share/opencv4/haarcascades",
    "/usr/local/share/opencv/haarcascades",
)


def haar_cascade_candidates():
    """Places a frontal-face Haar XML might live, most specific first."""
    paths = [os.path.join(os.path.dirname(os.path.abspath(__file__)), _HAAR_FILENAME)]
    data = getattr(cv2, "data", None)
    haar_dir = getattr(data, "haarcascades", None) if data is not None else None
    if haar_dir:
        paths.append(os.path.join(str(haar_dir), _HAAR_FILENAME))
    for directory in _HAAR_SYSTEM_DIRS:
        paths.append(os.path.join(directory, _HAAR_FILENAME))
    return paths


def load_frontal_face_cascade():
    """Load the frontal-face cascade, or None when no file works.

    A missing XML must not raise. Callers still have HOG body detection.
    """
    seen = set()
    for path in haar_cascade_candidates():
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            continue
        try:
            cascade = cv2.CascadeClassifier(path)
        except Exception as e:
            print(f"[VISION] Haar cascade {path} failed ({e})")
            continue
        if cascade is None or cascade.empty():
            print(f"[VISION] Haar cascade {path} did not load")
            continue
        print(f"[VISION] Haar cascade: {path}")
        return cascade
    print("[VISION] No Haar cascade found; HOG body detection only")
    return None


class PersonDetector:
    """YOLOv8n if available, else HOG body + Haar face. Returns list of
    (x, y, w, h, conf) boxes in the coordinates of the frame passed in.

    The Haar cascade is always loaded. Eye gaze uses faces from it, even
    when a body detector is what decides whether to follow someone.
    """

    def __init__(self):
        self.kind = None
        self.face = load_frontal_face_cascade()
        try:
            from ultralytics import YOLO
            self.model = YOLO("yolov8n.pt")   # downloads ~6 MB first run
            self.kind = "yolo"
        except Exception as e:
            print(f"[VISION] YOLO unavailable ({e}); using HOG + face fallback")
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self.kind = "hog"
        print(f"[VISION] Person detector: {self.kind}")

    def detect(self, frame, alert):
        if self.kind == "yolo":
            conf = YOLO_CONF_ALERT if alert else YOLO_CONF_NORMAL
            res = self.model(frame, imgsz=320, conf=conf, classes=[0], verbose=False)[0]
            out = []
            for b in res.boxes:
                x1, y1, x2, y2 = b.xyxy[0].tolist()
                out.append((x1, y1, x2 - x1, y2 - y1, float(b.conf[0])))
            return out

        # Fallback: faces catch people up close, HOG catches full bodies far away.
        # self.face is None when the Haar XML is missing (Debian opencv has no
        # cv2.data); HOG still runs.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out = []
        if self.face is not None:
            try:
                faces = self.face.detectMultiScale(gray, scaleFactor=1.1,
                                                   minNeighbors=4 if alert else 6,
                                                   minSize=(40, 40))
            except Exception as e:
                print(f"[VISION] Face detection failed ({e}); HOG only from here")
                self.face = None
                faces = ()
            for (x, y, w, h) in faces:
                out.append((x, y, w, h, 1.0))
        boxes, weights = self.hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(8, 8), scale=1.05,
            hitThreshold=0.0 if alert else 0.3)
        for (x, y, w, h), wt in zip(boxes, weights):
            out.append((x, y, w, h, float(wt)))
        return out

    def detect_faces(self, frame):
        """Haar faces only, in full-frame pixels. Empty if no cascade.

        Runs on a half-size image so it can run every frame next to HOG
        without dragging the Pi down. This is for the eyes, not for driving.
        """
        if self.face is None or frame is None:
            return []
        try:
            small = cv2.resize(frame, (320, 240))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            found = self.face.detectMultiScale(
                gray, scaleFactor=1.15, minNeighbors=4, minSize=(20, 20))
        except Exception as e:
            print(f"[VISION] Face detection failed ({e}); eyes will idle")
            self.face = None
            return []
        height, width = frame.shape[:2]
        sx = width / 320.0
        sy = height / 240.0
        faces = []
        for (x, y, w, h) in found:
            faces.append((x * sx, y * sy, w * sx, h * sy))
        return faces


def salient_motion(blobs, min_area=MOTION_MIN_AREA,
                   width=MOTION_WIDTH, height=MOTION_HEIGHT):
    """Largest blob at or above min_area.

    blobs are (x, y, w, h, area) on the motion image. Returns
    (x_norm, y_norm, area) or None.
    """
    big = [b for b in blobs if b[4] >= min_area]
    if not big:
        return None
    x, y, w, h, area = max(big, key=lambda b: b[4])
    return ((x + w / 2.0) / float(width), (y + h / 2.0) / float(height), float(area))


def face_gaze_point(faces, frame_w, frame_h):
    """Center of the largest face, in 0..1 camera space, or None.

    faces are (x, y, w, h) in frame pixels. Motion is not a face.
    """
    if not faces or not frame_w or not frame_h:
        return None
    x, y, w, h = max(faces, key=lambda b: b[2] * b[3])
    if w <= 0 or h <= 0:
        return None
    return ((x + w / 2.0) / float(frame_w), (y + h / 2.0) / float(frame_h))


def update_gaze(state, now, face_xy, hold_s=GAZE_HOLD_SECONDS):
    """Look at a face. face_xy is (x, y) in 0..1, or None.

    Does not follow motion. A missed frame holds the last face briefly,
    then the eyes idle. This does not change the drive state.
    Returns the smoothed (x, y) or None.
    """
    chosen = face_xy
    if chosen is not None:
        px, py = chosen
        ox, oy = state.get("gaze_x"), state.get("gaze_y")
        if ox is None or oy is None:
            state["gaze_x"], state["gaze_y"] = float(px), float(py)
        else:
            state["gaze_x"] = ox + (float(px) - ox) * 0.5
            state["gaze_y"] = oy + (float(py) - oy) * 0.5
        state["gaze_until"] = now + hold_s
        return (state["gaze_x"], state["gaze_y"])
    if state.get("gaze_x") is not None and now < state.get("gaze_until", 0):
        return (state["gaze_x"], state["gaze_y"])
    state["gaze_x"] = None
    state["gaze_y"] = None
    return None


def shift_gray(gray, shift_px):
    """Shift a grayscale image horizontally, filling the gap with the edge."""
    shift_px = int(shift_px)
    if shift_px == 0:
        return gray
    h, w = gray.shape[:2]
    if abs(shift_px) >= w:
        return gray
    out = gray.copy()
    if shift_px > 0:
        out[:, shift_px:] = gray[:, :-shift_px]
        out[:, :shift_px] = gray[:, :1]
    else:
        span = -shift_px
        out[:, :-span] = gray[:, span:]
        out[:, -span:] = gray[:, -1:]
    return out


# ======================= Motion detection =======================
class MotionDetector:
    """Frame differencing. Returns (x_norm, y_norm, area) of the biggest
    moving blob, or None. pan_degrees compensates a small neck move so the
    robot does not chase its own camera."""

    def __init__(self):
        self.prev = None

    def reset(self):
        self.prev = None

    def detect(self, frame, pan_degrees=0.0):
        small = cv2.resize(frame, (MOTION_WIDTH, MOTION_HEIGHT))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        if self.prev is None:
            self.prev = gray
            return None
        prev = self.prev
        if abs(pan_degrees) >= 0.3:
            # Angle up turns the camera toward image-left, so the scene
            # slides toward image-right. Shift the previous frame to match.
            shift = int(round(float(pan_degrees) * (MOTION_WIDTH / CAMERA_HFOV_DEG)))
            prev = shift_gray(prev, shift)
        self.prev = gray
        diff = cv2.absdiff(prev, gray)
        _, th = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        th = cv2.dilate(th, None, iterations=2)
        contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            blobs.append((x, y, w, h, cv2.contourArea(contour)))
        return salient_motion(blobs)


# ======================= Threads =======================
_jpeg_lock = threading.Lock()
_latest_jpeg = b""
_jpeg_tick = 0


def publish_camera_frame(frame):
    """Keep one modest JPEG for the phone. The vision thread owns the camera."""
    global _latest_jpeg, _jpeg_tick
    _jpeg_tick += 1
    if _jpeg_tick % 2:
        return
    try:
        small = cv2.resize(frame, (320, 240))
        quality = int(getattr(cv2, "IMWRITE_JPEG_QUALITY", 1))
        ok, buf = cv2.imencode(".jpg", small, [quality, 55])
    except Exception:
        return
    if not ok:
        return
    data = buf.tobytes() if hasattr(buf, "tobytes") else bytes(buf)
    with _jpeg_lock:
        _latest_jpeg = data


def latest_jpeg():
    with _jpeg_lock:
        return _latest_jpeg


def open_camera():
    """Try /dev/video0-9 and return the first capture that yields a frame.
    If none work, log an error and try again every 5 seconds."""
    backend = cv2.CAP_V4L2 if hasattr(cv2, "CAP_V4L2") else None
    while state["running"]:
        for index in range(10):
            if backend is None:
                cap = cv2.VideoCapture(index)
            else:
                cap = cv2.VideoCapture(index, backend)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
            cap.set(cv2.CAP_PROP_FPS, 15)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # always use the newest frame
            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None and getattr(frame, "size", 0):
                height, width = frame.shape[:2]
                print(f"[VISION] Camera opened on /dev/video{index} ({width}x{height})")
                return cap
            cap.release()
        print("[VISION] ERROR: no working camera found")
        time.sleep(5)
    return None


def vision_thread():
    cap = open_camera()
    if cap is None:
        return

    detector = PersonDetector()
    motion   = MotionDetector()
    hits     = deque(maxlen=HIT_WINDOW)
    misses_in_a_row = 0
    frame_no = 0
    bad_reads = 0
    last_heartbeat = time.time()
    last_motion_log = 0.0
    last_face_log = 0.0
    servo_at_prev = state["servo_angle"]
    last_tick = time.time()

    while state["running"]:
        now = time.time()
        dt = min(0.2, max(0.0, now - last_tick))
        last_tick = now
        if now - last_heartbeat >= 10.0:
            gx, gy = state.get("gaze_x"), state.get("gaze_y")
            gaze = "none" if gx is None else "{0:.2f},{1:.2f}".format(gx, gy)
            print("[VISION] frames={frames} person_hits={hits} detector={kind} gaze={gaze}".format(
                frames=frame_no, hits=sum(hits), kind=detector.kind, gaze=gaze))
            last_heartbeat = now

        ret, frame = cap.read()
        if not ret:
            bad_reads += 1
            if bad_reads >= 30:
                print("[VISION] Camera stopped giving frames - reopening")
                cap.release()
                cap = open_camera()
                bad_reads = 0
                motion.reset()
                if cap is None:
                    return
            else:
                time.sleep(0.1)
            continue
        bad_reads = 0
        frame_no += 1
        now = time.time()
        publish_camera_frame(frame)

        # Motion runs whenever the wheels are still. A small neck pan is
        # compensated; a big jump resets the reference frame instead.
        pan = state["servo_angle"] - servo_at_prev
        if state["motors_moving"] or abs(pan) > 12:
            motion.reset()
            seen = None
        else:
            seen = motion.detect(frame, pan_degrees=pan)
        servo_at_prev = state["servo_angle"]

        if seen and not state["human_visible"]:
            mx, my, area = seen
            if now >= state["alert_until"] and now - last_motion_log > 2.0:
                print("[VISION] Motion at ({0:.2f}, {1:.2f}) area {2}".format(mx, my, int(area)))
                last_motion_log = now
            state["alert_until"] = now + MOTION_ALERT_SECONDS

        alert = now < state["alert_until"]

        # Person detection: every frame when alert, less often when calm.
        every_n = DETECT_EVERY_N_ALERT if alert else DETECT_EVERY_N_NORMAL
        if frame_no % every_n == 0:
            boxes = detector.detect(frame, alert)
            hits.append(1 if boxes else 0)
            if boxes:
                misses_in_a_row = 0
                if sum(hits) >= HITS_NEEDED:
                    state["human_visible"]   = True
                    state["last_human_seen"] = now
                    state["alert_until"]     = now + MOTION_ALERT_SECONDS
            else:
                misses_in_a_row += 1
                limit = 12 if alert else 8
                if misses_in_a_row >= limit:
                    state["human_visible"] = False

        # Eyes look at a face every frame. This is not the follow state
        # machine and it does not command the wheels. Motion blobs (often
        # the whole background) are not a gaze target.
        faces = detector.detect_faces(frame)
        face_xy = face_gaze_point(faces, frame.shape[1], frame.shape[0])
        if face_xy is not None and state.get("gaze_x") is None and now - last_face_log > 2.0:
            print("[VISION] Face at ({0:.2f}, {1:.2f})".format(face_xy[0], face_xy[1]))
            last_face_log = now
        target = update_gaze(state, now, face_xy)

        if target is not None:
            command_servo(servo_step_toward(state["servo_angle"], target[0], dt))
        else:
            # No face: ease the neck back to center. Do not chase motion.
            nudged = state["servo_angle"] + (SERVO_CENTER - state["servo_angle"]) * min(0.5, dt * 0.6)
            command_servo(nudged)

        if frame_no % every_n != 0:
            time.sleep(0.01)

    cap.release()


def sonar_thread():
    while state["running"]:
        dist = measure_distance_cm()
        state["obstacle_distance_cm"] = dist
        # Only trust sonar as "human distance" when the camera (and so the
        # person) is roughly straight ahead of the sonar
        facing = abs(state["servo_angle"] - SERVO_CENTER) < STEER_DEADBAND_DEG
        if state["human_visible"] and facing:
            state["human_distance_cm"] = dist
        time.sleep(0.06)


def conversation_thread():
    """Listen whenever we are not already talking, then answer with Grok.

    A person does not have to be classified first. Seeing someone (or
    motion) still plays one short greeting if the chat is empty. Later
    turns keep the same conversation.
    """
    recognizer.dynamic_energy_threshold = False
    recognizer.pause_threshold = 0.7
    recognizer.phrase_threshold = 0.3
    recognizer.non_speaking_duration = 0.4
    mic = None
    next_try = 0.0
    while state["running"]:
        if state.get("drive_mode") == "control":
            # The phone is using the jack mic. Let go of it.
            mic = None
            time.sleep(0.15)
            continue
        if state["speaking"] or state["thinking"]:
            time.sleep(0.1)
            continue
        now = time.time()
        if mic is None:
            if now < next_try:
                time.sleep(0.2)
                continue
            mic = open_microphone()
            if mic is None:
                next_try = time.time() + 5
                continue

        try:
            heard = listen_for_speech(mic, timeout=3, phrase_time_limit=8)
        except Exception as e:
            print(f"[MIC] Listen failed ({e}); reopening microphone")
            mic = None
            next_try = time.time() + 2
            continue

        if heard:
            print(f"[HUMAN] {heard}")
            remember_heard(heard)
            state["greeted"] = True
            state["last_heard"] = time.time()
            reply = ai_respond(heard)
            speak(reply)
            state["last_heard"] = time.time()
            time.sleep(0.3)   # let the speaker finish before the mic opens
            continue

        now = time.time()
        present = state["human_visible"] or now < state["alert_until"]
        if present and not state["greeted"] and not state["conversation"]:
            speak("I see you, human. How are you doing, human?")
            state["greeted"] = True
            time.sleep(0.3)


def network_thread():
    """Keeps state["online"] up to date so the face can show it, and so
    the robot says so out loud when Wi-Fi drops or comes back."""
    import socket
    was_online = True
    while state["running"]:
        try:
            socket.create_connection(("api.x.ai", 443), timeout=3).close()
            online = True
        except OSError:
            online = False
        state["online"] = online
        if online != was_online:
            speak_async("I'm back online." if online else "I lost my internet connection.")
            was_online = online
        time.sleep(10)


def steer_toward_person():
    """Turn the body so the camera ends up centered. Returns True if
    already facing the person (so it's OK to drive forward)."""
    if not MOTORS_ENABLED:
        return True
    offset = state["servo_angle"] - SERVO_CENTER   # + = person to the left
    if offset > STEER_DEADBAND_DEG:
        motors_turn_left()
        return False
    if offset < -STEER_DEADBAND_DEG:
        motors_turn_right()
        return False
    return True


def movement_loop():
    """Approach / search state machine.

    While MOTORS_ENABLED is false every drive call writes zero throttle,
    so this loop still updates mood for the face and never turns a wheel.
    """
    robot_state = "IDLE"
    last_search_speech = 0.0
    search_turn_until  = 0.0
    motors_stop()

    while state["running"]:
        if state.get("drive_mode") == "control":
            # Hand driving owns the wheels. Do not zero them from here.
            time.sleep(0.05)
            continue
        now             = time.time()
        obstacle        = state["obstacle_distance_cm"]
        human_dist      = state["human_distance_cm"]
        human_vis       = state["human_visible"]
        time_since_seen = now - state["last_human_seen"]
        alert           = now < state["alert_until"]
        chatting        = _chat_active(now)
        state["mood"]   = robot_state.lower()

        if robot_state == "IDLE":
            motors_stop()
            if human_vis:
                print("[STATE] IDLE -> APPROACH")
                state["greeted"] = False
                robot_state = "APPROACH"
            elif alert:
                # Motion seen: sit still and look (the neck is already turning)
                pass

        elif robot_state == "APPROACH":
            if not human_vis:
                motors_stop()
                print("[STATE] APPROACH -> SEARCHING")
                robot_state = "SEARCHING"
            elif not steer_toward_person():
                pass  # turning to face them, only when the wheels are enabled
            elif obstacle < OBSTACLE_STOP_CM:
                motors_stop()
            elif human_dist <= HUMAN_DISTANCE_CM:
                motors_stop()
                print("[STATE] APPROACH -> MAINTAIN")
                robot_state = "MAINTAIN"
            else:
                motors_forward(SPEED_NORMAL)

        elif robot_state == "MAINTAIN":
            if not human_vis:
                motors_stop()
                print("[STATE] MAINTAIN -> SEARCHING")
                robot_state = "SEARCHING"
            elif not steer_toward_person():
                pass
            elif human_dist < HUMAN_DISTANCE_CM - 10:
                motors_backward()
            elif human_dist > HUMAN_DISTANCE_CM + 15 and obstacle >= OBSTACLE_STOP_CM:
                motors_forward(SPEED_SLOW)
            else:
                motors_stop()

        elif robot_state == "SEARCHING":
            if human_vis:
                motors_stop()
                print("[STATE] SEARCHING -> MAINTAIN")
                robot_state = "MAINTAIN"
            elif alert or state["listening"] or state["speaking"] or state["thinking"] or chatting:
                # Hold still while looking at motion or while a conversation
                # is in progress, so the mic is not drowned by the wheels.
                motors_stop()
            elif time_since_seen > LOST_HUMAN_TIMEOUT:
                if (not chatting and not state["listening"]
                        and now - last_search_speech > SEARCH_SPEECH_COOLDOWN):
                    speak_async("I can't find you, human.")
                    last_search_speech = now
                # Turn in short bursts, then pause so the camera gets a
                # sharp, still frame (and motion detection works).
                # These calls are zero throttle while MOTORS_ENABLED is false.
                if now < search_turn_until:
                    motors_turn_right(SPEED_TURN)
                elif now < search_turn_until + 0.8:
                    motors_stop()
                else:
                    search_turn_until = now + 0.4
                if time_since_seen > CONVERSATION_IDLE_TIMEOUT and not chatting:
                    motors_stop()
                    print("[STATE] SEARCHING -> IDLE")
                    state["greeted"]      = False
                    state["conversation"] = []
                    robot_state = "IDLE"

        time.sleep(0.05)


def main():
    global gpio_handle, motor_left, motor_right, tts_engine, ai_client

    gpio_handle = lgpio.gpiochip_open(0)   # on some Pi 5 OS builds this is 4
    lgpio.gpio_claim_output(gpio_handle, TRIG_PIN)
    lgpio.gpio_claim_input(gpio_handle,  ECHO_PIN)
    lgpio.gpio_claim_output(gpio_handle, SERVO_PIN)
    set_servo_angle(SERVO_CENTER)

    kit         = MotorKit()
    motor_left  = kit.motor1
    motor_right = kit.motor2
    # Kill any leftover throttle before speech, vision, or the face start.
    motors_stop()
    if MOTORS_ENABLED:
        print("[MOTORS] Drive motors are ON.")
    else:
        print("[MOTORS] Drive motors are OFF. Set MOTORS_ENABLED = True in robot_brain.py to drive again.")

    try:
        tts_engine = _new_tts()
    except Exception as e:
        print(f"[TTS] Voice unavailable ({e})")
        tts_engine = None

    ai_client = OpenAI(api_key=XAI_API_KEY, base_url=BASE_URL)

    print("Robot brain starting up...")
    speak("I am online. Looking for humans.")

    threads = [
        threading.Thread(target=vision_thread,       daemon=True, name="Vision"),
        threading.Thread(target=sonar_thread,        daemon=True, name="Sonar"),
        threading.Thread(target=conversation_thread, daemon=True, name="Conversation"),
        threading.Thread(target=network_thread,      daemon=True, name="Network"),
        threading.Thread(target=movement_loop,       daemon=True, name="Movement"),
    ]
    for t in threads:
        t.start()
    threading.Thread(target=drive_watchdog, daemon=True, name="DriveWatch").start()
    try:
        from panel import start_panel
        start_panel()
    except Exception as e:
        print("[PANEL] Phone panel did not start ({0})".format(e))

    try:
        while state["running"]:
            try:
                from face import run_face
                run_face(state)
            except Exception as e:
                # A face error must not take speech, motors, or vision down.
                print(f"[FACE] Face error ({e}); retrying")
                time.sleep(2)
                continue
            if not state["running"]:
                break
            print("[FACE] Face window ended; reopening")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        state["running"] = False
        state["drive_mode"] = "automate"
        time.sleep(0.3)
        try:
            manual_stop()
            motors_stop()
        except Exception as e:
            print(f"[MOTORS] stop failed ({e})")
        try:
            lgpio.tx_servo(gpio_handle, SERVO_PIN, 0)
            lgpio.gpiochip_close(gpio_handle)
        except Exception as e:
            print(f"[GPIO] close failed ({e})")
        print("Robot offline.")


if __name__ == "__main__":
    main()
