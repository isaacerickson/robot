"""
face.py - photorealistic, wide, block-shaped talking face for the robot's
screen (pygame, fullscreen). The face is stretched to fill the whole screen,
corner to corner.

Real photo layers from face_assets/:
  base.jpg      the face with the irises removed (just eye whites)
  iris0/1.png   his real irises, slid around inside each eye to look around
  eyemask0/1    the eye openings, so the irises never slide over the lids
  blink.png     eyes closed
  talk_mid/open mouth half open / wide open, blended while speaking

Reads the shared `state` dict from robot_brain.py:
  speaking       -> mouth moves
  human_visible  -> eyes follow the person (via servo_angle)
  alert_until    -> eyes snap toward the motion
  thinking       -> eyes glance up and to the side while waiting on Grok
  online         -> small "no internet" note when offline
If face_assets/ is missing, falls back to the cartoon face.
Press Esc or q to quit (stops the whole robot).
"""

import json
import math
import os
import random
import time
import pygame

HERE   = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "face_assets")
SRC_W, SRC_H = 1280, 720


def run_face(state):
    if not os.path.exists(os.path.join(ASSETS, "face.json")):
        from face_cartoon import run_face as cartoon
        return cartoon(state)

    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    W, H = screen.get_size()
    sx, sy = W / SRC_W, H / SRC_H          # stretch to fill every corner
    clock = pygame.time.Clock()
    font = pygame.font.SysFont(None, max(24, H // 18))

    def load(name, alpha=True):
        img = pygame.image.load(os.path.join(ASSETS, name))
        img = img.convert_alpha() if alpha else img.convert()
        w, h = img.get_size()
        return pygame.transform.smoothscale(img, (max(1, round(w * sx)), max(1, round(h * sy))))

    def pt(x, y):
        return (round(x * sx), round(y * sy))

    info = json.load(open(os.path.join(ASSETS, "face.json")))
    base = load("base.jpg", alpha=False)
    eyes = []
    for i, e in enumerate(info["eyes"]):
        bx0, by0, bx1, by1 = e["mask_box"]
        eyes.append({
            "iris":  load(f"iris{i}.png"),
            "mask":  load(f"eyemask{i}.png"),
            "pos":   pt(bx0, by0),
            "iris_off": pt(e["iris_pos"][0] - bx0, e["iris_pos"][1] - by0),
            "shift": e["max_shift"] * sx,
        })
    layers = {}
    for name, (x0, y0, x1, y1) in info["layers"].items():
        layers[name] = (load(name + ".png"), pt(x0, y0))

    def blend(name, amount, dy=0):
        if amount <= 0.02:
            return
        img, (x, y) = layers[name]
        img.set_alpha(int(255 * min(1.0, amount)))
        screen.blit(img, (x, y + dy))

    look_x = look_y = 0.0
    look_goal = (0.0, 0.0)
    wander_t   = 0.0
    next_blink = time.time() + random.uniform(2, 5)
    blink_t    = -1.0
    mouth = mouth_goal = 0.0
    next_mouth = 0.0

    while state["running"]:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (
                    ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q)):
                state["running"] = False

        now      = time.time()
        speaking = state.get("speaking", False)
        human    = state.get("human_visible", False)
        alert    = now < state.get("alert_until", 0)
        thinking = state.get("thinking", False)
        mood     = state.get("mood", "idle")

        # ---- where to look: x -1 = screen-left, +1 = screen-right ----
        # servo < 90 means the person is on the robot's right = viewer's left.
        servo = state.get("servo_angle", 90.0)
        if thinking:
            tx, ty = 0.7, -0.5
        elif human or alert:
            tx = max(-1.0, min(1.0, (servo - 90.0) / 35.0))
            tx, ty = (0.0 if abs(tx) < 0.2 else tx), 0.0
        elif mood == "searching":
            tx, ty = math.sin(now * 1.3), 0.0
        else:
            if now > wander_t:
                wander_t = now + random.uniform(1.2, 4.0)
                look_goal = random.choice([(0, 0), (0, 0), (-0.8, 0), (0.8, 0), (0.5, -0.4), (-0.5, 0.2)])
            tx, ty = look_goal
        # quick saccade-like eye movement
        look_x += (tx - look_x) * 0.35
        look_y += (ty - look_y) * 0.35

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

        # ---- draw ----
        dy = int(math.sin(now * 1.8) * 2)          # gentle breathing bob
        screen.blit(base, (0, dy))
        for e in eyes:
            mw, mh = e["mask"].get_size()
            roi = pygame.Surface((mw, mh), pygame.SRCALPHA)
            ix = e["iris_off"][0] + look_x * e["shift"]
            iy = e["iris_off"][1] + look_y * e["shift"] * 0.35
            roi.blit(e["iris"], (round(ix), round(iy)))
            roi.blit(e["mask"], (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            screen.blit(roi, (e["pos"][0], e["pos"][1] + dy))
        blend("blink", min(1.0, blink * 2.5), dy)     # snap shut, no ghosting
        blend("talk_mid", min(1.0, mouth * 2), dy)
        blend("talk_open", (mouth - 0.5) * 2, dy)

        if not state.get("online", True):
            txt = font.render("no internet - reconnecting...", True, (255, 220, 120))
            screen.blit(txt, (20, H - txt.get_height() - 12))

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()
