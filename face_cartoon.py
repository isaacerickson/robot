"""
face_cartoon.py - backup face used only when face_assets/ is missing.

The robot's real face is the photoreal one in face.py. This drawing is a
stand-in so a missing asset folder still shows something. It follows the
same gaze, display, and kiosk rules: eyes track gaze_x/gaze_y, the window
is the 800x480 panel, and Esc, q, or closing the window does not stop the robot.
"""

import math
import random
import time

BG         = (24, 20, 28)
SKIN       = (232, 172, 138)
SKIN_DARK  = (200, 135, 105)
SKIN_SHADE = (214, 152, 120)
CHEEK      = (240, 130, 120)
NOSE_TIP   = (238, 150, 125)
LIP        = (170, 80, 80)
MOUTH_IN   = (70, 20, 30)
TONGUE     = (215, 95, 105)
TEETH      = (245, 238, 215)
EYE_WHITE  = (250, 246, 238)
IRIS       = (95, 70, 45)
PUPIL      = (20, 14, 10)
BROW       = (85, 55, 35)
HAIR       = (90, 60, 40)
STUBBLE    = (175, 125, 100)


def run_face(state):
    import pygame
    from face import close_face_window, gaze_offsets, open_face_window, prepare_pygame, pump_kiosk_events

    prepare_pygame()
    try:
        screen = open_face_window()
        W, H = screen.get_size()
        clock = pygame.time.Clock()
        font = pygame.font.SysFont(None, max(24, H // 18))

        # Face block geometry (scales to any screen)
        fw, fh = int(W * 0.86), int(H * 0.90)
        fx, fy = (W - fw) // 2, (H - fh) // 2 + int(H * 0.02)
        cx = W // 2
        u = min(fw, fh) / 100.0            # 1 "face unit"

        rng = random.Random(7)             # fixed stubble pattern
        stubble = [(rng.uniform(-0.38, 0.38), rng.uniform(0.70, 0.93)) for _ in range(260)]
        hairs = [(rng.uniform(-0.25, 0.25), rng.uniform(-6, 6)) for _ in range(9)]

        look_x = look_y = 0.0
        next_blink = time.time() + random.uniform(2, 5)
        blink_t = -1.0
        mouth_open = mouth_goal = 0.0
        next_mouth = 0.0
        wander = (0.0, (0.0, 0.0))
        grin = 0.6

        while state["running"]:
            alive, screen = pump_kiosk_events(screen)
            if not alive:
                return
            if screen.get_size() != (W, H):
                print("[FACE] Face window size changed; reopening to fit the panel")
                return

            now      = time.time()
            speaking = state.get("speaking", False)
            human    = state.get("human_visible", False)
            alert    = now < state.get("alert_until", 0) and not human
            thinking = state.get("thinking", False)
            mood     = state.get("mood", "idle")
            jiggle   = math.sin(now * 2.2) * u * 0.4      # slow breathing wobble

            tx, ty, wander = gaze_offsets(state, now, wander)
            look_x += (tx - look_x) * 0.22
            look_y += (ty - look_y) * 0.22

            # ---------- blink ----------
            if now > next_blink and blink_t < 0:
                blink_t = now
            lid = 0.0
            if blink_t >= 0:
                p = (now - blink_t) / 0.12
                lid = 1 - abs(1 - p) if p < 2 else 0
                if p >= 2:
                    blink_t, next_blink = -1, now + random.uniform(2, 6)
            base_lid = 0.15 if (human or alert) else 0.45 if mood == "idle" else 0.3
            if alert:
                base_lid = 0.0
            lid = max(lid, base_lid)

            # ---------- mouth ----------
            if speaking:
                if now > next_mouth:
                    mouth_goal = random.choice([0.2, 0.55, 0.85, 1.0, 0.35, 0.05])
                    next_mouth = now + random.uniform(0.06, 0.14)
            else:
                mouth_goal = 0.0
            mouth_open += (mouth_goal - mouth_open) * 0.45
            grin_goal = 1.0 if human else 0.3 if alert else 0.6
            grin += (grin_goal - grin) * 0.08
            brow_lift = (6 if alert else 3 if human else 0) * u

            # ================= DRAW =================
            screen.fill(BG)
            top = fy + jiggle

            # Ears
            for s in (-1, 1):
                ex = cx + s * (fw // 2 + int(2 * u))
                pygame.draw.ellipse(screen, SKIN_DARK, (ex - 7 * u, top + fh * 0.33, 14 * u, 24 * u))
                pygame.draw.ellipse(screen, SKIN_SHADE, (ex - 4 * u, top + fh * 0.37, 8 * u, 15 * u))

            # Jowls bulging out at the bottom corners
            for s in (-1, 1):
                jx = cx + s * (fw // 2 - 8 * u)
                pygame.draw.ellipse(screen, SKIN_DARK, (jx - 16 * u, top + fh * 0.62, 32 * u, 32 * u))

            # The big block of a head, with a darker outline for fleshiness
            pygame.draw.rect(screen, SKIN_DARK, (fx - 3, top - 3, fw + 6, fh + 6), border_radius=int(14 * u))
            pygame.draw.rect(screen, SKIN, (fx, top, fw, fh), border_radius=int(13 * u))
            for s in (-1, 1):   # jowls blended in
                jx = cx + s * (fw // 2 - 8 * u)
                pygame.draw.ellipse(screen, SKIN, (jx - 14 * u, top + fh * 0.63, 28 * u, 28 * u))

            # Double chin
            pygame.draw.arc(screen, SKIN_DARK, (cx - 22 * u, top + fh * 0.84, 44 * u, 14 * u),
                            math.pi * 1.1, math.pi * 1.9, max(3, int(1.2 * u)))

            # Stubble
            for sx, sy in stubble:
                pygame.draw.circle(screen, STUBBLE, (int(cx + sx * fw), int(top + sy * fh)), max(1, int(0.35 * u)))

            # Forehead wrinkles and a few sad hairs on top
            for i in range(3):
                y = top + fh * (0.08 + i * 0.045) - brow_lift * 0.5
                pygame.draw.arc(screen, SKIN_SHADE, (cx - 20 * u + i * 2 * u, y, 40 * u - i * 4 * u, 6 * u),
                                0.2, math.pi - 0.2, max(2, int(0.6 * u)))
            for hx, lean in hairs:
                x0 = cx + hx * fw
                pygame.draw.line(screen, HAIR, (x0, top + 2), (x0 + lean * u * 0.4, top - 5 * u), max(2, int(0.5 * u)))

            # Rosy cheeks
            for s in (-1, 1):
                cs = pygame.Surface((int(26 * u), int(16 * u)), pygame.SRCALPHA)
                pygame.draw.ellipse(cs, (*CHEEK, int(90 + 60 * grin)), cs.get_rect())
                screen.blit(cs, (cx + s * 30 * u - 13 * u, top + fh * 0.53 + (-2 * u * grin)))

            # Eyes: small and beady, pushed up by the cheeks when grinning
            eye_y = top + fh * 0.36 - grin * 1.5 * u
            ew, eh = 15 * u, 10 * u * (1.25 if alert else 1.0)
            for s in (-1, 1):
                ex = cx + s * 22 * u
                # baggy under-eyes
                pygame.draw.arc(screen, SKIN_SHADE, (ex - ew * 0.6, eye_y + eh * 0.3, ew * 1.2, eh * 0.9),
                                math.pi * 1.15, math.pi * 1.85, max(2, int(0.6 * u)))
                rect = pygame.Rect(ex - ew / 2, eye_y - eh / 2, ew, eh)
                pygame.draw.ellipse(screen, EYE_WHITE, rect)
                ix = ex + look_x * ew * 0.25
                iy = eye_y + look_y * eh * 0.25
                pygame.draw.circle(screen, IRIS, (ix, iy), eh * 0.42)
                pygame.draw.circle(screen, PUPIL, (ix, iy), eh * 0.22)
                pygame.draw.circle(screen, (255, 255, 255), (ix - eh * 0.12, iy - eh * 0.14), max(2, eh * 0.08))
                # heavy upper lid
                if lid > 0:
                    pygame.draw.rect(screen, SKIN_DARK, (rect.x - 2, rect.y - 2, rect.w + 4, rect.h * lid + 2))
                pygame.draw.ellipse(screen, SKIN_DARK, rect, max(2, int(0.6 * u)))

                # Thick, bushy brows
                by = eye_y - eh * 1.05 - brow_lift - (2 * u if thinking and s == 1 else 0)
                tilt = 0  # flat, friendly brows
                pygame.draw.line(screen, BROW, (ex - ew * 0.65, by + tilt), (ex + ew * 0.65, by - tilt), max(5, int(3 * u)))

            # Big bulbous nose
            ny = top + fh * 0.50
            pygame.draw.ellipse(screen, SKIN_SHADE, (cx - 5 * u, ny - 15 * u, 10 * u, 18 * u))
            pygame.draw.ellipse(screen, NOSE_TIP, (cx - 11 * u, ny - 3 * u, 22 * u, 15 * u))
            for s in (-1, 1):
                pygame.draw.ellipse(screen, SKIN_DARK, (cx + s * 8 * u - 5 * u, ny + 1 * u, 10 * u, 10 * u))
                pygame.draw.ellipse(screen, (120, 60, 55), (cx + s * 5 * u - 2 * u, ny + 7 * u, 4 * u, 3 * u))
            pygame.draw.circle(screen, (250, 200, 175), (cx - 3 * u, ny + 1 * u), max(2, 1.6 * u))

            # Mouth: wide, a little crooked
            my = top + fh * 0.72
            mw = 46 * u + 6 * u * grin
            shift = 4 * u if thinking else 0
            if mouth_open > 0.06:
                mh = 4 * u + 20 * u * mouth_open
                mrect = pygame.Rect(cx - mw / 2 + shift, my - mh * 0.35, mw, mh)
                pygame.draw.ellipse(screen, LIP, mrect.inflate(2 * u, 2 * u))
                pygame.draw.ellipse(screen, MOUTH_IN, mrect)
                # tongue
                pygame.draw.ellipse(screen, TONGUE, (mrect.centerx - mw * 0.25, mrect.bottom - mh * 0.45,
                                                     mw * 0.5, mh * 0.5))
                # top teeth (one slightly off)
                tw = mw / 8
                for i in range(-2, 2):
                    tx0 = mrect.centerx + i * tw
                    drop = 0.8 * u if i == 1 else 0
                    pygame.draw.rect(screen, TEETH, (tx0 + 1, mrect.top + mh * 0.05, tw - 2, min(5 * u, mh * 0.35) + drop),
                                     border_radius=int(u))
            else:
                # closed grin: thick lip arc, curving up more when happy
                depth = 6 * u + 12 * u * grin
                arc = pygame.Rect(cx - mw / 2 + shift, my - depth, mw, depth * 2)
                pygame.draw.arc(screen, LIP, arc, math.pi * 1.05, math.pi * 1.95, max(6, int(2.4 * u)))
                for s in (-1, 1):   # smile creases
                    pygame.draw.arc(screen, SKIN_SHADE, (cx + s * mw * 0.55 - 4 * u + shift, my - 8 * u, 8 * u, 14 * u),
                                    math.pi * (0.6 if s < 0 else 1.6), math.pi * (1.4 if s < 0 else 2.4), max(2, int(0.7 * u)))

            if not state.get("online", True):
                txt = font.render("no internet - reconnecting...", True, (255, 200, 90))
                screen.blit(txt, (20, H - txt.get_height() - 12))

            try:
                pygame.display.flip()
            except pygame.error as e:
                print(f"[FACE] Display error ({e}); reopening")
                return
            clock.tick(30)
    finally:
        close_face_window()
