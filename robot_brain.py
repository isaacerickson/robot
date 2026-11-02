"""
robot_brain.py  -  Isaac's Human-Seeking Conversational Robot  (v2)
Hardware: Raspberry Pi 5, Adafruit Motor HAT, HC-SR04 ultrasonic,
          USB webcam, USB microphone, speaker/amp, MG996R servo.
Pi 5 COMPATIBLE - uses lgpio instead of RPi.GPIO.

What's new in v2
  - Brain is Grok (xAI API) instead of ChatGPT.
  - Much better person detection: YOLOv8n (ultralytics) if installed,
    otherwise falls back to HOG body + Haar face detection.
  - Motion = "probably a person": movement puts the robot on ALERT. It
    turns the camera toward the motion, checks every frame, and accepts
    weaker detections for a few seconds.
  - Steers the wheels toward the person (camera pans, body follows), so
    the sonar actually points at them before trusting the distance.
  - Detection uses "2 hits out of the last 5 frames" instead of
    "3 in a row", so one missed frame no longer resets it.
  - "I can't find you" no longer freezes the wheels for 5 seconds.
  - Speech is thread-safe (no overlapping pyttsx3 calls).

Install once:
  pip install ultralytics openai SpeechRecognition pyttsx3 adafruit-circuitpython-motorkit lgpio
  (ultralytics is optional but strongly recommended)
Run:
  export XAI_API_KEY="xai-..."
  python3 robot_brain.py
"""

import os
import cv2
import time
import threading
from collections import deque

import lgpio
import speech_recognition as sr
import pyttsx3
from openai import OpenAI
from adafruit_motorkit import MotorKit

# ---------------- Grok (xAI) settings ----------------
XAI_API_KEY = os.environ.get("XAI_API_KEY", "xai-YOUR_API_KEY_HERE")
BASE_URL    = "https://api.x.ai/v1"
MODEL       = "grok-3-mini"      # check console.x.ai for current model names

# ---------------- Behavior tuning ----------------
HUMAN_DISTANCE_CM         = 61
OBSTACLE_STOP_CM          = 25
SPEED_NORMAL              = 0.6
SPEED_SLOW                = 0.35
SPEED_TURN                = 0.35

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
MOTION_ALERT_SECONDS      = 3.0    # how long motion keeps us on alert
SERVO_SETTLE_SECONDS      = 0.35   # ignore motion right after camera moves

# Steering: camera servo angle where the body should turn to follow
SERVO_CENTER              = 90.0
SERVO_MIN, SERVO_MAX      = 30.0, 150.0
STEER_DEADBAND_DEG        = 15.0   # within this, sonar points at the person

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
    "alert_until":          0.0,     # motion alert active until this time
    "motors_moving":        False,
    "greeted":              False,
    "conversation":         [],
    "speaking":             False,
    "running":              True,
    "thinking":             False,   # waiting on Grok (face looks up)
    "mood":                 "idle",  # idle / approach / maintain / searching
    "online":               True,    # internet reachable
}

gpio_handle  = None
motor_left   = None
motor_right  = None
tts_engine   = None
ai_client    = None
tts_lock     = threading.Lock()
recognizer   = sr.Recognizer()
microphone   = sr.Microphone()


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


def _set_motors(left, right):
    motor_left.throttle  = left
    motor_right.throttle = right
    state["motors_moving"] = (left != 0 or right != 0)

def motors_stop():                      _set_motors(0, 0)
def motors_forward(speed=SPEED_NORMAL): _set_motors(speed, speed)
def motors_backward(speed=SPEED_SLOW):  _set_motors(-speed, -speed)
def motors_turn_left(speed=SPEED_TURN): _set_motors(-speed, speed)
def motors_turn_right(speed=SPEED_TURN):_set_motors(speed, -speed)


# ======================= Speech =======================
def speak(text):
    with tts_lock:
        state["speaking"] = True
        print(f"[ROBOT] {text}")
        tts_engine.say(text)
        tts_engine.runAndWait()
        state["speaking"] = False

def speak_async(text):
    if state["speaking"]:
        return
    state["speaking"] = True   # set now so nothing else talks over it
    threading.Thread(target=speak, args=(text,), daemon=True).start()

def listen_for_speech(timeout=5):
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        try:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=10)
            return recognizer.recognize_google(audio)
        except (sr.WaitTimeoutError, sr.UnknownValueError, sr.RequestError):
            return None


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


# ======================= Person detectors =======================
class PersonDetector:
    """YOLOv8n if available, else HOG body + Haar face. Returns list of
    (x, y, w, h, conf) boxes in the coordinates of the frame passed in."""

    def __init__(self):
        self.kind = None
        try:
            from ultralytics import YOLO
            self.model = YOLO("yolov8n.pt")   # downloads ~6 MB first run
            self.kind = "yolo"
        except Exception as e:
            print(f"[VISION] YOLO unavailable ({e}); using HOG + face fallback")
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self.face = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
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

        # Fallback: faces catch people up close, HOG catches full bodies far away
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out = []
        faces = self.face.detectMultiScale(gray, scaleFactor=1.1,
                                           minNeighbors=4 if alert else 6,
                                           minSize=(40, 40))
        for (x, y, w, h) in faces:
            out.append((x, y, w, h, 1.0))
        boxes, weights = self.hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(8, 8), scale=1.05,
            hitThreshold=0.0 if alert else 0.3)
        for (x, y, w, h), wt in zip(boxes, weights):
            out.append((x, y, w, h, float(wt)))
        return out


# ======================= Motion detection =======================
class MotionDetector:
    """Simple frame differencing. Returns (x_center_fraction, area) of the
    biggest moving blob, or None."""

    def __init__(self):
        self.prev = None

    def reset(self):
        self.prev = None

    def detect(self, frame):
        small = cv2.resize(frame, (320, 240))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        if self.prev is None:
            self.prev = gray
            return None
        diff = cv2.absdiff(self.prev, gray)
        self.prev = gray
        _, th = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        th = cv2.dilate(th, None, iterations=2)
        contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        biggest = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(biggest)
        if area < MOTION_MIN_AREA:
            return None
        x, y, w, h = cv2.boundingRect(biggest)
        return ((x + w / 2.0) / 320.0, area)


# ======================= Threads =======================
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

    while state["running"]:
        now = time.time()
        if now - last_heartbeat >= 10.0:
            print(f"[VISION] frames={frame_no} person_hits={sum(hits)} detector={detector.kind}")
            last_heartbeat = now

        ret, frame = cap.read()
        if not ret:
            bad_reads += 1
            if bad_reads >= 30:
                print("[VISION] Camera stopped giving frames - reopening")
                cap.release()
                cap = open_camera()
                bad_reads = 0
                if cap is None:
                    return
            else:
                time.sleep(0.1)
            continue
        bad_reads = 0
        frame_no += 1
        now = time.time()

        # ---- Motion: only trust it when the camera and wheels are still ----
        camera_still = (now - _last_servo_move) > SERVO_SETTLE_SECONDS
        if camera_still and not state["motors_moving"]:
            m = motion.detect(frame)
            if m and not state["human_visible"]:
                mx, area = m
                if now > state["alert_until"]:
                    print(f"[VISION] Motion seen (area {int(area)}) - paying attention")
                state["alert_until"] = now + MOTION_ALERT_SECONDS
                # Glance toward the motion
                error = mx - 0.5
                set_servo_angle(state["servo_angle"] - error * 40.0)
        else:
            motion.reset()   # avoid fake motion from our own movement

        alert = now < state["alert_until"]

        # ---- Person detection: every frame when alert, less often when calm ----
        every_n = DETECT_EVERY_N_ALERT if alert else DETECT_EVERY_N_NORMAL
        if frame_no % every_n != 0:
            time.sleep(0.01)
            continue

        boxes = detector.detect(frame, alert)
        hits.append(1 if boxes else 0)

        if boxes:
            misses_in_a_row = 0
            if sum(hits) >= HITS_NEEDED:
                state["human_visible"]   = True
                state["last_human_seen"] = now
                state["alert_until"]     = now + MOTION_ALERT_SECONDS
                x, y, w, h, _ = max(boxes, key=lambda b: b[2] * b[3])
                center_x = (x + w / 2.0) / frame.shape[1]
                error    = center_x - 0.5
                if abs(error) > 0.05:
                    set_servo_angle(state["servo_angle"] - error * 25.0)
        else:
            misses_in_a_row += 1
            # Stay "visible" a bit longer when alert, it's probably still there
            limit = 12 if alert else 8
            if misses_in_a_row >= limit:
                state["human_visible"] = False
            if not alert and not state["human_visible"]:
                # Drift camera back to center when calm
                set_servo_angle(state["servo_angle"] +
                                (SERVO_CENTER - state["servo_angle"]) * 0.05)

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
    while state["running"]:
        if not state["human_visible"] or state["speaking"]:
            time.sleep(0.2)
            continue
        if not state["greeted"]:
            time.sleep(0.5)
            if state["human_visible"]:
                speak("I see you, human.")
                time.sleep(0.5)
                speak("How are you doing, human?")
                state["greeted"]      = True
                state["conversation"] = []
        if state["greeted"] and state["human_visible"]:
            human_text = listen_for_speech(timeout=5)
            if human_text:
                print(f"[HUMAN] {human_text}")
                speak(ai_respond(human_text))
        time.sleep(0.1)


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
    offset = state["servo_angle"] - SERVO_CENTER   # + = person to the left
    if offset > STEER_DEADBAND_DEG:
        motors_turn_left()
        return False
    if offset < -STEER_DEADBAND_DEG:
        motors_turn_right()
        return False
    return True


def movement_loop():
    robot_state = "IDLE"
    last_search_speech = 0.0
    search_turn_until  = 0.0

    while state["running"]:
        now             = time.time()
        obstacle        = state["obstacle_distance_cm"]
        human_dist      = state["human_distance_cm"]
        human_vis       = state["human_visible"]
        time_since_seen = now - state["last_human_seen"]
        alert           = now < state["alert_until"]
        state["mood"]   = robot_state.lower()

        if robot_state == "IDLE":
            motors_stop()
            if human_vis:
                print("[STATE] IDLE -> APPROACH")
                state["greeted"] = False
                robot_state = "APPROACH"
            elif alert:
                # Motion seen: sit still and look (camera is already turning)
                pass

        elif robot_state == "APPROACH":
            if not human_vis:
                motors_stop()
                print("[STATE] APPROACH -> SEARCHING")
                robot_state = "SEARCHING"
            elif not steer_toward_person():
                pass  # turning to face them
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
            elif alert:
                # Motion while searching: stop spinning and look at it
                motors_stop()
            elif time_since_seen > LOST_HUMAN_TIMEOUT:
                if now - last_search_speech > SEARCH_SPEECH_COOLDOWN:
                    speak_async("I can't find you, human.")
                    last_search_speech = now
                # Turn in short bursts, then pause so the camera gets a
                # sharp, still frame (and motion detection works)
                if now < search_turn_until:
                    motors_turn_right(SPEED_TURN)
                elif now < search_turn_until + 0.8:
                    motors_stop()
                else:
                    search_turn_until = now + 0.4
                if time_since_seen > CONVERSATION_IDLE_TIMEOUT:
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

    tts_engine = pyttsx3.init()   # uses espeak-ng on the Pi
    tts_engine.setProperty("rate",   165)
    tts_engine.setProperty("volume", 0.9)

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

    try:
        try:
            from face import run_face
            run_face(state)           # runs until Esc/q or shutdown
        except Exception as e:        # no screen? keep the robot running anyway
            print(f"[FACE] Face disabled: {e}")
            while state["running"]:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        state["running"] = False
        time.sleep(0.3)
        motors_stop()
        lgpio.tx_servo(gpio_handle, SERVO_PIN, 0)
        lgpio.gpiochip_close(gpio_handle)
        print("Robot offline.")


if __name__ == "__main__":
    main()
