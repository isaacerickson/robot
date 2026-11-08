"""Phone control panel for the robot.

Open http://robot.local:8080 on the same Wi-Fi. The page never receives
robot.env or the API key. The camera picture is the latest frame from the
vision thread. The jack microphone is streamed only in CONTROL, so it does
not fight conversation listening.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import robot_brain as rb

PANEL_PORT = 8080
_HERE = os.path.dirname(os.path.abspath(__file__))
_PAGE = ""


def load_page():
    global _PAGE
    with open(os.path.join(_HERE, "panel.html"), encoding="utf-8") as handle:
        _PAGE = handle.read()
    return _PAGE


def fields_from_request(path, body, content_type):
    """Merge the query string and a form or JSON body. Last value wins."""
    query = parse_qs(urlparse(path).query)
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/json" and body:
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        parsed = {str(k): [str(v)] for k, v in data.items()} if isinstance(data, dict) else {}
    else:
        parsed = parse_qs(body or "", keep_blank_values=True)
    merged = {}
    for source in (query, parsed):
        for key, values in source.items():
            if values:
                merged[key] = values[-1]
    return merged


class MicRelay:
    """One capture of the jack mic, fanned out to phone listeners.

    Started only while CONTROL is on and someone is listening. AUTOMATE
    keeps the device for conversation instead.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.thread = None
        self.stop_flag = False

    def subscribe(self):
        queue = []
        with self.lock:
            self.clients.append(queue)
        self.start()
        return queue

    def unsubscribe(self, queue):
        with self.lock:
            if queue in self.clients:
                self.clients.remove(queue)
        if not self.clients:
            self.stop()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.stop_flag = False
                return
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="PhoneMic", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_flag = True

    def _run(self):
        try:
            import pyaudio
        except Exception as exc:
            print("[PANEL] microphone unavailable ({0})".format(exc))
            return
        while not self.stop_flag and rb.state.get("drive_mode") == "control" and rb.state.get("running", True):
            stream = None
            audio = None
            try:
                devices = rb.describe_input_devices()
                index = rb.select_input_device(devices)
                audio = pyaudio.PyAudio()
                stream = audio.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    input_device_index=index,
                    frames_per_buffer=1024,
                )
                print("[PANEL] streaming jack mic to the phone")
                while (not self.stop_flag and rb.state.get("drive_mode") == "control"
                       and rb.state.get("running", True)):
                    chunk = stream.read(1024, exception_on_overflow=False)
                    with self.lock:
                        clients = list(self.clients)
                    for queue in clients:
                        queue.append(chunk)
                        if len(queue) > 12:
                            del queue[:-12]
                    if not clients:
                        time.sleep(0.05)
            except Exception as exc:
                print("[PANEL] mic stream ({0})".format(exc))
                time.sleep(0.4)
            finally:
                try:
                    if stream is not None:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
                try:
                    if audio is not None:
                        audio.terminate()
                except Exception:
                    pass
        print("[PANEL] mic stream stopped")


RELAY = MicRelay()


class PanelHandler(BaseHTTPRequestHandler):
    # 1.0 so the live microphone can stream without a Content-Length.
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload), "application/json")

    def _fields(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return fields_from_request(self.path, raw, self.headers.get("Content-Type", ""))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            page = _PAGE or load_page()
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/camera.jpg":
            frame = rb.latest_jpeg()
            if not frame:
                self._send(503, b"", "image/jpeg")
                return
            self._send(200, frame, "image/jpeg")
            return
        if path == "/api/status":
            self._json(200, rb.panel_status())
            return
        if path == "/mic":
            self._stream_mic()
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        fields = self._fields()
        if path == "/api/mode":
            mode = rb.set_drive_mode(fields.get("mode", "automate"))
            if mode != "control":
                RELAY.stop()
            self._json(200, {"ok": True, "mode": mode})
            return
        if path == "/api/drive":
            direction = rb.manual_drive(fields.get("dir"))
            if direction is None and rb.normalize_direction(fields.get("dir")) is None:
                self._json(400, {"ok": False, "error": "unknown direction"})
                return
            if direction is None:
                self._json(200, {"ok": False, "error": "not in control", "mode": rb.state.get("drive_mode")})
                return
            self._json(200, {"ok": True, "dir": direction})
            return
        if path == "/api/say":
            spoken = rb.say_typed(fields.get("text"))
            if not spoken:
                self._json(400, {"ok": False, "error": "nothing to say"})
                return
            self._json(200, {"ok": True})
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _stream_mic(self):
        if rb.state.get("drive_mode") != "control":
            self._send(409, "switch to control to hear the microphone", "text/plain; charset=utf-8")
            return
        queue = RELAY.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Audio-Format", "s16le;rate=16000;channels=1")
        self.end_headers()
        try:
            while rb.state.get("running", True) and rb.state.get("drive_mode") == "control":
                if queue:
                    chunk = queue.pop(0)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                else:
                    time.sleep(0.02)
        except Exception:
            pass
        finally:
            RELAY.unsubscribe(queue)


def start_panel(port=PANEL_PORT):
    load_page()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), PanelHandler)
    except OSError as exc:
        print("[PANEL] port {0} unavailable ({1})".format(port, exc))
        return None
    threading.Thread(target=server.serve_forever, name="PhonePanel", daemon=True).start()
    print("[PANEL] Open http://robot.local:{0} on a phone joined to this Wi-Fi".format(port))
    return server
