"""Behavior checks that do not need the Pi, a camera, or a speaker."""

import json
import os
import sys
import time
import types
import unittest
import wave
import io

os.environ.pop("MOTORS_ENABLED", None)
os.environ.pop("MIC_DEVICE", None)

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _install_stubs():
    def mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    class Recognizer:
        def __init__(self):
            self.dynamic_energy_threshold = True
            self.pause_threshold = 0.8
            self.phrase_threshold = 0.3
            self.non_speaking_duration = 0.3
            self.energy_threshold = 300

    class Microphone:
        def __init__(self, device_index=None):
            self.device_index = device_index

    class WaitTimeoutError(Exception):
        pass

    mod("speech_recognition",
        Recognizer=Recognizer,
        Microphone=Microphone,
        WaitTimeoutError=WaitTimeoutError,
        UnknownValueError=Exception,
        RequestError=Exception)

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    mod("openai", OpenAI=OpenAI)

    class MotorKit:
        def __init__(self, *args, **kwargs):
            pass

    mod("adafruit_motorkit", MotorKit=MotorKit)
    mod("lgpio")
    mod("pyttsx3", init=lambda: None)
    mod("cv2")


_install_stubs()

import face  # noqa: E402
import robot_brain as rb  # noqa: E402
import panel  # noqa: E402


class FacePanelTests(unittest.TestCase):
    def test_prefers_dsi_panel_whichever_index(self):
        self.assertEqual(face.choose_face_panel([(2560, 1080), (800, 480)]), (1, (800, 480)))
        self.assertEqual(face.choose_face_panel([(800, 480), (2560, 1080)])[0], 0)
        self.assertEqual(face.choose_face_panel([(480, 800), (1920, 1080)])[0], 0)

    def test_smallest_when_no_dsi_size(self):
        index, size = face.choose_face_panel([(2560, 1080), (1920, 1080)])
        self.assertEqual((index, size), (1, (1920, 1080)))

    def test_empty_defaults_to_dsi_size(self):
        self.assertEqual(face.choose_face_panel([]), (0, (800, 480)))

    def test_800x480_crop_is_wider_and_fills_the_panel(self):
        sx, sy = face.face_art_scale(800, 480)
        self.assertGreater(sx, sy)
        self.assertGreater(sx / sy, 1.15)
        left, top, right, bottom = face.FACE_CROP
        x0, y0 = face.map_art(left, top, 800, 480)
        x1, y1 = face.map_art(right, bottom, 800, 480)
        self.assertEqual(x0, 0)
        self.assertEqual(y0, 0)
        self.assertAlmostEqual(x1, 800)
        self.assertAlmostEqual(y1, 480)

    def test_eyes_and_mouth_stay_inside_the_crop(self):
        with open(os.path.join(HERE, "face_assets", "face.json")) as fh:
            info = json.load(fh)
        for box in (e["mask_box"] for e in info["eyes"]):
            x0, y0 = face.map_art(box[0], box[1], 800, 480)
            x1, y1 = face.map_art(box[2], box[3], 800, 480)
            self.assertGreater(x0, 0)
            self.assertLess(x1, 800)
            self.assertGreater(y0, 0)
            self.assertLess(y1, 480)
        mouth = info["layers"]["talk_open"]
        mx0, my0 = face.map_art(mouth[0], mouth[1], 800, 480)
        mx1, my1 = face.map_art(mouth[2], mouth[3], 800, 480)
        self.assertGreater(mx0, 0)
        self.assertLess(mx1, 800)
        self.assertGreater(my0, 0)
        self.assertAlmostEqual(my1, 480)


class GazeTests(unittest.TestCase):
    def test_image_right_looks_screen_left_and_beats_thinking(self):
        tx, ty, _ = face.gaze_offsets(
            {"gaze_x": 1.0, "gaze_y": 0.5, "thinking": True}, 0, (999, (0, 0)))
        self.assertEqual(tx, -1.0)
        self.assertEqual(ty, 0.0)

    def test_image_bottom_looks_down(self):
        tx, ty, _ = face.gaze_offsets(
            {"gaze_x": 0.5, "gaze_y": 1.0}, 0, (999, (0, 0)))
        self.assertEqual(tx, 0.0)
        self.assertEqual(ty, 1.0)

    def test_idle_when_nothing_to_track(self):
        tx, ty, _ = face.gaze_offsets(
            {"gaze_x": None, "gaze_y": None, "thinking": False, "mood": "idle"},
            0, (999, (0.2, -0.1)))
        self.assertEqual((tx, ty), (0.2, -0.1))

    def test_thinking_glance_only_without_a_target(self):
        tx, ty, _ = face.gaze_offsets(
            {"gaze_x": None, "gaze_y": None, "thinking": True, "mood": "idle"},
            0, (999, (0, 0)))
        self.assertLess(ty, 0)


class KioskTests(unittest.TestCase):
    def test_only_minimize_restores_fullscreen(self):
        self.assertEqual(face.RESTORE_WINDOW_EVENTS, ("WINDOWMINIMIZED",))
        constants = {"WINDOWMINIMIZED": 10, "WINDOWFOCUSLOST": 11, "WINDOWHIDDEN": 12, "QUIT": 13}
        self.assertEqual(face.face_event_should_restore(10, constants), "WINDOWMINIMIZED")
        self.assertIsNone(face.face_event_should_restore(11, constants))
        self.assertIsNone(face.face_event_should_restore(12, constants))
        self.assertIsNone(face.face_event_should_restore(13, constants))
        for name in ("start_robot.sh", "install_on_pi.sh"):
            with open(os.path.join(HERE, name)) as fh:
                text = fh.read()
            self.assertIn("ALSA_CARD=Device", text)
            self.assertIn('slave.pcm "hw:Device,0"', text)
        with open(os.path.join(HERE, "face.py")) as fh:
            pump = fh.read().split("def pump_kiosk_events", 1)[1]
        self.assertNotIn("get_active", pump)
        self.assertNotIn("WINDOWFOCUSLOST", pump)

    def test_restore_is_debounced(self):
        face._face_display["last_restore"] = face.time.time()
        self.assertIsNone(face.restore_face_fullscreen("WINDOWMINIMIZED"))

    def test_face_code_does_not_stop_the_robot(self):
        for name in ("face.py", "face_cartoon.py"):
            with open(os.path.join(HERE, name)) as fh:
                source = fh.read()
            self.assertNotIn('state["running"] = False', source)
            self.assertNotIn("state['running'] = False", source)


class AudioTests(unittest.TestCase):
    def test_silence_is_dummy_before_init(self):
        face.silence_pygame_audio()
        self.assertEqual(os.environ["SDL_AUDIODRIVER"], "dummy")
        self.assertEqual(os.environ["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"], "0")
        with open(os.path.join(HERE, "face.py")) as fh:
            source = fh.read()
        prepare = source.split("def prepare_pygame", 1)[1].split("def face_event_should_restore", 1)[0]
        self.assertLess(prepare.index("silence_pygame_audio()"), prepare.index("pygame.init()"))
        self.assertLess(prepare.index("pygame.init()"), prepare.index("release_pygame_mixer()"))


class MotorTests(unittest.TestCase):
    def test_default_off_and_env_override(self):
        self.assertFalse(rb.MOTORS_ENABLED)
        self.assertFalse(rb.motors_enabled_setting(False, None))
        self.assertFalse(rb.motors_enabled_setting(False, ""))
        self.assertTrue(rb.motors_enabled_setting(False, "1"))
        self.assertTrue(rb.motors_enabled_setting(False, "true"))
        self.assertFalse(rb.motors_enabled_setting(True, "0"))
        self.assertTrue(rb.motors_enabled_setting(True, None))

    def test_disabled_drive_writes_zero_throttle(self):
        class Motor:
            def __init__(self):
                self.throttle = 0.4
        left, right = Motor(), Motor()
        rb.motor_left = left
        rb.motor_right = right
        was = rb.MOTORS_ENABLED
        try:
            rb.MOTORS_ENABLED = False
            rb.motors_forward(0.6)
            self.assertEqual(left.throttle, 0)
            self.assertEqual(right.throttle, 0)
            self.assertFalse(rb.state["motors_moving"])
            rb.MOTORS_ENABLED = True
            rb.motors_forward(0.6)
            self.assertEqual(left.throttle, 0.6)
            self.assertEqual(right.throttle, 0.6)
            rb.motors_stop()
            self.assertEqual(left.throttle, 0)
            self.assertEqual(right.throttle, 0)
        finally:
            rb.MOTORS_ENABLED = was
            rb.motor_left = None
            rb.motor_right = None


class ConversationTests(unittest.TestCase):
    def test_multi_turn_history_goes_to_grok(self):
        class Message:
            def __init__(self, content):
                self.content = content

        class Choice:
            def __init__(self, content):
                self.message = Message(content)

        class Response:
            def __init__(self, content):
                self.choices = [Choice(content)]

        class Completions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return Response("Hello, human.")

        class Client:
            def __init__(self):
                self.chat = types.SimpleNamespace(completions=Completions())

        client = Client()
        rb.ai_client = client
        rb.state["conversation"] = []
        rb.state["thinking"] = False
        first = rb.ai_respond("hello there")
        second = rb.ai_respond("what can you do")
        self.assertEqual(first, "Hello, human.")
        self.assertEqual(second, "Hello, human.")
        second_call = client.chat.completions.calls[1]
        roles = [m["role"] for m in second_call["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(second_call["messages"][1]["content"], "hello there")
        self.assertEqual(second_call["messages"][3]["content"], "what can you do")
        self.assertIn("api.x.ai", rb.BASE_URL)
        self.assertIn("api.x.ai", rb.STT_URL)

    def test_stt_body_puts_wav_last_and_not_the_key(self):
        wav = b"RIFFxxxxWAVEdata"
        body = rb.build_stt_body(wav, "BoundZZ")
        self.assertIn(b"grok-voice-transcribe-2.0", body)
        self.assertLess(body.find(b'name="model"'), body.find(b'name="file"'))
        self.assertLess(body.find(wav), body.rfind(b"--BoundZZ--"))
        self.assertNotIn(rb.XAI_API_KEY.encode(), body)
        text = rb.transcript_from_stt_payload(b'{"text": "  hello human  "}')
        self.assertEqual(text, "hello human")
        self.assertIsNone(rb.clean_transcript("."))
        self.assertIsNone(rb.transcript_from_stt_payload({"text": ""}))

    def test_wav_wrapper_roundtrip(self):
        pcm = b"\x00\x00\x10\x00\xff\x7f"
        data = rb.pcm_to_wav(pcm, 16000, 2)
        self.assertTrue(data.startswith(b"RIFF"))
        with wave.open(io.BytesIO(data), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.readframes(wf.getnframes()), pcm)

    def test_usb_mic_beats_hdmi_and_pulse(self):
        devices = [
            {"index": 0, "name": "vc4-hdmi-0", "max_input_channels": 0},
            {"index": 1, "name": "hdmi monitor", "max_input_channels": 2},
            {"index": 2, "name": "pulse", "max_input_channels": 2},
            {"index": 4, "name": "USB PnP Audio Device", "max_input_channels": 1},
        ]
        self.assertEqual(rb.select_input_device(devices), 4)
        self.assertIsNone(rb.select_input_device([
            {"index": 1, "name": "hdmi monitor", "max_input_channels": 2}]))
        self.assertIsNone(rb.select_input_device([]))
        self.assertEqual(rb.select_input_device([
            {"index": 3, "name": "UVC Camera", "max_input_channels": 1},
            {"index": 1, "name": "pulse", "max_input_channels": 1},
        ]), 3)

    def test_jack_adapter_beats_webcam_mic(self):
        # Names from the live Pi. The camera used to win because both say
        # "usb" and the camera got an extra bonus for the word "camera".
        devices = [
            {"index": 0, "name": "USB 2.0 Camera: Audio (hw:2,0)", "max_input_channels": 1},
            {"index": 1, "name": "USB Audio Device: - (hw:3,0)", "max_input_channels": 1},
        ]
        self.assertGreater(
            rb._input_score(devices[1]), rb._input_score(devices[0]))
        self.assertEqual(rb.select_input_device(devices), 1)
        self.assertEqual(rb.select_input_device(devices, prefer="hw:2,0"), 0)
        self.assertEqual(rb.select_input_device(devices, prefer="0"), 0)
        self.assertEqual(rb.select_input_device(devices, prefer="Device"), 1)
        self.assertEqual(rb.select_input_device(devices, prefer="Audio"), 1)

    def test_quiet_audio_is_not_sent_as_speech(self):
        def pcm(values):
            data = bytearray()
            for value in values:
                data += int(value).to_bytes(2, "little", signed=True)
            return bytes(data)

        self.assertFalse(rb.audio_has_speech(pcm([0] * 16000), 16000, 2))
        self.assertFalse(rb.audio_has_speech(pcm([80, -80] * 8000), 16000, 2))
        self.assertFalse(rb.audio_has_speech(pcm([8000] * 200), 16000, 2))
        tone = [4000 if (i // 20) % 2 == 0 else -4000 for i in range(6400)]
        self.assertTrue(rb.audio_has_speech(pcm(tone), 16000, 2))


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.state = {"gaze_x": None, "gaze_y": None, "gaze_until": 0.0}

    def test_largest_face_is_what_the_eyes_follow(self):
        faces = [(10, 10, 20, 20), (100, 40, 80, 90)]
        point = rb.face_gaze_point(faces, 640, 480)
        self.assertAlmostEqual(point[0], (100 + 40) / 640.0)
        self.assertAlmostEqual(point[1], (40 + 45) / 480.0)
        self.assertIsNone(rb.face_gaze_point([], 640, 480))
        looked = rb.update_gaze(self.state, 5.0, point)
        self.assertEqual(looked, point)
        # A missed frame holds the face, then the eyes idle.
        self.assertIsNotNone(rb.update_gaze(self.state, 5.2, None))
        self.assertIsNone(rb.update_gaze(self.state, 30.0, None))
        self.assertIsNone(self.state["gaze_x"])

    def test_motion_does_not_move_the_eyes(self):
        blob = rb.salient_motion([(20, 30, 200, 180, 55000)])
        self.assertIsNotNone(blob)
        self.assertIsNone(rb.update_gaze(self.state, 1.0, None))
        self.assertIsNone(self.state["gaze_x"])
        self.assertIsNone(self.state["gaze_y"])

    def test_largest_blob_wins(self):
        blobs = [(0, 0, 10, 10, 100), (40, 20, 80, 60, 5000), (10, 10, 30, 30, 2000)]
        nx, ny, area = rb.salient_motion(blobs)
        self.assertEqual(area, 5000)
        self.assertAlmostEqual(nx, (40 + 40) / 320.0)
        self.assertAlmostEqual(ny, (20 + 30) / 240.0)
        self.assertIsNone(rb.salient_motion([(0, 0, 2, 2, 10)]))

    def test_neck_turns_toward_image_right_and_clamps(self):
        self.assertLess(rb.servo_step_toward(90, 0.8, 0.1), 90)
        self.assertGreater(rb.servo_step_toward(90, 0.2, 0.1), 90)
        self.assertEqual(rb.servo_step_toward(90, 0.5, 0.1), 90)
        self.assertEqual(rb.servo_step_toward(30, 1.0, 0.2), 30)

    def test_shift_gray_moves_pixels(self):
        import numpy as np
        gray = np.zeros((4, 6), dtype=np.uint8)
        gray[1, 2] = 9
        shifted = rb.shift_gray(gray, 1)
        self.assertEqual(int(shifted[1, 3]), 9)
        self.assertEqual(int(shifted[1, 2]), 0)


class PanelTests(unittest.TestCase):
    def setUp(self):
        class Motor:
            def __init__(self):
                self.throttle = 0
        self.left = Motor()
        self.right = Motor()
        rb.motor_left = self.left
        rb.motor_right = self.right
        rb.state["drive_mode"] = "automate"
        rb.state["heard_lines"] = []
        rb.state["speaking"] = False
        rb.manual_stop()

    def tearDown(self):
        rb.state["drive_mode"] = "automate"
        rb.manual_stop()
        rb.motor_left = None
        rb.motor_right = None
        rb.state["speaking"] = False

    def test_control_drives_even_while_automate_motors_are_off(self):
        self.assertFalse(rb.MOTORS_ENABLED)
        self.assertEqual(rb.set_drive_mode("control"), "control")
        self.assertEqual(self.left.throttle, 0)
        self.assertEqual(rb.manual_drive("forward"), "forward")
        self.assertAlmostEqual(self.left.throttle, rb.MANUAL_SPEED)
        self.assertAlmostEqual(self.right.throttle, rb.MANUAL_SPEED)
        self.assertEqual(rb.manual_drive("left"), "left")
        self.assertLess(self.left.throttle, 0)
        self.assertGreater(self.right.throttle, 0)
        self.assertEqual(rb.manual_drive("stop"), "stop")
        self.assertEqual(self.left.throttle, 0)
        self.assertEqual(self.right.throttle, 0)

    def test_release_timeout_and_automate_stop_the_wheels(self):
        rb.set_drive_mode("control")
        rb.manual_drive("back")
        self.assertLess(self.left.throttle, 0)
        rb.state["manual_stamp"] = time.time() - 5
        self.assertTrue(rb.expire_manual_drive())
        self.assertEqual(self.left.throttle, 0)
        rb.manual_drive("right")
        self.assertGreater(self.left.throttle, 0)
        self.assertEqual(rb.set_drive_mode("automate"), "automate")
        self.assertEqual(self.left.throttle, 0)
        self.assertIsNone(rb.manual_drive("forward"))
        self.assertEqual(self.left.throttle, 0)
        self.assertEqual(self.right.throttle, 0)

    def test_typed_speech_uses_speak_and_not_the_chat(self):
        said = []
        spoken = rb.say_typed("  hello there  ", speaker=said.append)
        self.assertEqual(spoken, "hello there")
        self.assertEqual(said, ["hello there"])
        self.assertIsNone(rb.say_typed("   ", speaker=said.append))
        self.assertEqual(rb.clean_say_text("x" * 500), "x" * 300)
        self.assertTrue(rb.state["speaking"])
        status = rb.panel_status()
        self.assertNotIn("key", json.dumps(status).lower())
        self.assertNotIn("robot.env", json.dumps(status))
        rb.remember_heard("can you hear me")
        self.assertIn("can you hear me", rb.panel_status()["heard"])

    def test_page_has_controls_and_no_secrets(self):
        page = panel.load_page()
        for label in ("AUTOMATE", "CONTROL", "Forward", "Back", "Left", "Right", "Say", "Hear microphone"):
            self.assertIn(label, page)
        lowered = page.lower()
        self.assertNotIn("robot.env", lowered)
        self.assertNotIn("xai", lowered)
        self.assertNotIn("api_key", lowered)
        fields = panel.fields_from_request("/api/drive?dir=forward", "dir=stop", "")
        self.assertEqual(fields["dir"], "stop")
        fields = panel.fields_from_request("/api/say", '{"text": "hi"}', "application/json")
        self.assertEqual(fields["text"], "hi")


class PanelHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = panel.start_panel(port=8765)
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.shutdown()
            cls.server.server_close()
        rb.state["drive_mode"] = "automate"
        rb.manual_stop()

    def test_phone_routes_stop_on_mode_change(self):
        import urllib.error
        import urllib.request

        def post(path, body):
            req = urllib.request.Request(
                "http://127.0.0.1:8765" + path, data=body.encode(), method="POST")
            with urllib.request.urlopen(req, timeout=2) as res:
                return json.loads(res.read().decode())

        def get(path):
            with urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=2) as res:
                return res.status, res.read()

        status, page = get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"CONTROL", page)
        self.assertNotIn(b"robot.env", page.lower())
        self.assertEqual(post("/api/mode", "mode=control")["mode"], "control")
        self.assertEqual(post("/api/drive", "dir=forward")["dir"], "forward")
        self.assertGreater(rb.state["manual_left"], 0)
        self.assertEqual(post("/api/drive", "dir=stop")["dir"], "stop")
        self.assertEqual(rb.state["manual_left"], 0)
        self.assertEqual(post("/api/mode", "mode=automate")["mode"], "automate")
        self.assertEqual(rb.state["manual_left"], 0)
        refused = post("/api/drive", "dir=forward")
        self.assertFalse(refused["ok"])
        self.assertEqual(rb.state["manual_left"], 0)
        said = []
        original = rb.speak
        rb.speak = lambda text: said.append(text)
        try:
            self.assertTrue(post("/api/say", "text=beep")["ok"])
            deadline = time.time() + 1
            while not said and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(said, ["beep"])
        finally:
            rb.speak = original
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/mic", timeout=2)
            self.fail("mic stream should refuse while automate")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)


if __name__ == "__main__":
    unittest.main()
