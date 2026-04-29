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

import mss
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
    NSEvent,
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
    time.sleep(1.5)
    play_sound("Funk.aiff")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Task selection ────────────────────────────────────────
    TASKS = {
        "1": {
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
        "2": {
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
        "3": {
            "name": "Amazon — Tennis Racket for Kids (Overall Pick)",
            "url":  "amazon.com",
            "site": "Amazon",
            "goal": (
                "On Amazon, search for 'tennis racket for toddler', find an item with an 'Overall Pick' badge or a sale/discount (e.g. 'Save 10%'), and add it to the cart. "
                "Ignore sign-in prompts and popups."
            ),
        },
        "4": {
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
        # ── Transactional ──────────────────────────────────────
        "5": {
            "name": "T1 — Google Calendar: Multi-Person Meeting",
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
        "6": {
            "name": "T2 — Instacart: Vegan Pasta Primavera Shopping",
            "url":  "instacart.com",
            "site": "Instacart",
            "goal": (
                "📋 Shopping for tonight's dinner\n\n"
                "Making: Vegan pasta primavera for 4 people\n\n"
                "Already at home:\n"
                "✓ Pasta, garlic, cherry tomatoes, zucchini, lemon\n\n"
                "Need to buy (3 items):\n"
                "1. Fresh basil — 1 bunch\n"
                "2. Olive oil — small bottle, extra virgin\n"
                "3. Parmesan-style topping — must be vegan\n\n"
                "⚠️ Constraints:\n"
                "- One guest has a SEVERE tree-nut allergy (no cashews, almonds, etc.)\n"
                "- Everyone is vegan (no dairy, no eggs)\n"
                "- Budget: $30 total\n\n"
                "Search for each item and add it to cart. For each item, briefly explain "
                "why you chose that specific product over alternatives — especially for "
                "the parmesan-style topping, which requires care: real parmesan is dairy, "
                "and many vegan alternatives are cashew-based (tree nut). Find one that is "
                "both vegan AND nut-free.\n"
                "Do NOT use any tools in your final response — confirm what was added and why."
            ),
        },
        "7": {
            "name": "T3 — Zocdoc: Dermatology Appointment",
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
        # ── Information Synthesis ──────────────────────────────
        "8": {
            "name": "S1 — CHI 2025 vs 2026: Submission Requirement Comparison",
            "url":  "chi2026.acm.org",
            "site": "the CHI 2026 conference website",
            "goal": (
                "Your task: compare paper submission requirements between CHI 2025 and CHI 2026 "
                "and flag differences that could cause a desk rejection.\n\n"
                "Steps:\n"
                "1. Find the submission/formatting requirements on chi2026.acm.org.\n"
                "2. Navigate to chi2025.acm.org and find the equivalent page.\n"
                "3. Compare: page limits, anonymization policy, reference format, "
                "figure guidelines, supplemental material rules, and new requirements.\n"
                "4. Write a summary: (a) unchanged rules, (b) changed rules, "
                "(c) new 2026 rules not in 2025.\n"
                "Do NOT use any tools in your final response."
            ),
        },
        "9": {
            "name": "S2 — HealthCare.gov: Insurance Plan Recommendation",
            "url":  "healthcare.gov",
            "site": "HealthCare.gov",
            "goal": (
                "Your task: recommend the best health insurance plan for this user:\n\n"
                "Profile:\n"
                "- Age 32, non-smoker, Austin TX (ZIP 78701)\n"
                "- Income ~$45,000/year\n"
                "- Needs: weekly therapy, brand-name Lexapro, preferred psychiatrist Dr. Amanda Chen\n"
                "- Hard constraint: annual out-of-pocket must stay under $4,000\n"
                "- Preference: lower monthly premium over lower deductible\n\n"
                "Steps:\n"
                "1. Browse plans available at ZIP 78701.\n"
                "2. For the top 2–3 candidates, check: monthly premium, deductible, "
                "mental health copay, and drug tier for Lexapro.\n"
                "3. Recommend the best plan and explain why it fits. "
                "Flag any plan where Lexapro is not covered or out-of-pocket risk exceeds $4,000.\n"
                "Do NOT use any tools in your final response."
            ),
        },
        "10": {
            "name": "S3 — Travel Requirements: US Citizen to Japan",
            "url":  "travel.state.gov",
            "site": "the U.S. Department of State travel site",
            "goal": (
                "Your task: compile a complete travel requirements checklist for this trip:\n\n"
                "Scenario: US passport holder, San Francisco → Tokyo, 14-day tourist visit, no layover.\n\n"
                "Collect all of the following:\n"
                "1. Passport validity requirement\n"
                "2. Visa requirement (do US citizens need a visa for 14-day tourism?)\n"
                "3. Entry forms or arrival cards required\n"
                "4. Health/vaccination requirements or recommendations\n"
                "5. Customs rules (cash limits, prohibited items)\n"
                "6. Current travel advisories or entry restrictions\n\n"
                "Start at travel.state.gov, then check japan.travel or japan-guide.com "
                "for local entry details. Compile a clear checklist and flag anything "
                "that changed in the last 6 months.\n"
                "Do NOT use any tools in your final response."
            ),
        },
    }

    print("\n─────────────────────────────────", file=sys.stderr)
    print("  Select a task:", file=sys.stderr)
    for k, t in TASKS.items():
        print(f"  {k}. {t['name']}", file=sys.stderr)
    print("─────────────────────────────────", file=sys.stderr)
    choice = input("  Enter 1–10: ").strip()
    task = TASKS.get(choice, TASKS["1"])
    print(f"\n  ▶ Running: {task['name']}\n", file=sys.stderr)

    SYSTEM_PROMPT = (
        f"You are a macOS computer-use agent. "
        f"Display: {DISPLAY_W}×{DISPLAY_H}. Origin top-left. "
        f"Chrome is showing {task['site']}.\n"
        "Rules:\n"
        "- ALWAYS write a short narration sentence (max 12 words) before any tool call. "
        "Name the exact UI element: "
        "e.g. \"I'll click the 'Full CFP' tab.\" "
        "e.g. \"I'll scroll down to find the submission deadline.\" "
        "Never skip this narration.\n"
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
    time.sleep(0.4)
    human_type_visible(task["url"])
    time.sleep(0.1)
    pyautogui.press("return")
    time.sleep(3.5)

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

    MAX_ITER = 30
    consec_shots = 0
    summary_text = ""

    for iteration in range(MAX_ITER):
        with state_lock:
            if not state["running_demo"]:
                return

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
            time.sleep(random.uniform(0.08, 0.18))

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": [{"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": screenshot_base64(),
                }}],
            })

        if not tool_results:
            break
        messages.append({"role": "user", "content": tool_results})
    else:
        print("[CU] Max iterations.", file=sys.stderr)

    print("[CU] Done!", file=sys.stderr)
    play_sound("Glass.aiff")

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
    def on_key(event):
        try:
            if event.keyCode() == 53:
                print("[overlay] ESC – stopping.", file=sys.stderr)
                with state_lock:
                    state["running_demo"] = False
                NSApplication.sharedApplication().terminate_(None)
        except Exception:
            pass
    try:
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, on_key)
    except Exception as e:
        print(f"[esc] {e}", file=sys.stderr)

def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    build_window()
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
