import argparse
import math
import time
import random
import subprocess
import sys
import threading
import base64
import os
import io
import re
import signal
import concurrent.futures
from typing import Optional

import mss
import pyperclip
from PIL import Image

import objc
import pyautogui
from dotenv import load_dotenv
import anthropic

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBorderlessWindowMask,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableDictionary,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Foundation import NSObject, NSMakeRect, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration

load_dotenv()

# ─────────────────────────────────────────────────────────────
# Recording (optional — enabled with --record flag)
# ─────────────────────────────────────────────────────────────

from workflow_recorder import WorkflowRecorder, next_recording_id  # noqa: E402

_recorder: Optional[WorkflowRecorder] = None
_record_enabled: bool = False


def _finalize_recording(summary: str) -> None:
    """Write log.json, report.md, video (if ffmpeg). Safe to call multiple times."""
    global _recorder
    if _recorder is None:
        return
    try:
        _recorder.stop(summary=summary)
    except Exception as exc:
        print(f"[recorder] finalize failed: {exc}", file=sys.stderr)


# Friendly recording folder names for main study tasks (others use the task number)
_RECORDING_NAMES: dict[str, str] = {
    "1": "t1", "2": "t2", "3": "t3",
    "4": "s1", "5": "s2", "6": "s3",
}

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

TRAIL_FADE_SEC     = 2.5
PREVIEW_SEC        = 0.20
FPS                = 60
TYPE_CHAR_INTERVAL = 0.055

NSEventMaskKeyDown = 1024

state = {
    "trail":              [],
    "preview_path":       None,
    "preview_target":     None,
    "preview_label":      None,
    "preview_start_ts":   None,
    "cursor_pos":         (0, 0),
    "running_demo":       True,
    "screenshot_action":  False,
    "vignette_alpha":     0.0,
    "vignette_target":    0.0,
    "session_clicks":     [],        # accumulated (x, y, ts) for all clicks in session
    "action_count":       0,         # total actions taken (for tempo acceleration)
    "scroll_count":       0,         # consecutive scrolls (for tempo acceleration)
}

state_lock   = threading.Lock()
overlay_view = None

screen   = NSScreen.mainScreen()
frame    = screen.frame()
SCREEN_W = int(frame.size.width)
SCREEN_H = int(frame.size.height)

PANEL_W   = int(SCREEN_W * 0.22)
OVERLAY_W = SCREEN_W - PANEL_W


def _set_panel_visibility(show_panel: bool):
    """Resize overlay vs reasoning strip. Call before build_window / build_chat_panel."""
    global PANEL_W, OVERLAY_W
    if show_panel:
        PANEL_W = int(SCREEN_W * 0.22)
        OVERLAY_W = SCREEN_W - PANEL_W
    else:
        PANEL_W = 0
        OVERLAY_W = SCREEN_W


_chat_webview = None
_chat_window  = None

def push_chat_message(role: str, text: str):
    if _chat_webview is None:
        return
    import time as _time
    ts = _time.strftime("%H:%M:%S")
    safe = text.replace("\\", "\\\\").replace("`", "\\`").replace("\n", "\\n")
    _chat_webview.evaluateJavaScript_completionHandler_(f"addMsg(`{role}`, `{safe}`, `{ts}`);", None)

DISPLAY_W = 1280
DISPLAY_H = 720
COORD_SX  = SCREEN_W / DISPLAY_W
COORD_SY  = SCREEN_H / DISPLAY_H

pyautogui.FAILSAFE = False
pyautogui.PAUSE    = 0.0

# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────

def now():
    return time.time()

def mouse_pos():
    return pyautogui.position()

def to_cocoa(x, y):
    return x, SCREEN_H - y

def sc(coord):
    x, y = coord
    return int(x * COORD_SX), int(y * COORD_SY)

def lerp(a, b, t):
    return a + (b - a) * t

def ease_out_cubic(t):
    return 1.0 - (1.0 - t) ** 3

def ease_in_out_sine(t):
    return -(math.cos(math.pi * t) - 1.0) / 2.0

def bezier_path(x0, y0, x1, y1, n=80):
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 5:
        return [(x0, y0), (x1, y1)]
    px, py = -dy / dist, dx / dist
    spread = min(dist * 0.15, 130)
    o1 = random.uniform(-spread, spread)
    o2 = o1 * random.uniform(0.15, 0.55)
    c1 = (x0 + dx * 0.28 + px * o1, y0 + dy * 0.28 + py * o1)
    c2 = (x0 + dx * 0.74 + px * o2, y0 + dy * 0.74 + py * o2)
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        x = u**3*x0 + 3*u*u*t*c1[0] + 3*u*t*t*c2[0] + t**3*x1
        y = u**3*y0 + 3*u*u*t*c1[1] + 3*u*t*t*c2[1] + t**3*y1
        pts.append((x, y))
    return pts

# ─────────────────────────────────────────────────────────────
# Context helpers
# ─────────────────────────────────────────────────────────────

def strip_old_screenshots(messages: list, keep_recent: int = 3) -> list:
    """Return a copy of messages keeping only the most recent `keep_recent`
    tool_result screenshots; older ones are replaced with a text placeholder."""
    tr_indices = [
        i for i, m in enumerate(messages)
        if m["role"] == "user" and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result"
                for b in m["content"])
    ]
    keep = set(tr_indices[-keep_recent:])

    result = []
    for i, m in enumerate(messages):
        if m["role"] == "user" and isinstance(m.get("content"), list):
            new_content = []
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "image":
                    if i not in keep:
                        continue  # strip initial screenshot from old turns
                elif isinstance(b, dict) and b.get("type") == "tool_result":
                    if i not in keep:
                        new_content.append({
                            "type": "tool_result",
                            "tool_use_id": b["tool_use_id"],
                            "content": "[screenshot omitted]",
                        })
                    else:
                        new_content.append(b)
                    continue
                new_content.append(b)
            result.append({**m, "content": new_content})
        else:
            result.append(m)
    return result


# ─────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────

def set_preview(x, y, label=None):
    sx, sy = mouse_pos()
    path = bezier_path(sx, sy, x, y)
    with state_lock:
        state["preview_path"]     = path
        state["preview_target"]   = (x, y)
        state["preview_label"]    = label
        state["preview_start_ts"] = now()

def clear_preview():
    with state_lock:
        state["preview_path"]     = None
        state["preview_target"]   = None
        state["preview_label"]    = None
        state["preview_start_ts"] = None

# ─────────────────────────────────────────────────────────────
# Human-like movement (organic, with overshoot + ease)
# ─────────────────────────────────────────────────────────────

def human_move_to(x, y, speed_factor=1.0):
    """Abrupt direct move — no bezier, no easing."""
    pyautogui.moveTo(x, y, duration=0)


def human_type_visible(text, target_pos=None):
    """Type text directly at full speed."""
    pyautogui.write(text, interval=0.02)
    time.sleep(0.05)

# ─────────────────────────────────────────────────────────────
# Action helpers
# ─────────────────────────────────────────────────────────────

def click_with_preview(x, y, label=None, double=False, speed_factor=1.0):
    pyautogui.moveTo(x, y, duration=0)
    if double:
        pyautogui.doubleClick()
    else:
        pyautogui.click()
    with state_lock:
        state["session_clicks"].append((x, y, now()))

def scroll_action(x, y, dy, speed_factor=1.0):
    """Scroll abruptly."""
    pyautogui.moveTo(x, y, duration=0)
    direction = -1 if dy > 0 else 1
    total_clicks = max(5, min(abs(dy) * 3, 30))
    bursts = 1
    clicks_per_burst = total_clicks

    for b in range(bursts):
        for _ in range(clicks_per_burst):
            pyautogui.scroll(direction)
        # Pause between bursts shrinks with speed_factor
        time.sleep(random.uniform(0.03, 0.08) / max(speed_factor, 0.3))

# ─────────────────────────────────────────────────────────────
# Trail sampling
# ─────────────────────────────────────────────────────────────

def sample_mouse_loop():
    while True:
        x, y = mouse_pos()
        t = now()
        with state_lock:
            state["cursor_pos"] = (x, y)
            trail = state["trail"]
            if trail:
                px, py, _ = trail[-1]
                if math.hypot(x - px, y - py) >= 1.5:
                    trail.append((x, y, t))
            else:
                trail.append((x, y, t))
            cutoff = t - TRAIL_FADE_SEC
            state["trail"] = [(a, b, ts) for (a, b, ts) in trail if ts >= cutoff]
            # Smooth vignette
            state["vignette_alpha"] = lerp(state["vignette_alpha"], state["vignette_target"], 0.08)
        time.sleep(1 / FPS)

# ─────────────────────────────────────────────────────────────
# Screenshot
# ─────────────────────────────────────────────────────────────

def screenshot_base64():
    with state_lock:
        state["vignette_target"] = 1.0
    try:
        with mss.mss() as sct:
            time.sleep(0.05)
            raw = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img = img.resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return base64.b64encode(buf.getvalue()).decode()
    finally:
        with state_lock:
            state["screenshot_action"] = False
            state["vignette_target"]   = 0.0

# ─────────────────────────────────────────────────────────────
# DOM helpers (non-legibility)
# ─────────────────────────────────────────────────────────────

def dom_mark_copied():
    """Style the currently selected/copied text to show 'agent kept this'.
    Finds the selection anchor element and applies a persistent tint."""
    js = (
        "(function(){"
        "var sel=window.getSelection();"
        "if(!sel||sel.rangeCount===0)return 'no selection';"
        "var range=sel.getRangeAt(0);"
        "var el=range.startContainer;"
        "if(el.nodeType===3)el=el.parentElement;"
        "var walk=el;"
        "for(var i=0;i<6;i++){"
        "if(!walk)break;"
        "walk.style.transition='background-color 0.4s ease,border-left 0.3s ease,color 0.3s ease';"
        "walk.style.backgroundColor='rgba(60,200,255,0.08)';"
        "walk.style.borderLeft='3px solid rgba(60,200,255,0.5)';"
        "walk.setAttribute('data-cr-kept','1');"
        "var next=walk.nextElementSibling;"
        "if(!next)break;"
        "var r2=next.getBoundingClientRect();"
        "var selRect=range.getBoundingClientRect();"
        "if(r2.top>selRect.bottom+20)break;"
        "walk=next;"
        "}"
        "return 'marked';"
        "})()"
    )
    safe = js.replace('\\', '\\\\').replace('"', '\\"')
    apple = (
        'tell application "Google Chrome"\n'
        '    tell active tab of front window\n'
        f'        execute javascript "{safe}"\n'
        '    end tell\n'
        'end tell'
    )
    try:
        subprocess.Popen(
            ["osascript", "-e", apple],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

def dom_scroll_scan(ms=1000):
    """During scroll: briefly highlight headings/links in viewport
    to show what the agent is scanning for."""
    js = (
        "(function(){"
        "var els=document.querySelectorAll('h1,h2,h3,a[href],button,nav a,[role=button]');"
        "var vh=window.innerHeight;"
        "var vis=[];"
        "els.forEach(function(el){"
        "  var r=el.getBoundingClientRect();"
        "  if(r.top>0&&r.top<vh&&r.height>5&&vis.length<8)vis.push(el);"
        "});"
        "vis.forEach(function(el,i){"
        "  setTimeout(function(){"
        "    el.style.transition='background-color 0.15s ease';"
        "    el.style.backgroundColor='rgba(60,200,255,0.10)';"
        "    setTimeout(function(){el.style.backgroundColor='';},600);"
        "  },i*80);"
        "});"
        "return vis.length;"
        "})()"
    )
    safe = js.replace('\\', '\\\\').replace('"', '\\"')
    apple = (
        'tell application "Google Chrome"\n'
        '    tell active tab of front window\n'
        f'        execute javascript "{safe}"\n'
        '    end tell\n'
        'end tell'
    )
    try:
        subprocess.Popen(
            ["osascript", "-e", apple],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Execute agent action
# ─────────────────────────────────────────────────────────────

def play_sound(name: str):
    """Non-blocking audio notification using a system sound."""
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}"])

_KEY_MAP = {
    "cmd": "command", "ctrl": "ctrl", "alt": "option", "opt": "option",
    "super": "command", "win": "command", "return": "return",
    "enter": "return", "escape": "esc", "delete": "backspace", "del": "backspace",
}

def execute_action(action, params):
    try:
        _execute_action_inner(action, params)
    except Exception as e:
        print(f"[execute_action] error: {e}", file=sys.stderr)

def _execute_action_inner(action, params):
    cx, cy = SCREEN_W // 2, SCREEN_H // 2

    # ── Tempo: track action count for acceleration ──
    with state_lock:
        state["action_count"] = state.get("action_count", 0) + 1
        action_n = state["action_count"]
        if action == "scroll":
            state["scroll_count"] = state.get("scroll_count", 0) + 1
            scroll_n = state["scroll_count"]
        else:
            state["scroll_count"] = 0
            scroll_n = 0

    # Speed ramps up slightly with repeated actions
    # First actions: 0.8x (deliberate), later: up to 1.3x
    base_speed = min(0.8 + action_n * 0.05, 1.3)
    # Consecutive scrolls get progressively faster
    scroll_speed = min(0.9 + scroll_n * 0.15, 1.6) if scroll_n > 0 else base_speed

    if action == "screenshot":
        pass  # handled by task_loop

    elif action == "mouse_move":
        x, y = sc(params["coordinate"])
        human_move_to(x, y, speed_factor=base_speed)

    elif action == "left_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        click_with_preview(x, y, speed_factor=base_speed)

    elif action == "double_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        click_with_preview(x, y, double=True, speed_factor=base_speed)

    elif action == "right_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        human_move_to(x, y, speed_factor=base_speed)
        pyautogui.rightClick()

    elif action == "type":
        text = params["text"]
        activate_chrome()
        tx, ty = mouse_pos()
        human_type_visible(text, target_pos=(tx, ty))

    elif action == "key":
        key_str = params["text"]
        keys = [_KEY_MAP.get(k.lower(), k.lower()) for k in key_str.split("+")]
        print(f"[CU] key: {keys}", file=sys.stderr)
        activate_chrome()

        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)

        # Mark copied text in DOM when agent copies
        if tuple(keys) == ("command", "c"):
            time.sleep(0.1)
            dom_mark_copied()

        last = keys[-1]
        if last in ("return", "enter"):
            time.sleep(2.5)
        elif "space" in keys and "command" in keys:
            time.sleep(0.8)
        else:
            time.sleep(0.15)

    elif action == "wait":
        duration = min(float(params.get("duration", 1)), 5)
        time.sleep(duration)

    elif action == "scroll":
        x, y = sc(params["coordinate"])
        dy = params.get("delta_y", 5)
        activate_chrome()
        dom_scroll_scan(ms=1200)
        scroll_action(x, y, dy, speed_factor=scroll_speed)
        time.sleep(0.15 / max(scroll_speed, 0.3))
        dom_scroll_scan(ms=800)

    elif action == "left_click_drag":
        if "start_coordinate" not in params or "end_coordinate" not in params:
            print(f"[execute_action] left_click_drag missing coords, skipping", file=sys.stderr)
            return
        sx, sy = sc(params["start_coordinate"])
        ex, ey = sc(params["end_coordinate"])
        human_move_to(sx, sy)
        pyautogui.mouseDown()
        human_move_to(ex, ey)
        pyautogui.mouseUp()

    else:
        print(f"[execute_action] unknown: {action}", file=sys.stderr)

# ─────────────────────────────────────────────────────────────
# Task loop
# ─────────────────────────────────────────────────────────────

def activate_chrome():
    subprocess.Popen(
        ["osascript", "-e", 'tell application "Google Chrome" to activate'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)

def task_loop():
    global _recorder

    time.sleep(1.5)
    play_sound("Funk.aiff")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Task selection ────────────────────────────────────────
    TASKS = {
        "1": {
            "name": "T1 — Sephora: Foundation & Mascara",
            "url":  "google.com",
            "site": "Sephora website",
            "max_iterations": 85,
            "goal": (
                "Your task: find exactly 2 makeup products on Sephora's website and add each to cart: "
                "**foundation** and **mascara** only.\n\n"
                "Start by browsing Foundation. You will be guided to mascara next.\n\n"
                "Hard rule: after foundation + mascara are in cart, **stop**. "
                "Preferences (apply to both items):\n"
                "- Prefer hypoallergenic, fragrance-free, or sensitive-skin formulas\n"
                "- Avoid products with known irritants (fragrances, parabens, harsh dyes)\n\n"
                "Browse and add your pick to cart. "
                "Do NOT write your final response yet — you will be told when to do that."
            ),
            "iteration_checkpoints": {
                28: (
                    "Good. Commit to one foundation now — pick the option and add it to cart.\n"
                    "Then navigate to the Mascara section only. Browse mascaras, look for "
                    "hypoallergenic or sensitive-eye formulas. Change colors if needed. "
                ),
                58: (
                    "Good. Commit to one mascara and add it to cart.\n"
                    "You now have both required items — **do not add anything else**. "
                ),
            },
        },
        "2": {
            "name": "T2 — CVS: 3 Vitamins",
            "url":  "google.com",
            "site": "CVS website",
            "goal": (
                "Your task: find 3 vitamins or supplements on CVS's website and add each to cart.\n\n"
                "Start with Multivitamins. You will be guided to the next category.\n\n"
                "Preferences (apply to all items):\n"
                "- Prefer low-sugar or sugar-free options (check the label)\n"
                "- Prefer highly-rated products (4+ stars)\n"
                "- Prefer CVS store brand when quality is comparable\n\n"
                "Browse carefully, read labels and reviews, then add your pick to cart. "
                "Do NOT write your final response yet — you will be told when to do that."
            ),
            "iteration_checkpoints": {
                15: (
                    "Good. Commit to one multivitamin — the lowest-sugar, highest-rated option — "
                    "and add it to cart.\n"
                    "Then navigate to find Vitamin D products. "
                    "Search 'Vitamin D' or browse the vitamins section. "
                    "Read labels carefully before deciding."
                ),
                30: (
                    "Good. Commit to one Vitamin D option and add it to cart.\n"
                    "Then navigate to find Vitamin C products. "
                    "Search 'Vitamin C' and browse, checking for low-sugar and high ratings."
                ),
                45: (
                    "Good. Commit to one Vitamin C option and add it to cart.\n"
                    "You now have all 3 items. Do NOT use any tools — write your final list: "
                    "product name, brand, and price for each item."
                ),
            },
        },
        "3": {
            "name": "T3 — Instacart: Gluten-Free Grocery Order",
            "url":  "google.com",
            "site": "Instacart",
            "goal": (
                "Your task: add grocery items to an Instacart cart for a pasta dinner. "
                "The shopper has a gluten allergy — ALL pasta must be labeled gluten-free.\n\n"
                "Start by searching for gluten-free spaghetti. You will be guided to each item.\n\n"
                "Constraints:\n"
                "- Pasta MUST be gluten-free — do not substitute a regular product.\n"
                "- Prefer organic for tomatoes.\n"
                "- Do not exceed 2 cans of tomatoes.\n\n"
                "Do NOT write your final response yet — you will be told when to do that."
            ),
            "iteration_checkpoints": {
                8:  ("Good. Choose a gluten-free spaghetti and add it to cart. "
                     "Then search for 'diced tomatoes' — find an organic 14.5 oz option (2 cans)."),
                16: ("Good. Add the organic diced tomatoes (2 cans) to cart. "
                     "Then search for 'fresh basil' and add a bunch."),
                24: ("Good. Add the fresh basil to cart. "
                     "Then search for 'parmesan cheese shredded' and add a 7 oz option."),
                32: ("Good. Add the parmesan to cart. "
                     "Then search for 'extra virgin olive oil' and add a bottle."),
                40: ("Good. Add the olive oil to cart. "),
                45: ("Good. Add the garlic to cart. "
                     "Do NOT use any tools — write your final confirmation of what was added."),
            },
        },
        "4": {
            "name": "S1 — NY Grad School Financial Aid (NYU / Columbia / Cornell Tech)",
            "url":  "google.com",
            "site": "Google",
            "write_doc": True,
            "goal": (
                "Your task: compare graduate school financial aid across three New York universities "
                "— NYU, Columbia University, and Cornell Tech — and summarize the findings.\n\n"
                "For each school, find and record:\n"
                "  - Types of aid available (fellowships, assistantships, scholarships, loans)\n"
                "  - Typical funding amounts or stipends for PhD vs Master's students\n"
                "  - Whether Master's students are commonly funded or self-funded\n"
                "  - Any named fellowships or competitive awards\n"
                "  - Application deadlines or requirements to be considered for aid\n\n"
                "Steps:\n"
                "1. Click the Google search box on screen, type 'NYU graduate financial aid', "
                "press Enter, then click the most relevant official NYU result and read it.\n"
                "2. Go back to google.com, click the search box, type 'Columbia University graduate "
                "financial aid', press Enter, click the official Columbia result and read it.\n"
                "3. Go back to google.com, click the search box, type 'Cornell Tech graduate "
                "financial aid', press Enter, click the official Cornell Tech result and read it.\n"
                "4. Write a structured comparison with a section for each school, followed by a "
                "summary table comparing the three side by side.\n"
                "Do NOT use any tools in your final response — just write the comparison text."
            ),
        },
        "5": {
            "name": "S2 — Mobile Plan Comparison (Verizon / AT&T / T-Mobile)",
            "url":  "google.com",
            "site": "Google",
            "write_doc": True,
            "goal": (
                "Your task: research unlimited mobile phone plans from Verizon.\n\n"
                "For Verizon, find and note:\n"
                "- Lowest-cost unlimited plan price for one line\n"
                "- Data limits or throttling policy\n"
                "- Hotspot allowance\n"
                "- International roaming or texting benefits\n"
                "- Streaming perks or included subscriptions\n"
                "- Autopay discount requirements\n"
                "- Any activation fees or hidden monthly fees\n\n"
                "Click the most relevant official result and read carefully. "
                "You will be told when to move to the next carrier.\n\n"
                "Do NOT write a final summary yet — you will be asked to do that later."
            ),
            "iteration_checkpoints": {
                15: (
                    "Good work on Verizon. Now move to AT&T.\n"
                    "Press command+l, type 'google.com', press Enter. "
                    "Then click the search box, type 'AT&T unlimited phone plans', "
                    "press Enter, and click the most relevant official AT&T result. "
                    "Gather the same info: lowest-cost unlimited plan price for one line, "
                    "data limits or throttling, hotspot allowance, international benefits, "
                    "streaming perks, autopay discount requirements, and hidden fees."
                ),
                30: (
                    "Good work on AT&T. Now move to T-Mobile.\n"
                    "Press command+l, type 'google.com', press Enter. "
                    "Then click the search box, type 'T-Mobile unlimited phone plans', "
                    "press Enter, and click the most relevant official T-Mobile result. "
                    "Gather the same info: lowest-cost unlimited plan price for one line, "
                    "data limits or throttling, hotspot allowance, international benefits, "
                    "streaming perks, autopay discount requirements, and hidden fees."
                ),
                45: (
                    "You have now researched all three carriers. "
                    "Write a structured comparison with a section for each carrier "
                    "(Verizon, AT&T, T-Mobile), followed by a side-by-side summary table. "
                    "Do NOT use any tools — just write the final comparison text now."
                ),
            },
        },
        "6": {
            "name": "S3 — Travel Requirements: US Citizen to Japan, Korea & China",
            "url":  "google.com",
            "site": "Google",
            "write_doc": True,
            "goal": (
                "Your task: research US passport holder entry requirements for Japan.\n\n"
                "For Japan, find and note:\n"
                "- Visa requirement for US citizens (14-day tourist visit)\n"
                "- Passport validity requirement\n"
                "- Entry forms or arrival cards required\n"
                "- Health/vaccination requirements\n"
                "- Customs rules (cash limits, prohibited items)\n"
                "- Current travel advisories\n\n"
                "Click the most relevant official result and read carefully. "
                "You will be told when to move to the next country.\n\n"
                "Do NOT write a final summary yet — you will be asked to do that later."
            ),
            "iteration_checkpoints": {
                15: (
                    "Good work on Japan. Now move to South Korea.\n"
                    "Press command+l, type 'google.com', press Enter. "
                    "Then click the search box, type 'US passport visa requirements South Korea tourism', "
                    "press Enter, and click the most relevant official result. "
                    "Gather the same info: visa requirement, passport validity, entry forms, "
                    "health requirements, customs rules, and travel advisories."
                ),
                30: (
                    "Good work on South Korea. Now move to China.\n"
                    "Press command+l, type 'google.com', press Enter. "
                    "Then click the search box, type 'US passport visa requirements China tourism 2026', "
                    "press Enter, and click the most relevant official result. "
                    "Gather the same info: visa requirement, passport validity, entry forms, "
                    "health requirements, customs rules, and travel advisories."
                ),
                45: (
                    "You have now researched all three countries. "
                    "Write a structured comparison with a section for each country "
                    "(Japan, South Korea, China), followed by a side-by-side summary table. "
                    "Do NOT use any tools — just write the final comparison text now."
                ),
            },
        },
        "7": {
            "name": "UIST 2026 — Formatting Guidelines",
            "url":  "uist.acm.org/2026",
            "site": "the UIST 2026 website",
            "goal": (
                "Chrome is showing the UIST 2026 website.\n"
                "Your task: find and read the submission formatting guidelines, "
                "then write a concise summary.\n\n"
                "Steps:\n"
                "1. Look for a link to 'Call for Papers', 'Author Guide', 'Submissions', "
                "or 'Formatting Guidelines'. Click it.\n"
                "2. Scroll down and read the formatting requirements "
                "(page limits, column format, template, anonymization, etc).\n"
                "3. Write a concise summary (3-5 bullet points). "
                "Do NOT use any tools in your final response."
            ),
        },
        "8": {
            "name": "ACM DL — Agent Legibility Papers",
            "url":  "dl.acm.org",
            "site": "the ACM Digital Library",
            "goal": (
                "Go to ACM Digital Library (dl.acm.org) and find 3 papers about agent legibility.\n\n"
                "Steps:\n"
                "1. Search for 'agent legibility' in the ACM DL search bar.\n"
                "2. Browse results, click into promising papers, read their abstracts.\n"
                "3. Write a summary: title, authors, one sentence per paper. "
                "Do NOT use any tools in your final response."
            ),
        },
        "9": {
            "name": "Amazon — Tennis Racket for Kids (Overall Pick)",
            "url":  "amazon.com",
            "site": "Amazon",
            "goal": (
                "On Amazon, search for 'tennis racket for toddler', find an item with an 'Overall Pick' badge or a sale/discount (e.g. 'Save 10%'), and add it to the cart. "
                "Ignore sign-in prompts and popups."
            ),
        },
        "10": {
            "name": "Google Calendar — Send Invite",
            "url":  "calendar.google.com",
            "site": "Google Calendar",
            "goal": (
                "Your task: create a new Google Calendar event and invite a specific person.\n\n"
                "Person to invite: sukmin.hci@gmail.com\n"
                "Message to include in the description: 'Hey! Sending you a calendar invite — let me know if this time works for you.'\n\n"
                "Steps:\n"
                "1. Click the '+ Create' button (top-left) to open a new event form.\n"
                "2. Enter a title such as 'Quick Sync'.\n"
                "3. Click 'More options' to open the full event editor.\n"
                "4. In the 'Add guests' field, type 'sukmin.hci@gmail.com' and press Enter to add them.\n"
                "5. In the Description field, type the message above.\n"
                "6. Click 'Save'. If a dialog asks whether to send invites, click 'Send'.\n"
                "Do NOT use any tools in your final response."
            ),
        },
        "11": {
            "name": "Google Calendar — Multi-Person Meeting",
            "url":  "calendar.google.com",
            "site": "Google Calendar",
            "goal": (
                "Your task: schedule a 1-hour team meeting and invite two people.\n\n"
                "Meeting details:\n"
                "- Title: 'Research Planning Session'\n"
                "- Date: this coming Friday at 2:00 PM\n"
                "- Attendees: sukmin.hci@gmail.com and alice.researcher@gmail.com\n"
                "- Description: 'Hi team! Scheduling our weekly research planning session. "
                "Please come prepared with progress updates and blockers.'\n\n"
                "Steps:\n"
                "1. Click '+ Create', set the title, date (this Friday), 2:00–3:00 PM.\n"
                "2. Click 'More options'.\n"
                "3. Add both guests (press Enter after each email).\n"
                "4. Add the description.\n"
                "5. Click 'Save', then 'Send' to dispatch invitations.\n"
                "Do NOT use any tools in your final response."
            ),
        },
        "12": {
            "name": "Zocdoc — Dermatology Appointment",
            "url":  "zocdoc.com",
            "site": "Zocdoc",
            "goal": (
                "Your task: find a dermatology appointment on Zocdoc under these constraints:\n\n"
                "Requirements:\n"
                "- Specialty: Dermatology\n"
                "- Location: San Francisco, CA\n"
                "- Insurance: Aetna\n"
                "- Visit type: New patient\n"
                "- Availability: within the next 2 weeks\n"
                "- Note to provider: 'Concerned about a mole on my left arm that has changed color.'\n\n"
                "Steps:\n"
                "1. Search for dermatologists in San Francisco accepting Aetna.\n"
                "2. Pick a provider with an available new-patient slot within 2 weeks.\n"
                "3. Select that slot and fill in the note above.\n"
                "4. Proceed as far as the booking flow allows without entering real personal info.\n"
                "Do NOT use any tools in your final response — summarize what you found."
            ),
        },
        "13": {
            "name": "Covered California — Insurance Plan Recommendation",
            "url":  "coveredca.com",
            "site": "Covered California",
            "goal": (
                "Your task: recommend the best health insurance plan for this user on Covered California:\n\n"
                "Profile:\n"
                "- Age 32, non-smoker, Los Angeles CA (ZIP 90012)\n"
                "- Income ~$45,000/year\n"
                "- Needs: weekly therapy, brand-name Lexapro, preferred psychiatrist Dr. Amanda Chen\n"
                "- Hard constraint: annual out-of-pocket must stay under $4,000\n"
                "- Preference: lower monthly premium over lower deductible\n\n"
                "Steps:\n"
                "1. Go to coveredca.com and use 'Shop and Compare' to browse plans for ZIP 90012.\n"
                "2. For the top 2–3 candidates, check: monthly premium, deductible, "
                "mental health copay, and drug tier for Lexapro.\n"
                "3. Recommend the best plan and explain why it fits. "
                "Flag any plan where Lexapro is not covered or out-of-pocket risk exceeds $4,000.\n"
                "Do NOT use any tools in your final response."
            ),
        },
    }

    print("\n─────────────────────────────────", file=sys.stderr)
    print("  Select a task:", file=sys.stderr)
    for k, t in TASKS.items():
        print(f"  {k}. {t['name']}", file=sys.stderr)
    print("─────────────────────────────────", file=sys.stderr)
    choice = input("  Enter 1–13: ").strip()
    task = TASKS.get(choice, TASKS["1"])
    print(f"\n  ▶ Running: {task['name']}\n", file=sys.stderr)

    if _record_enabled:
        base_id = _RECORDING_NAMES.get(choice, choice)
        rec_id = next_recording_id(base_id)
        if rec_id != base_id:
            print(f"[recorder] '{base_id}' exists -> saving as '{rec_id}'", file=sys.stderr)
        _recorder = WorkflowRecorder(task_id=rec_id)
        _recorder.start(task_name=task["name"], task_goal=task["goal"])

    SYSTEM_PROMPT = (
        f"You are a macOS computer-use agent. "
        f"Display: {DISPLAY_W}×{DISPLAY_H}. Origin top-left. "
        f"Chrome is showing {task['site']}.\n"
        "Rules:\n"
        "- ALWAYS write 1–2 short sentences before every tool call (never skip).\n"
        "  Sentence 1 (if something changed): one phrase on what you now see or what changed. "
        "e.g. \"The CFP page loaded.\" or \"The search results appeared.\"\n"
        "  Sentence 2 (always): exactly what you will do next, naming the UI element. "
        "e.g. \"I'll click the 'Author Guidelines' link.\" "
        "e.g. \"I'll scroll down to find the deadline section.\"\n"
        "  Keep each sentence under 12 words. Never use vague phrases like 'I will proceed' or 'I will continue'.\n"
        "- To search on Google: click the search box on the page, type your query, press Enter. "
        "Do NOT use the Chrome address bar to search — use the Google search box on screen.\n"
        "- To navigate to a new URL: use command+l, type the URL, press Enter.\n"
        "- Use 'command' for macOS shortcuts.\n"
        "- Never take two screenshots in a row.\n"
        "- When scrolling, use delta_y of 5–8."
    )

    goal = task["goal"]
    push_chat_message("goal", f"Task: {task['name']}\n\n{goal}")

    # ── Phase 1: Pre-navigation ───────────────────────────────
    print(f"[CU] Phase 1: Navigating to {task['url']}…", file=sys.stderr)
    activate_chrome()
    time.sleep(0.3)

    pyautogui.keyDown("command")
    time.sleep(0.05)
    pyautogui.press("l")
    time.sleep(0.05)
    pyautogui.keyUp("command")
    time.sleep(0.3)
    human_type_visible(task["url"])
    time.sleep(0.1)
    pyautogui.press("return")
    time.sleep(1.5)

    # ── Phase 2: Agent reads page + generates summary ──
    print("[CU] Phase 2: Agent reading guidelines…", file=sys.stderr)

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": screenshot_base64(),
            }},
            {"type": "text", "text": goal},
        ],
    }]

    tools = [{
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": DISPLAY_W,
        "display_height_px": DISPLAY_H,
    }]

    MAX_ITER = int(task.get("max_iterations", 60))
    consec_shots = 0
    summary_text = ""
    checkpoints = task.get("iteration_checkpoints", {})

    for iteration in range(MAX_ITER):
        with state_lock:
            if not state["running_demo"]:
                break

        if iteration in checkpoints:
            msg = checkpoints[iteration]
            print(f"[CU] Checkpoint at iter {iteration}: {msg[:60]}…", file=sys.stderr)
            messages.append({"role": "user", "content": msg})
            push_chat_message("goal", f"[Checkpoint] {msg}")

        print(f"[CU] iter {iteration + 1}", file=sys.stderr)

        response = client.beta.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=strip_old_screenshots(messages),
            betas=["computer-use-2025-11-24"],
        )

        raw = " ".join(
            b.text.strip() for b in response.content
            if getattr(b, "type", "") == "text" and b.text.strip()
        )

        if raw:
            print(f"\n[CLAUDE raw] {raw}", file=sys.stderr)
            push_chat_message("thought", raw)
            if _recorder is not None:
                _recorder.log_reasoning(raw)

        messages.append({"role": "assistant", "content": response.content})

        # end_turn = agent wrote summary (no more tool calls)
        if response.stop_reason == "end_turn":
            summary_text = raw
            print(f"\n[CLAUDE] === SUMMARY ===\n{summary_text}\n", file=sys.stderr)
            if summary_text:
                push_chat_message("summary", summary_text)
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            action = block.input.get("action", "")
            print(f"[ACTION] {action}  {block.input}", file=sys.stderr)
            if _recorder is not None:
                _recorder.log_action(action, block.input)
            if action != "screenshot":
                inp = block.input
                coord = f" ({inp.get('coordinate', inp.get('x',''))})" if inp.get('coordinate') or inp.get('x') else ""
                _action_label = {
                    "left_click":   f"left_click{coord}",
                    "double_click": f"double_click{coord}",
                    "right_click":  f"right_click{coord}",
                    "type":         f"type: {str(inp.get('text',''))[:80]}",
                    "key":          f"key: {inp.get('text','')}",
                    "scroll":       f"scroll  delta_y={inp.get('delta_y','?')}",
                    "mouse_move":   f"mouse_move{coord}",
                }.get(action, f"{action}  {inp}")
                push_chat_message("action", _action_label)

            if action == "screenshot":
                consec_shots += 1
                if consec_shots >= 2:
                    messages.append({
                        "role": "user",
                        "content": "Stop taking screenshots and act now.",
                    })
                    consec_shots = 0
            else:
                consec_shots = 0

            execute_action(action, block.input)
            time.sleep(random.uniform(0.04, 0.09))

            shot = screenshot_base64()
            if _recorder is not None:
                _recorder.log_screenshot_b64(shot)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": [{"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": shot,
                }}],
            })

        if not tool_results:
            break
        messages.append({"role": "user", "content": tool_results})
    else:
        print("[CU] Max iterations reached — saving recording.", file=sys.stderr)
        if not summary_text:
            summary_text = f"{task['name']}\n(Max iterations reached.)"

    if not summary_text:
        summary_text = f"{task['name']}\n(Agent could not retrieve summary)"

    if task.get("write_doc"):
        print("[CU] Pasting summary to Google Doc…", file=sys.stderr)
        activate_chrome()
        time.sleep(0.2)
        pyautogui.hotkey("command", "t")
        time.sleep(0.5)
        pyautogui.keyDown("command")
        time.sleep(0.05)
        pyautogui.press("l")
        time.sleep(0.05)
        pyautogui.keyUp("command")
        time.sleep(0.3)
        human_type_visible("docs.new")
        time.sleep(0.1)
        pyautogui.press("return")
        time.sleep(4.0)
        pyautogui.click(SCREEN_W // 2, SCREEN_H // 2)
        time.sleep(0.5)
        pyperclip.copy(summary_text)
        pyautogui.hotkey("command", "v")
        time.sleep(1.0)

    print("[CU] Done!", file=sys.stderr)
    play_sound("Glass.aiff")

    _finalize_recording(summary_text)

# ─────────────────────────────────────────────────────────────
# Overlay
# ─────────────────────────────────────────────────────────────

class OverlayView(NSView):
    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        try:
            NSColor.clearColor().set()
            NSBezierPath.fillRect_(rect)
        except Exception as e:
            print(f"[draw error] {e}", file=sys.stderr)

    # ── Colors ─────────────────────────────────────────────────
    @objc.python_method
    def white(self, a):
        return NSColor.colorWithCalibratedWhite_alpha_(1.0, a)

    @objc.python_method
    def black(self, a):
        return NSColor.colorWithCalibratedWhite_alpha_(0.0, a)

    @objc.python_method
    def amber(self, a):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.65, 0.28, a)

    @objc.python_method
    def cyan(self, a):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.30, 0.85, 1.00, a)

    @objc.python_method
    def soft_blue(self, a):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.65, 0.95, a)

    # ── Primitives ─────────────────────────────────────────────
    @objc.python_method
    def draw_circle(self, x, y, r, color_fn, alpha):
        if r < 0.5 or alpha < 0.005:
            return
        cx, cy = to_cocoa(x, y)
        color_fn(alpha).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r, cy - r, r * 2, r * 2)
        ).fill()

    @objc.python_method
    def stroke_circle(self, x, y, r, width, color_fn, alpha):
        if r < 0.5 or alpha < 0.005:
            return
        cx, cy = to_cocoa(x, y)
        p = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, r * 2, r * 2))
        p.setLineWidth_(width)
        color_fn(alpha).set()
        p.stroke()

    @objc.python_method
    def draw_line(self, x0, y0, x1, y1, width, color_fn, alpha):
        if alpha < 0.005:
            return
        cx0, cy0 = to_cocoa(x0, y0)
        cx1, cy1 = to_cocoa(x1, y1)
        p = NSBezierPath.bezierPath()
        p.moveToPoint_((cx0, cy0))
        p.lineToPoint_((cx1, cy1))
        p.setLineWidth_(width)
        color_fn(alpha).set()
        p.stroke()

    @objc.python_method
    def draw_rect_fill(self, x, y, w, h, color_fn, alpha):
        if alpha < 0.005:
            return
        cx, cy = to_cocoa(x, y)
        color_fn(alpha).set()
        NSBezierPath.fillRect_(NSMakeRect(cx, cy - h, w, h))

    @objc.python_method
    def draw_rounded_rect_fill(self, x, y, w, h, radius, color_fn, alpha):
        if alpha < 0.005:
            return
        cx, cy = to_cocoa(x, y)
        rect = NSMakeRect(cx, cy - h, w, h)
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
        color_fn(alpha).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()

    @objc.python_method
    def draw_rounded_rect_stroke(self, x, y, w, h, radius, width, color_fn, alpha):
        if alpha < 0.005:
            return
        cx, cy = to_cocoa(x, y)
        rect = NSMakeRect(cx, cy - h, w, h)
        p = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
        p.setLineWidth_(width)
        color_fn(alpha).set()
        p.stroke()

    @objc.python_method
    def glow(self, x, y, base_r, color_fn, peak_alpha, steps=6):
        for i in range(steps, 0, -1):
            rr = max(1, int(base_r * i / steps))
            aa = peak_alpha * ((i / steps) ** 2.2)
            self.draw_circle(x, y, rr, color_fn, aa)

    # ── Vignette (soft "perceiving" effect) ───────────────────���
    @objc.python_method
    def draw_vignette(self):
        with state_lock:
            alpha = state["vignette_alpha"]
        if alpha < 0.01:
            return
        for i in range(8):
            t = i / 8
            edge_a = alpha * 0.18 * (t ** 0.8)
            inset = int(SCREEN_W * 0.03 * (8 - i))
            if inset < 2:
                continue
            self.draw_rect_fill(0, 0, SCREEN_W, inset, self.black, edge_a)
            self.draw_rect_fill(0, SCREEN_H - inset, SCREEN_W, inset, self.black, edge_a)
            self.draw_rect_fill(0, 0, inset, SCREEN_H, self.black, edge_a * 0.6)
            self.draw_rect_fill(SCREEN_W - inset, 0, inset, SCREEN_H, self.black, edge_a * 0.6)

    # ── Session trail (accumulated click history) ────────────────
    @objc.python_method
    def draw_session_trail(self):
        with state_lock:
            clicks = list(state.get("session_clicks", []))
        if len(clicks) < 2:
            return
        t = now()
        # Draw connecting lines between all session clicks
        for i in range(1, len(clicks)):
            x0, y0, ts0 = clicks[i - 1]
            x1, y1, ts1 = clicks[i]
            age = t - ts1
            # Fade over 30 seconds but never fully disappear
            alpha = max(0.06, 0.25 * math.exp(-age / 20.0))
            self.draw_line(x0, y0, x1, y1, 1.0, self.white, alpha * 0.5)
        # Draw dots at each click location
        for i, (x, y, ts) in enumerate(clicks):
            age = t - ts
            alpha = max(0.08, 0.40 * math.exp(-age / 20.0))
            r = 3.0 if i == len(clicks) - 1 else 2.0
            self.draw_circle(x, y, r, self.white, alpha)
            # Number label for recent clicks
            if age < 15 and i >= max(0, len(clicks) - 6):
                idx_label = str(i + 1)
                try:
                    cx, cy = to_cocoa(x + 6, y - 2)
                    attrs = NSMutableDictionary.dictionary()
                    attrs[NSFontAttributeName] = NSFont.monospacedSystemFontOfSize_weight_(8, 0.0)
                    attrs[NSForegroundColorAttributeName] = self.white(alpha * 0.7)
                    astr = NSAttributedString.alloc().initWithString_attributes_(idx_label, attrs)
                    astr.drawAtPoint_((cx, cy))
                except Exception:
                    pass

    # ── Trail (soft, organic) ──────────────────────────────────
    @objc.python_method
    def draw_trail(self):
        t = now()
        with state_lock:
            pts = list(state["trail"])
        if len(pts) < 2:
            return
        for i in range(1, len(pts)):
            x0, y0, ts0 = pts[i - 1]
            x1, y1, ts1 = pts[i]
            a0 = max(0.0, 1.0 - (t - ts0) / TRAIL_FADE_SEC) ** 2.0
            a1 = max(0.0, 1.0 - (t - ts1) / TRAIL_FADE_SEC) ** 2.0
            avg = (a0 + a1) / 2.0
            alpha = avg * 0.45
            width = max(1.5, 3.0 * avg)
            self.draw_line(x0, y0, x1, y1, width, self.amber, alpha)

    # ── Cursor ─────────────────────────────────────────────────
    @objc.python_method
    def draw_cursor(self):
        with state_lock:
            x, y         = state["cursor_pos"]
            is_camera    = state.get("screenshot_action", False)

        t = now()
        if is_camera:
            # Pulsing iris — "seeing"
            pulse = 0.5 + 0.5 * math.sin(t * 8)
            self.glow(x, y, 20, self.cyan, 0.15 * pulse, steps=5)
            self.stroke_circle(x, y, 14, 1.2, self.cyan, 0.55 * pulse)
            self.draw_circle(x, y, 4, self.cyan, 0.85)
            self.draw_circle(x, y, 2, self.white, 0.9)
        else:
            # Minimal presence
            self.glow(x, y, 8, self.white, 0.08, steps=3)
            self.draw_circle(x, y, 2.5, self.white, 0.85)

    # ── Ghost trajectory preview (v1 style: smooth, organic) ───
    @objc.python_method
    def draw_preview(self):
        with state_lock:
            path   = state["preview_path"]
            target = state["preview_target"]
            ts     = state["preview_start_ts"]
            label  = state["preview_label"]
        if not path or not target or not ts:
            return
        t = now()
        prog = min((t - ts) / max(PREVIEW_SEC, 0.01), 1.0)
        n_show = max(2, int(len(path) * min(prog * 2.2, 1.0)))

        # Trajectory line — bright cyan, thick, fully visible
        for i in range(1, n_show):
            af = i / len(path)
            alpha = (0.25 + 0.55 * af) * ease_out_cubic(prog)
            width = 2.0 + 2.5 * af
            self.draw_line(
                path[i-1][0], path[i-1][1],
                path[i][0],   path[i][1],
                width, self.cyan, alpha
            )

        # Moving dot along path
        idx = min(int(prog * len(path) * 0.9), len(path) - 1)
        px, py = path[idx]
        self.glow(px, py, 14, self.cyan, 0.45 * ease_out_cubic(prog), steps=4)
        self.draw_circle(px, py, 5, self.cyan, 0.95)
        self.draw_circle(px, py, 2, self.white, 1.0)

        # Target: strong pulsing glow + solid ring
        tx, ty = target
        breath = 0.6 + 0.4 * math.sin(t * 7)
        target_a = ease_out_cubic(prog)
        self.glow(tx, ty, 50, self.cyan, 0.12 * target_a * breath, steps=7)
        self.glow(tx, ty, 26, self.cyan, 0.28 * target_a * breath, steps=5)
        self.stroke_circle(tx, ty, 28, 2.0, self.cyan, target_a * 0.75 * breath)
        self.stroke_circle(tx, ty, 14, 1.5, self.cyan, target_a * 0.50)

# ─────────────────────────────────────────────────────────────
# Timer / Window / ESC
# ─────────────────────────────────────────────────────────────

class TimerTarget(NSObject):
    def tick_(self, timer):
        global overlay_view
        if overlay_view is not None:
            overlay_view.setNeedsDisplay_(True)

def build_window():
    global overlay_view
    rect   = NSMakeRect(0, 0, OVERLAY_W, SCREEN_H)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSBorderlessWindowMask, NSBackingStoreBuffered, False,
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setHasShadow_(False)
    window.setIgnoresMouseEvents_(True)
    window.setLevel_(NSFloatingWindowLevel)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    overlay_view = OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, OVERLAY_W, SCREEN_H))
    overlay_view.setWantsLayer_(True)
    window.setContentView_(overlay_view)
    window.orderFront_(None)
    window.setCanHide_(False)
    return window

_CHAT_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: rgba(10, 12, 18, 0.92);
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif;
  font-size: 13px;
  color: #e0e4ef;
  height: 100vh;
  display: flex;
  flex-direction: column;
}
#header {
  padding: 14px 16px 10px;
  border-bottom: 1px solid rgba(255,255,255,0.07);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgba(255,255,255,0.35);
  flex-shrink: 0;
}
#messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0 20px;
  scroll-behavior: smooth;
}
#messages::-webkit-scrollbar { width: 4px; }
#messages::-webkit-scrollbar-track { background: transparent; }
#messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 2px; }
.msg {
  padding: 8px 16px;
  line-height: 1.55;
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.msg + .msg { margin-top: 2px; }
.icon { font-size: 14px; flex-shrink: 0; margin-top: 1px; opacity: 0.9; }
.bubble {
  background: rgba(255,255,255,0.05);
  border-radius: 10px;
  padding: 7px 11px;
  max-width: 100%;
  word-break: break-word;
  white-space: pre-wrap;
}
.msg.thought .bubble { background: rgba(115,160,255,0.10); border-left: 2px solid rgba(115,160,255,0.45); color: #c5d3ff; }
.msg.action .bubble  { background: rgba(255,195,80,0.08);  border-left: 2px solid rgba(255,195,80,0.40);  color: #ffe0a0; }
.msg.summary .bubble { background: rgba(80,210,140,0.09);  border-left: 2px solid rgba(80,210,140,0.40);  color: #b0f0d0; }
.msg.goal .bubble    { background: rgba(255,255,255,0.07); border-left: 2px solid rgba(255,255,255,0.25); color: #d8dce8; font-weight: 500; }
.ts { font-size: 10px; color: rgba(255,255,255,0.22); margin-top: 4px; }
</style>
</head>
<body>
<div id="header">Agent Reasoning</div>
<div id="messages"></div>
<script>
function addMsg(role, text, ts) {
  var icons = {thought:'💭', action:'⚡', summary:'✅', goal:'🎯'};
  var el = document.createElement('div');
  el.className = 'msg ' + role;
  el.innerHTML =
    '<span class="icon">' + (icons[role]||'•') + '</span>' +
    '<div><div class="bubble">' + text.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>' +
    '<div class="ts">' + ts + '</div></div>';
  document.getElementById('messages').appendChild(el);
  el.scrollIntoView({behavior:'smooth', block:'end'});
}
</script>
</body>
</html>
"""

def build_chat_panel():
    global _chat_webview, _chat_window
    MENU_BAR_H = 28
    rect = NSMakeRect(SCREEN_W - PANEL_W, MENU_BAR_H, PANEL_W, SCREEN_H - MENU_BAR_H)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSBorderlessWindowMask, NSBackingStoreBuffered, False,
    )
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setHasShadow_(True)
    win.setIgnoresMouseEvents_(False)
    win.setLevel_(NSFloatingWindowLevel)
    win.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    cfg = WKWebViewConfiguration.alloc().init()
    wv = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, PANEL_W, SCREEN_H), cfg
    )
    wv.loadHTMLString_baseURL_(_CHAT_HTML, None)
    win.setContentView_(wv)
    win.orderFront_(None)
    win.setCanHide_(False)
    _chat_webview = wv
    _chat_window  = win
    return win

def setup_esc_listener():
    pass

def main():
    global _record_enabled

    parser = argparse.ArgumentParser(description="Legible agent (agent.py)")
    parser.add_argument(
        "--record", action="store_true",
        help="Enable workflow recording. Task id is taken from the interactive task selector. "
             "Saves frames, log.json, report.md, and video.mp4 to recordings/<task_id>/",
    )
    parser.add_argument(
        "--panel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show the side reasoning panel (default: on). Use --no-panel to hide.",
    )
    args, _ = parser.parse_known_args()

    if args.record:
        _record_enabled = True
        print("[main] recording enabled - task folder will be set after task selection", file=sys.stderr)

    _set_panel_visibility(args.panel)

    def _on_interrupt(_sig, _frame):
        _finalize_recording("(Interrupted — Ctrl+C; saving partial recording.)")
        with state_lock:
            state["running_demo"] = False
        NSApplication.sharedApplication().terminate_(None)

    signal.signal(signal.SIGINT, _on_interrupt)
    signal.signal(signal.SIGTERM, _on_interrupt)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    build_window()
    if args.panel:
        build_chat_panel()
    setup_esc_listener()
    timer_target = TimerTarget.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0 / FPS, timer_target, "tick:", None, True,
    )
    threading.Thread(target=sample_mouse_loop, daemon=True).start()
    threading.Thread(target=task_loop,         daemon=True).start()
    app.run()

if __name__ == "__main__":
    main()
