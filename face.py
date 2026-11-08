"""
face.py - photorealistic talking face for the robot's 800x480 panel.

The picture is a wide, fleshy face packed into the rectangle: cheeks and
jaw meet the left and right edges, and the art is scaled to the window
with no empty border. Assets are 1280x720. A source crop narrower than
the panel is smoothscaled to whatever size the window actually is, so on
the 800x480 DSI the face reads wider than a plain 16:9 fit.

Reads the shared `state` dict from robot_brain.py:
  speaking       -> mouth moves
  gaze_x/gaze_y  -> eyes look at a face in the camera (0..1), else idle
  thinking       -> eyes glance up when nothing else is there to look at
  online         -> small "no internet" note when offline
If face_assets/ is missing, falls back to the cartoon face.

This is a kiosk. Esc, q, and closing the window do not stop the robot.
Only a minimized window is put fullscreen again. Focus loss is ignored:
calling set_mode on every focus loss fought the Pi control centre and
turned the panel black. KeyboardInterrupt is handled in robot_brain.

SDL audio is forced to the dummy driver before pygame.init(), and the
mixer is shut down right after, so pygame does not hold the USB speakers.
Speech itself goes out the ALSA card named Device (the Unitek adapter).
"""

import json
import math
import os
import random
import time

HERE   = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "face_assets")
SRC_W, SRC_H = 1280, 720

# Artboard rectangle stretched onto the whole panel. It is narrower than
# an 800x480 frame, so horizontal scale runs ahead of vertical scale and
# the face is packed wider. Full height keeps the open mouth on screen.
# Cheeks at x=180 and x=1100 become the side edges. Corners stay skin.
FACE_CROP = (180, 0, 1100, 720)

_PANEL_SIZES = ((800, 480), (480, 800))


def choose_face_panel(sizes):
    """Pick the robot face display.

    sizes is a list of (width, height). Prefer the 800x480 DSI panel
    (or 480x800). Otherwise use the smallest display. Returns
    (index, (width, height)).
    """
    cleaned = []
    for wh in sizes or []:
        if not wh or len(wh) < 2:
            continue
        cleaned.append((int(wh[0]), int(wh[1])))
    if not cleaned:
        return 0, (800, 480)
    for i, wh in enumerate(cleaned):
        if wh in _PANEL_SIZES:
            return i, wh
    i = min(range(len(cleaned)), key=lambda n: cleaned[n][0] * cleaned[n][1])
    return i, cleaned[i]


def face_art_scale(window_w, window_h, crop=FACE_CROP):
    """Pixels of window per artboard pixel, for the crop stretched to the window."""
    left, top, right, bottom = crop
    src_w = max(1, right - left)
    src_h = max(1, bottom - top)
    return window_w / float(src_w), window_h / float(src_h)


def map_art(x, y, window_w, window_h, crop=FACE_CROP):
    """Map an artboard point into window pixels. Crop edges land on the window edges."""
    left, top, right, bottom = crop
    sx, sy = face_art_scale(window_w, window_h, crop)
    return (x - left) * sx, (y - top) * sy


def gaze_offsets(state, now, wander):
    """Where the eyes should look, as (tx, ty, wander) with each axis in -1..1.

    Camera-frame gaze wins over everything else, including the thinking
    glance. tx -1 is screen-left. Image-right maps to screen-left, matching
    the neck (a target on the right of the picture lowers the servo angle,
    and the old face treated that as the viewer's left). Image-down is
    positive ty. wander is (wander_t, look_goal) for the idle look.
    """
    gx = state.get("gaze_x")
    gy = state.get("gaze_y")
    if gx is not None and gy is not None:
        tx = max(-1.0, min(1.0, (0.5 - float(gx)) * 2.0))
        ty = max(-1.0, min(1.0, (float(gy) - 0.5) * 2.0))
        return tx, ty, wander
    if state.get("thinking"):
        return 0.7, -0.5, wander
    wander_t, look_goal = wander
    if state.get("mood") == "searching":
        return math.sin(now * 1.3), 0.0, wander
    if now > wander_t:
        wander_t = now + random.uniform(1.6, 4.0)
        look_goal = random.choice([
            (0.0, 0.0), (0.0, 0.0),
            (-0.35, 0.08), (0.35, -0.08),
            (0.15, -0.2), (-0.15, 0.12),
        ])
    return look_goal[0], look_goal[1], (wander_t, look_goal)


# Only a minimize should rebuild the fullscreen window. WINDOWFOCUSLOST
# fires while Raspberry Pi Control Centre is open, and set_mode on that
# event fought rpcc until the DSI panel went black.
RESTORE_WINDOW_EVENTS = ("WINDOWMINIMIZED",)

_face_display = {
    "index": 0,
    "size": (800, 480),
    "last_restore": 0.0,
}


def silence_pygame_audio():
    """Stop SDL from opening the USB speakers. Call before pygame.init().

    pygame.init() starts the mixer. On the Pi that opens the USB speakers
    and holds them, so espeak/aplay cannot play. The dummy driver never
    opens that card. Speech is routed separately, in start_robot.sh, to
    ALSA card "Device" (plughw:Device,0). Focus loss must not iconify the
    face, and it must not call set_mode either.
    """
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"] = "0"


def release_pygame_mixer():
    """Drop SDL audio after init so speech can use the real speakers."""
    import pygame
    try:
        was = pygame.mixer.get_init()
    except Exception:
        was = None
    try:
        pygame.mixer.quit()
    except Exception as e:
        print("[FACE] Could not release SDL audio ({0})".format(e))
        return
    if was:
        print("[FACE] Released SDL mixer {0} so speech can use the speakers".format(was))


def parse_wlr_outputs(text):
    """Parse `wlr-randr` text into output dicts.

    Header lines are not indented: `DSI-2 "..."` or `NOOP-1 "Headless output 2"`.
    Mode lines look like `800x480 px, 60 Hz`.
    """
    outputs = []
    current = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[:1].isspace():
            name, _, rest = raw.strip().partition(" ")
            description = rest.strip().strip('"')
            label = (name + " " + description).lower()
            current = {
                "name": name,
                "description": description,
                "modes": [],
                "headless": name.upper().startswith("NOOP") or "headless" in label,
            }
            outputs.append(current)
            continue
        if current is None:
            continue
        token = raw.strip().split()[0]
        width, _, height = token.partition("x")
        if width.isdigit() and height.isdigit() and "px" in raw:
            current["modes"].append((int(width), int(height)))
    return outputs


def display_commands(outputs):
    """Turn headless NOOP outputs off and enable the 800x480 DSI panel.

    labwc was advertising a 1920x1080 headless output at 0,0 as well as the
    real DSI panel. pygame only saw 1920x1080, so the face never appeared.
    """
    commands = []
    chosen = None
    chosen_mode = None
    for output in outputs:
        if output.get("headless"):
            commands.append(["wlr-randr", "--output", output["name"], "--off"])
            continue
        modes = [tuple(mode) for mode in output.get("modes") or []]
        panel_mode = next((mode for mode in modes if mode in _PANEL_SIZES), None)
        name = output.get("name") or ""
        if panel_mode is not None or name.upper().startswith("DSI"):
            if chosen is None or (panel_mode is not None and chosen_mode not in _PANEL_SIZES):
                chosen = output
                chosen_mode = panel_mode
    if chosen is not None:
        command = ["wlr-randr", "--output", chosen["name"], "--on", "--pos", "0,0"]
        if chosen_mode:
            command.extend(["--mode", "{0}x{1}".format(*chosen_mode)])
        commands.append(command)
    return commands


def configure_dsi_panel():
    """Enable the DSI face panel and switch off headless outputs before SDL starts."""
    import subprocess
    try:
        proc = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=3)
    except Exception as exc:
        print("[FACE] wlr-randr unavailable ({0})".format(exc))
        return
    text = proc.stdout or ""
    if proc.returncode != 0 and not text:
        print("[FACE] wlr-randr exited {0}".format(proc.returncode))
        return
    outputs = parse_wlr_outputs(text)
    for output in outputs:
        modes = ", ".join("{0}x{1}".format(*mode) for mode in output["modes"]) or "none"
        kind = "headless" if output["headless"] else "output"
        print("[FACE] {0} {1} modes {2}".format(kind, output["name"], modes))
    for command in display_commands(outputs):
        print("[FACE] {0}".format(" ".join(command)))
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=3)
        except Exception as exc:
            print("[FACE] display command failed ({0})".format(exc))


def prepare_pygame():
    """Init pygame without taking the sound card.

    The DSI panel is enabled first. SDL was only seeing labwc's headless
    1920x1080 output, so the face never landed on the 800x480 screen.
    """
    configure_dsi_panel()
    silence_pygame_audio()
    import pygame
    pygame.init()
    release_pygame_mixer()
    try:
        pygame.mouse.set_visible(False)
    except Exception:
        pass
    return pygame


def face_event_should_restore(event_type, constants):
    """Return the event name if this event hid the face, else None.

    constants maps pygame event names to their integer types.
    """
    for name in RESTORE_WINDOW_EVENTS:
        code = constants.get(name)
        if code is not None and event_type == code:
            return name
    return None


def _window_constants():
    import pygame
    found = {}
    for name in RESTORE_WINDOW_EVENTS:
        if hasattr(pygame, name):
            found[name] = getattr(pygame, name)
    return found


def _set_fullscreen(index, w, h):
    import pygame
    flags = pygame.FULLSCREEN
    try:
        screen = pygame.display.set_mode((w, h), flags, display=index)
    except TypeError:
        print("[FACE] This pygame has no display index; fullscreen on the default display")
        screen = pygame.display.set_mode((w, h), flags)
    try:
        pygame.display.set_caption("Robot face")
        pygame.mouse.set_visible(False)
    except Exception:
        pass
    release_pygame_mixer()
    return screen


def open_face_window():
    """Fullscreen on the 800x480 panel. Logs every display and the index used."""
    import pygame
    if not pygame.get_init():
        prepare_pygame()
    elif not pygame.display.get_init():
        pygame.display.init()
    sizes = []
    try:
        sizes = list(pygame.display.get_desktop_sizes() or [])
    except Exception as e:
        print(f"[FACE] Could not list displays ({e})")
    if not sizes:
        print("[FACE] No displays reported")
    for i, wh in enumerate(sizes):
        print("[FACE] display {0}: {1}x{2}".format(i, int(wh[0]), int(wh[1])))
    index, (w, h) = choose_face_panel(sizes)
    print("[FACE] Chose display {0} ({1}x{2}) for the face".format(index, w, h))
    _face_display["index"] = index
    _face_display["size"] = (w, h)
    _face_display["last_restore"] = time.time()
    screen = _set_fullscreen(index, w, h)
    print("[FACE] Face window {0}x{1} on display {2}".format(
        screen.get_width(), screen.get_height(), index))
    return screen


def restore_face_fullscreen(reason):
    """Put the face fullscreen again. Returns the new screen, or None if skipped.

    A second set_mode inside about a second is ignored so a burst of
    minimize events cannot flash the panel black.
    """
    now = time.time()
    if now - _face_display.get("last_restore", 0.0) < 1.0:
        return None
    index = _face_display["index"]
    w, h = _face_display["size"]
    _face_display["last_restore"] = now
    print("[FACE] Restoring fullscreen on display {0} ({1}x{2}) after {3}".format(
        index, w, h, reason))
    try:
        return _set_fullscreen(index, w, h)
    except Exception as e:
        print("[FACE] Could not restore fullscreen ({0})".format(e))
        return None


def pump_kiosk_events(screen):
    """Eat window events. Esc, q, and close do not stop the robot.

    Returns (keep_going, screen). keep_going is False when the window has
    gone away and the caller should reopen it. A minimized face is put
    fullscreen again. Focus loss is left alone so the window stays stable.
    """
    import pygame
    constants = _window_constants()
    restore_reason = None
    for ev in pygame.event.get():
        if ev.type == pygame.QUIT:
            print("[FACE] Window close ignored; reopening the face")
            return False, screen
        close_type = getattr(pygame, "WINDOWCLOSE", None)
        if close_type is not None and ev.type == close_type:
            print("[FACE] Window close ignored; reopening the face")
            return False, screen
        if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q):
            continue
        reason = face_event_should_restore(ev.type, constants)
        if reason:
            restore_reason = reason
    if restore_reason:
        restored = restore_face_fullscreen(restore_reason)
        if restored is not None:
            screen = restored
    return True, screen


def close_face_window():
    import pygame
    try:
        pygame.display.quit()
    except Exception:
        pass
    try:
        pygame.quit()
    except Exception:
        pass


def run_face(state):
    if not os.path.exists(os.path.join(ASSETS, "face.json")):
        from face_cartoon import run_face as cartoon
        return cartoon(state)

    prepare_pygame()
    import pygame
    try:
        screen = open_face_window()
        W, H = screen.get_size()
        clock = pygame.time.Clock()
        font = pygame.font.SysFont(None, max(24, H // 18))

        raw_base = pygame.image.load(os.path.join(ASSETS, "base.jpg")).convert()
        crop = FACE_CROP
        iw, ih = raw_base.get_size()
        left, top, right, bottom = crop
        if left < 0 or top < 0 or right > iw or bottom > ih or right <= left or bottom <= top:
            print("[FACE] Crop {0} does not fit {1}x{2}; using the full picture".format(crop, iw, ih))
            crop = (0, 0, iw, ih)
            left, top, right, bottom = crop
        sx, sy = face_art_scale(W, H, crop)
        print("[FACE] Framing crop {0} scaled by {1:.3f} x {2:.3f} onto {3}x{4}".format(
            crop, sx, sy, W, H))
        base = pygame.transform.smoothscale(
            raw_base.subsurface((left, top, right - left, bottom - top)), (W, H))

        def load(name, alpha=True):
            img = pygame.image.load(os.path.join(ASSETS, name))
            img = img.convert_alpha() if alpha else img.convert()
            w, h = img.get_size()
            return pygame.transform.smoothscale(img, (max(1, round(w * sx)), max(1, round(h * sy))))

        def art_pt(x, y):
            return (round((x - left) * sx), round((y - top) * sy))

        def art_d(dx, dy):
            return (round(dx * sx), round(dy * sy))

        info = json.load(open(os.path.join(ASSETS, "face.json")))
        eyes = []
        for i, e in enumerate(info["eyes"]):
            bx0, by0, bx1, by1 = e["mask_box"]
            eyes.append({
                "iris":  load(f"iris{i}.png"),
                "mask":  load(f"eyemask{i}.png"),
                "pos":   art_pt(bx0, by0),
                "iris_off": art_d(e["iris_pos"][0] - bx0, e["iris_pos"][1] - by0),
                "shift": e["max_shift"] * sx,
            })
        layers = {}
        for name, (x0, y0, x1, y1) in info["layers"].items():
            layers[name] = (load(name + ".png"), art_pt(x0, y0))

        def blend(name, amount):
            if amount <= 0.02:
                return
            img, (x, y) = layers[name]
            img.set_alpha(int(255 * min(1.0, amount)))
            screen.blit(img, (x, y))

        look_x = look_y = 0.0
        wander = (0.0, (0.0, 0.0))
        next_blink = time.time() + random.uniform(2, 5)
        blink_t    = -1.0
        mouth = mouth_goal = 0.0
        next_mouth = 0.0

        while state["running"]:
            alive, screen = pump_kiosk_events(screen)
            if not alive:
                return
            if screen.get_size() != (W, H):
                print("[FACE] Face window size changed; reopening to fit the panel")
                return

            now      = time.time()
            speaking = state.get("speaking", False)
            tx, ty, wander = gaze_offsets(state, now, wander)
            # Smooth, including small motions. No deadband, so a modest
            # shift in the camera still moves the eyes.
            look_x += (tx - look_x) * 0.22
            look_y += (ty - look_y) * 0.22

            # ---- blink ----
            if now > next_blink and blink_t < 0:
                blink_t = now
            blink = 0.0
            if blink_t >= 0:
                p = (now - blink_t) / 0.08
                blink = 1 - abs(1 - p) if p < 2 else 0.0
                if p >= 2:
                    blink_t = -1
                    next_blink = now + (0.25 if random.random() < 0.15 else random.uniform(2.0, 6.0))

            # ---- mouth ----
            if speaking:
                if now > next_mouth:
                    mouth_goal = random.choice([0.1, 0.4, 0.7, 1.0, 1.0, 0.55])
                    next_mouth = now + random.uniform(0.07, 0.15)
            else:
                mouth_goal = 0.0
            mouth += (mouth_goal - mouth) * 0.5

            # The face is glued to the edges. A breathing shift would flash
            # the desktop along the top, so the picture does not bob.
            screen.blit(base, (0, 0))
            for e in eyes:
                mw, mh = e["mask"].get_size()
                roi = pygame.Surface((mw, mh), pygame.SRCALPHA)
                ix = e["iris_off"][0] + look_x * e["shift"]
                iy = e["iris_off"][1] + look_y * e["shift"] * 0.35
                roi.blit(e["iris"], (round(ix), round(iy)))
                roi.blit(e["mask"], (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
                screen.blit(roi, e["pos"])
            blend("blink", min(1.0, blink * 2.5))
            blend("talk_mid", min(1.0, mouth * 2))
            blend("talk_open", (mouth - 0.5) * 2)

            if not state.get("online", True):
                txt = font.render("no internet - reconnecting...", True, (255, 220, 120))
                screen.blit(txt, (20, H - txt.get_height() - 12))

            try:
                pygame.display.flip()
            except pygame.error as e:
                print(f"[FACE] Display error ({e}); reopening")
                return
            clock.tick(30)
    finally:
        close_face_window()
