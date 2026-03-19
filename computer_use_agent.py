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

load_dotenv()

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

TRAIL_FADE_SEC     = 2.5
PREVIEW_SEC        = 0.20
FPS                = 60
BUBBLE_FADE_IN     = 0.25
BUBBLE_FADE_OUT    = 0.60
BUBBLE_SLIDE_PX    = 12
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
    "reasoning":          False,
    "reasoning_text":     "",
    "reasoning_start_ts": None,
    "reasoning_end_ts":   None,
    "goal_ts":            None,
    "goal_text":          "",
    "vignette_alpha":     0.0,
    "vignette_target":    0.0,
    # ── Legibility ──
    "progress_step":      0,
    "progress_total":     0,
    "progress_label":     "",
    "session_clicks":     [],        # accumulated (x, y, ts) for all clicks in session
    "action_count":       0,         # total actions taken (for tempo acceleration)
    "scroll_count":       0,         # consecutive scrolls (for tempo acceleration)
    "reading_done":       False,      # flips True when DOM reading finishes
}

state_lock   = threading.Lock()
overlay_view = None

screen   = NSScreen.mainScreen()
frame    = screen.frame()
SCREEN_W = int(frame.size.width)
SCREEN_H = int(frame.size.height)

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

def first_sentence(text, max_len=60):
    if not text:
        return ""
    m = re.search(r'[.!?](?:\s|$)', text)
    s = text[:m.end()].strip() if m else text.strip()
    if len(s) > max_len:
        cut = s[:max_len].rfind(' ')
        s = s[:cut] + "…" if cut > 10 else s[:max_len] + "…"
    return s

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

def set_progress(step, total, label=""):
    with state_lock:
        state["progress_step"]  = step
        state["progress_total"] = total
        state["progress_label"] = label

# ─────────────────────────────────────────────────────────────
# Human-like movement (organic, with overshoot + ease)
# ─────────────────────────────────────────────────────────────

def human_move_to(x, y, speed_factor=1.0):
    """Move mouse with organic bezier path. speed_factor < 1 = slower, > 1 = faster."""
    sx, sy = mouse_pos()
    dist = math.hypot(x - sx, y - sy)
    if dist < 2:
        return

    steps = max(14, int(dist / 10))
    base_time = (0.020 + (dist / 3200) ** 0.6) * random.uniform(0.85, 1.15)
    total_time = base_time / max(speed_factor, 0.2)

    overshoot = random.uniform(0.0, 0.06) if dist > 60 else 0.0
    ox = x + (x - sx) * overshoot
    oy = y + (y - sy) * overshoot
    path = bezier_path(sx, sy, ox, oy, n=steps)

    for i, (px, py) in enumerate(path):
        t = i / max(steps, 1)
        # Slow start → gradually faster (ease-in: cubic ramp)
        speed_mult = 0.15 + 1.6 * (t ** 1.8)
        step_dt = (total_time / steps) / max(speed_mult, 0.12) * random.uniform(0.88, 1.12)
        pyautogui.moveTo(int(px), int(py), duration=0)
        time.sleep(max(0.001, step_dt))

    if overshoot > 0:
        cx, cy = mouse_pos()
        for i in range(1, 5):
            t = i / 4
            nx = lerp(cx, x, ease_out_cubic(t))
            ny = lerp(cy, y, ease_out_cubic(t))
            pyautogui.moveTo(int(nx), int(ny), duration=0)
            time.sleep(0.008)
    pyautogui.moveTo(x, y, duration=0)


def human_type_visible(text, target_pos=None):
    """Type char-by-char: starts slow, gradually speeds up."""
    for i, char in enumerate(text):
        pyautogui.press(char) if len(char) == 1 and char.isprintable() else pyautogui.write(char)
        progress = i / max(len(text) - 1, 1)
        # Slow start → faster: delay shrinks as progress increases
        speed_mult = 0.4 + 1.4 * (progress ** 1.5)
        delay = TYPE_CHAR_INTERVAL / max(speed_mult, 0.2) * random.uniform(0.8, 1.2)
        time.sleep(max(0.015, delay))
    time.sleep(0.15)

# ─────────────────────────────────────────────────────────────
# Action helpers
# ─────────────────────────────────────────────────────────────

def click_with_preview(x, y, label=None, double=False, speed_factor=1.0):
    set_preview(x, y, label)
    time.sleep(PREVIEW_SEC * random.uniform(0.8, 1.2))
    human_move_to(x, y, speed_factor=speed_factor)
    # Dwell at target — "I'm about to click here"
    time.sleep(random.uniform(0.15, 0.25))
    if double:
        pyautogui.doubleClick()
    else:
        pyautogui.click()
    clear_preview()
    # Record in session trail
    with state_lock:
        state["session_clicks"].append((x, y, now()))

def scroll_action(x, y, dy, speed_factor=1.0):
    """Scroll with tempo: repeated scrolls get faster."""
    human_move_to(x, y, speed_factor=speed_factor)
    time.sleep(random.uniform(0.04, 0.10) / max(speed_factor, 0.3))

    direction = -1 if dy > 0 else 1
    total_clicks = max(8, min(abs(dy) * 3, 30))
    bursts = random.randint(3, 6)
    clicks_per_burst = max(1, total_clicks // bursts)

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
# DOM injection — VERIFIED WORKING syntax
# ─────────────────────────────────────────────────────────────

def _chrome_js(js: str):
    """Fire-and-forget JS execution in Chrome (async)."""
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


def _chrome_js_sync(js: str, timeout=1.5) -> str:
    """Execute JS in Chrome and return result (sync)."""
    safe = js.replace('\\', '\\\\').replace('"', '\\"')
    apple = (
        'tell application "Google Chrome"\n'
        '    tell active tab of front window\n'
        f'        execute javascript "{safe}"\n'
        '    end tell\n'
        'end tell'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", apple],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _poll_reading_done():
    """Background thread: poll DOM reading state and flip to 'thinking' when done.
    Initial 1s delay to let reading animation start before polling."""
    time.sleep(1.0)
    for _ in range(60):   # poll for up to 30 seconds
        with state_lock:
            if not state.get("reasoning", False):
                return
        try:
            result = _chrome_js_sync("window._crTimer===null?'done':'reading'", timeout=1.0)
            if result == "done":
                with state_lock:
                    state["reading_done"] = True
                return
        except Exception:
            pass
        time.sleep(0.5)
    # Timed out — just mark as done
    with state_lock:
        state["reading_done"] = True


# ── DOM legibility: continuous reading animation ───────────────

def dom_start_reading():
    """Start reading animation. Skips elements already read (marked with data-cr-read).
    Tracks read state via data attribute so re-screenshots only highlight new content."""
    js = (
        "(function(){"
        "if(window._crTimer)clearTimeout(window._crTimer);"
        "if(window._crEls)window._crEls.forEach(function(el){"
        "el.style.backgroundColor='';el.style.boxShadow='';el.style.borderLeft='';el.style.transition='';"
        "});"
        "var els=document.querySelectorAll('p,li,h1,h2,h3,h4,td,th,figcaption,blockquote,dt,dd,pre');"
        "var vh=window.innerHeight;"
        "var visible=[];"
        "els.forEach(function(el){"
        "var r=el.getBoundingClientRect();"
        "if(r.top>-10&&r.top<vh&&r.height>8&&r.height<500"
        "&&!el.getAttribute('data-cr-read'))visible.push(el);"
        "});"
        "window._crEls=visible;"
        "window._crIdx=0;"
        "window._crStopped=false;"
        "function step(){"
        "if(window._crStopped)return;"
        "var els=window._crEls;"
        "var idx=window._crIdx;"
        "if(!els||idx>=els.length){"
        "els.forEach(function(e){"
        "e.style.transition='background-color 0.6s ease,box-shadow 0.4s ease,border-left 0.4s ease';"
        "e.style.backgroundColor='';e.style.boxShadow='';e.style.borderLeft='';"
        "e.setAttribute('data-cr-read','1');"
        "});"
        "window._crTimer=null;"
        "return;"
        "}"
        "if(idx>0){"
        "var prev=els[idx-1];"
        "prev.style.transition='background-color 0.5s ease,box-shadow 0.4s ease,border-left 0.3s ease';"
        "prev.style.backgroundColor='rgba(255,195,60,0.05)';"
        "prev.style.boxShadow='none';"
        "prev.style.borderLeft='3px solid rgba(255,170,30,0.12)';"
        "prev.setAttribute('data-cr-read','1');"
        "}"
        "var cur=els[idx];"
        "cur.style.transition='background-color 0.25s ease,box-shadow 0.25s ease,border-left 0.15s ease';"
        "cur.style.backgroundColor='rgba(255,195,60,0.22)';"
        "cur.style.boxShadow='inset 0 -2px 0 rgba(255,170,30,0.5)';"
        "cur.style.borderLeft='3px solid rgba(255,170,30,0.65)';"
        "cur.scrollIntoView({behavior:'smooth',block:'nearest'});"
        "var len=(cur.textContent||'').length;"
        "var dwell=Math.min(Math.max(300,len*4),1400);"
        "window._crIdx=idx+1;"
        "window._crTimer=setTimeout(step,dwell);"
        "}"
        "step();"
        "return visible.length;"
        "})()"
    )
    _chrome_js(js)


def dom_stop_reading():
    """Stop reading animation and fade out highlights. Already-read markers persist."""
    js = (
        "(function(){"
        "window._crStopped=true;"
        "if(window._crTimer){clearTimeout(window._crTimer);window._crTimer=null;}"
        "if(window._crEls){"
        "window._crEls.forEach(function(el){"
        "el.style.transition='background-color 0.6s ease,box-shadow 0.4s ease,border-left 0.4s ease';"
        "el.style.backgroundColor='';el.style.boxShadow='';el.style.borderLeft='';"
        "el.setAttribute('data-cr-read','1');"
        "});"
        "}"
        "})()"
    )
    _chrome_js(js)


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
    _chrome_js(js)


# ── DOM legibility: click target preview ───────────────────────

def dom_click_preview(sx: int, sy: int, ms=600):
    """Before clicking: outline + subtle glow on target element."""
    js = (
        "(function(){"
        f"var ex={sx}-window.screenX,"
        f"ey={sy}-window.screenY-(window.outerHeight-window.innerHeight);"
        "var el=document.elementFromPoint(ex,ey);"
        "if(!el||el.tagName==='HTML'||el.tagName==='BODY')return;"
        "el.style.transition='outline 0.15s ease, box-shadow 0.15s ease';"
        "el.style.outline='2px solid rgba(60,200,255,0.75)';"
        "el.style.outlineOffset='2px';"
        "el.style.boxShadow='0 0 16px 4px rgba(60,200,255,0.3)';"
        "setTimeout(function(){"
        "  el.style.outline='';"
        "  el.style.outlineOffset='';"
        "  el.style.boxShadow='';"
        f"}},{ms});"
        "})()"
    )
    _chrome_js(js)


# ── DOM legibility: click ripple ───────────────────────────────

def dom_click_ripple(sx: int, sy: int, ms=500):
    """After clicking: expanding ring from click point in the DOM."""
    js = (
        "(function(){"
        f"var ex={sx}-window.screenX,"
        f"ey={sy}-window.screenY-(window.outerHeight-window.innerHeight);"
        "if(!document.getElementById('_cr_style')){"
        "  var s=document.createElement('style');"
        "  s.id='_cr_style';"
        "  s.textContent='@keyframes _cr{0%{transform:scale(0.2);opacity:0.8}100%{transform:scale(2.5);opacity:0}}';"
        "  document.head.appendChild(s);"
        "}"
        "var d=document.createElement('div');"
        "d.style.cssText='position:fixed;left:'+(ex-25)+'px;top:'+(ey-25)+'px;"
        "width:50px;height:50px;border-radius:50%;"
        "border:2px solid rgba(60,200,255,0.7);"
        "pointer-events:none;z-index:999999;"
        "animation:_cr 0.5s ease-out forwards;';"
        "document.body.appendChild(d);"
        f"setTimeout(function(){{if(d.parentNode)d.parentNode.removeChild(d);}},{ms});"
        "})()"
    )
    _chrome_js(js)


# ── DOM legibility: scroll scanning (highlight headings/links) ─

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
    _chrome_js(js)


# ── DOM legibility: focus ring for screenshot ──────────────────

def dom_focus_ring(sx: int, sy: int, ms=1200):
    """Dashed outline on element under cursor during screenshot analysis."""
    js = (
        "(function(){"
        f"var ex={sx}-window.screenX,"
        f"ey={sy}-window.screenY-(window.outerHeight-window.innerHeight);"
        "var el=document.elementFromPoint(ex,ey);"
        "if(!el||el.tagName==='HTML'||el.tagName==='BODY')return;"
        "el.style.transition='outline 0.2s ease';"
        "el.style.outline='1.5px dashed rgba(255,200,60,0.55)';"
        "el.style.outlineOffset='3px';"
        "setTimeout(function(){"
        "  el.style.outline='';"
        "  el.style.outlineOffset='';"
        f"}},{ms});"
        "})()"
    )
    _chrome_js(js)

# ─────────────────────────────────────────────────────────────
# Execute agent action + DOM legibility
# ─────────────────────────────────────────────────────────────

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
        with state_lock:
            state["screenshot_action"] = True
        mx, my = mouse_pos()
        # DOM reading is handled by task_loop (start before API, stop after)
        dom_focus_ring(mx, my, ms=1200)

    elif action == "mouse_move":
        x, y = sc(params["coordinate"])
        set_preview(x, y)
        time.sleep(PREVIEW_SEC)
        human_move_to(x, y, speed_factor=base_speed)
        clear_preview()

    elif action == "left_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        dom_click_preview(x, y)
        time.sleep(0.12)
        click_with_preview(x, y, speed_factor=base_speed)
        dom_click_ripple(x, y)

    elif action == "double_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        dom_click_preview(x, y, ms=800)
        time.sleep(0.12)
        click_with_preview(x, y, double=True, speed_factor=base_speed)
        dom_click_ripple(x, y)

    elif action == "right_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        dom_click_preview(x, y)
        human_move_to(x, y, speed_factor=base_speed)
        pyautogui.rightClick()

    elif action == "type":
        text = params["text"]
        activate_chrome()
        tx, ty = mouse_pos()
        short = text[:28] + ("…" if len(text) > 28 else "")
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

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    SYSTEM_PROMPT = (
        f"You are a macOS computer-use agent. "
        f"Display: {DISPLAY_W}×{DISPLAY_H}. Origin top-left. "
        f"Chrome is showing the UIST 2026 website.\n"
        "Rules:\n"
        "- Before each tool call, write ONE short sentence (max 10 words) "
        "saying what you will do, in first person.\n"
        "- Use 'command' for macOS shortcuts.\n"
        "- Never take two screenshots in a row.\n"
        "- When scrolling, use delta_y of 5–8."
    )

    goal = (
        "Chrome is showing the UIST 2026 website.\n"
        "Your task: find and read the submission formatting guidelines, "
        "then write a concise summary.\n\n"
        "Steps:\n"
        "1. Look for a link to 'Call for Papers', 'Author Guide', 'Submissions', "
        "or 'Formatting Guidelines'. Click it.\n"
        "2. On that page, scroll down and READ the content carefully. "
        "Look for formatting requirements "
        "(page limits, column format, template, anonymization, etc).\n"
        "3. When you have read enough, stop using tools and write a concise "
        "summary (3-5 bullet points) of the key formatting requirements. "
        "Do NOT use any tools in your final response — just write the summary text."
    )

    # ── Phase 1: Pre-navigation (pyautogui, fast + visible) ──
    print("[CU] Phase 1: Navigating to UIST 2026…", file=sys.stderr)
    activate_chrome()
    time.sleep(0.3)

    pyautogui.keyDown("command")
    time.sleep(0.05)
    pyautogui.press("l")
    time.sleep(0.05)
    pyautogui.keyUp("command")
    time.sleep(0.4)
    human_type_visible("uist.acm.org/2026")
    time.sleep(0.1)
    pyautogui.press("return")
    time.sleep(3.5)

    set_progress(1, 4, "Navigate to site")

    # ── Phase 2: Agent reads page + generates summary ──
    print("[CU] Phase 2: Agent reading guidelines…", file=sys.stderr)
    set_progress(2, 4, "Read guidelines")

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
        "type": "computer_20250124",
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

        with state_lock:
            state["reasoning"]          = True
            state["reasoning_start_ts"] = now()
            state["reasoning_end_ts"]   = None
            state["reasoning_text"]     = ""
            state["reading_done"]       = False

        dom_start_reading()
        threading.Thread(target=_poll_reading_done, daemon=True).start()

        response = client.beta.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            betas=["computer-use-2025-01-24"],
        )

        dom_stop_reading()

        raw = " ".join(
            b.text.strip() for b in response.content
            if getattr(b, "type", "") == "text" and b.text.strip()
        )
        thought = first_sentence(raw)

        with state_lock:
            state["reasoning"]        = False
            state["reasoning_end_ts"] = now()
            state["reasoning_text"]   = thought

        messages.append({"role": "assistant", "content": response.content})

        # end_turn = agent wrote summary (no more tool calls)
        if response.stop_reason == "end_turn":
            summary_text = raw
            print(f"[CU] Agent summary: {summary_text[:100]}…", file=sys.stderr)
            set_progress(3, 4, "Summary ready")
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            action = block.input.get("action", "")
            print(f"[CU] action={action} params={block.input}", file=sys.stderr)

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

    # ── Phase 3: Open Google Doc + paste summary ──
    if not summary_text:
        summary_text = "UIST 2026 Formatting Guidelines\n(Agent could not retrieve summary)"

    print("[CU] Phase 3: Pasting summary to Google Doc…", file=sys.stderr)
    set_progress(4, 4, "Paste to Doc")

    activate_chrome()
    time.sleep(0.2)
    pyautogui.hotkey("command", "t")       # new tab
    time.sleep(0.5)
    pyautogui.keyDown("command")
    time.sleep(0.05)
    pyautogui.press("l")
    time.sleep(0.05)
    pyautogui.keyUp("command")
    time.sleep(0.4)
    human_type_visible("docs.new")
    time.sleep(0.1)
    pyautogui.press("return")
    time.sleep(5.0)                        # wait for Google Doc to load

    # Click in the doc body and paste the summary
    pyautogui.click(SCREEN_W // 2, SCREEN_H // 2)
    time.sleep(0.8)
    pyperclip.copy(summary_text)
    pyautogui.hotkey("command", "v")
    time.sleep(1.0)

    print("[CU] Done!", file=sys.stderr)
    dom_stop_reading()
    subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])

# ─────────────────────────────────────────────────────────────
# Overlay — stripped to legibility only
# ─────────────────────────────────────────────────────────────

class OverlayView(NSView):
    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        try:
            NSColor.clearColor().set()
            NSBezierPath.fillRect_(rect)
            self.draw_vignette()
            self.draw_session_trail()
            self.draw_trail()
            self.draw_cursor()
            self.draw_preview()
            self.draw_progress_bar()
            self.draw_goal_bubble()
            self.draw_reasoning_bubble()
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

    # ── Vignette (soft "perceiving" effect) ────────────────────
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
            x, y      = state["cursor_pos"]
            is_camera = state.get("screenshot_action", False)
            reasoning = state.get("reasoning", False)

        if is_camera:
            # Soft pulsing iris — "seeing"
            pulse = 0.5 + 0.5 * math.sin(now() * 8)
            self.glow(x, y, 20, self.cyan, 0.15 * pulse, steps=5)
            self.stroke_circle(x, y, 14, 1.2, self.cyan, 0.55 * pulse)
            self.draw_circle(x, y, 4, self.cyan, 0.85)
            self.draw_circle(x, y, 2, self.white, 0.9)
        elif reasoning:
            # Breathing glow — "thinking"
            pulse = 0.3 + 0.7 * math.sin(now() * 3.5)
            self.glow(x, y, 16, self.soft_blue, 0.15 * pulse, steps=4)
            self.draw_circle(x, y, 3.5, self.soft_blue, 0.8)
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

        # Smooth glowing trajectory line (not dashed — organic)
        for i in range(1, n_show):
            af = i / len(path)
            alpha = (0.05 + 0.25 * af) * ease_out_cubic(prog)
            width = 1.5 + 1.5 * af
            self.draw_line(
                path[i-1][0], path[i-1][1],
                path[i][0],   path[i][1],
                width, self.white, alpha
            )

        # Moving dot along path
        idx = min(int(prog * len(path) * 0.9), len(path) - 1)
        px, py = path[idx]
        self.glow(px, py, 10, self.white, 0.25 * ease_out_cubic(prog), steps=4)
        self.draw_circle(px, py, 3, self.white, 0.7)

        # Target: big soft breathing glow (not crosshair)
        tx, ty = target
        breath = 0.6 + 0.4 * math.sin(t * 6)
        target_a = 0.30 * ease_out_cubic(prog)
        self.glow(tx, ty, 40, self.white, 0.06 * target_a * breath, steps=6)
        self.glow(tx, ty, 20, self.white, 0.10 * target_a * breath, steps=5)
        self.stroke_circle(tx, ty, 22, 1.2, self.white, target_a * 0.5 * breath)

    # ── Progress bar ───────────────────────────────────────────
    @objc.python_method
    def draw_progress_bar(self):
        with state_lock:
            step  = state.get("progress_step", 0)
            total = state.get("progress_total", 0)
            label = state.get("progress_label", "")
        if total <= 0 or step <= 0:
            return

        bar_h = 2.5
        bar_y = 2
        progress = min(step / total, 1.0)

        self.draw_rounded_rect_fill(0, bar_y, SCREEN_W, bar_h, 1, self.white, 0.05)
        fill_w = max(4, int(SCREEN_W * progress))
        self.draw_rounded_rect_fill(0, bar_y, fill_w, bar_h, 1, self.cyan, 0.40)

        step_text = f"{step}/{total}  {label}"
        try:
            cx, cy = to_cocoa(SCREEN_W - 185, 16)
            attrs = NSMutableDictionary.dictionary()
            attrs[NSFontAttributeName] = NSFont.monospacedSystemFontOfSize_weight_(9.5, 0.0)
            attrs[NSForegroundColorAttributeName] = self.white(0.45)
            astr = NSAttributedString.alloc().initWithString_attributes_(step_text, attrs)
            astr.drawAtPoint_((cx, cy))
        except Exception:
            pass

    # ── Glass bubble (for goal + reasoning) ────────────────────
    @objc.python_method
    def draw_speech_bubble(self, sx, sy, text, color_fn, alpha,
                           max_chars=44, above=True, slide=1.0):
        if not text or alpha < 0.02:
            return
        words = text.split()
        lines, cur = [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if len(trial) > max_chars and cur:
                lines.append(cur); cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        lines = lines[:3]

        pad_x, pad_y, line_h, cw = 14, 10, 16, 6.6
        box_w = min(max(len(l) for l in lines) * cw + pad_x * 2, 360)
        box_h = len(lines) * line_h + pad_y * 2
        tail_h = 8

        tip_cx, tip_cy = to_cocoa(sx, sy)
        offset = BUBBLE_SLIDE_PX * (1.0 - ease_out_cubic(slide))
        bx = max(8, min(tip_cx - box_w / 2, SCREEN_W - box_w - 8))
        by = (tip_cy + tail_h + offset) if above else (tip_cy - box_h - tail_h + offset)

        # Background
        rect = NSMakeRect(bx, by, box_w, box_h)
        bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 9, 9)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.10, 0.14, alpha * 0.94).set()
        bg.fill()
        bg.setLineWidth_(0.8)
        color_fn(alpha * 0.45).set()
        bg.stroke()

        # Tail
        tail_tip = tip_cy - 2 if above else tip_cy + 2
        tail_base = by if above else by + box_h
        tail = NSBezierPath.bezierPath()
        tail.moveToPoint_((tip_cx - 5, tail_base))
        tail.lineToPoint_((tip_cx, tail_tip))
        tail.lineToPoint_((tip_cx + 5, tail_base))
        tail.closePath()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.10, 0.14, alpha * 0.94).set()
        tail.fill()

        # Text
        attrs = NSMutableDictionary.dictionary()
        attrs[NSFontAttributeName] = NSFont.monospacedSystemFontOfSize_weight_(10.5, 0.0)
        attrs[NSForegroundColorAttributeName] = NSColor.colorWithCalibratedWhite_alpha_(0.90, alpha)
        for i, line in enumerate(lines):
            ty = by + box_h - pad_y - (i + 1) * line_h + 3
            astr = NSAttributedString.alloc().initWithString_attributes_(line, attrs)
            astr.drawAtPoint_((bx + pad_x, ty))

    # ── Goal bubble ────────────────────────────────────────────
    @objc.python_method
    def draw_goal_bubble(self):
        with state_lock:
            ts   = state.get("goal_ts")
            text = state.get("goal_text", "")
        if not ts or not text:
            return
        age = now() - ts
        if age > 9.5:
            with state_lock:
                state["goal_ts"] = None
            return
        alpha = min(age / 0.3, 1.0)
        slide = min(age / 0.3, 1.0)
        if age > 8.0:
            alpha *= 1.0 - (age - 8.0) / 1.5
        self.draw_speech_bubble(SCREEN_W // 2, SCREEN_H - 80, text,
                                self.cyan, alpha * 0.88, max_chars=56,
                                above=True, slide=slide)

    # ── Reasoning bubble ───────────────────────────────────────
    @objc.python_method
    def draw_reasoning_bubble(self):
        with state_lock:
            reasoning    = state.get("reasoning", False)
            text         = state.get("reasoning_text", "")
            start_ts     = state.get("reasoning_start_ts")
            end_ts       = state.get("reasoning_end_ts")
            reading_done = state.get("reading_done", False)
            cx, cy       = state["cursor_pos"]

        t = now()
        if reasoning and start_ts:
            age = t - start_ts
            alpha = min(age / BUBBLE_FADE_IN, 1.0) * 0.85
            slide = min(age / BUBBLE_FADE_IN, 1.0)
            dots = "·" * (int(t * 3) % 4)
            display = f"thinking{dots}" if reading_done else f"reading{dots}"
        elif not reasoning and text and end_ts:
            age = t - end_ts
            if age > 2.0 + BUBBLE_FADE_OUT:
                with state_lock:
                    state["reasoning_text"] = ""
                return
            alpha = 0.85 if age < 2.0 else 0.85 * (1.0 - (age - 2.0) / BUBBLE_FADE_OUT)
            slide = 1.0
            display = text
        else:
            return
        if not display:
            return
        self.draw_speech_bubble(cx, cy, display, self.soft_blue, alpha,
                                max_chars=50, above=True, slide=slide)

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
    rect   = NSMakeRect(0, 0, SCREEN_W, SCREEN_H)
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
    overlay_view = OverlayView.alloc().initWithFrame_(rect)
    overlay_view.setWantsLayer_(True)
    window.setContentView_(overlay_view)
    window.orderFront_(None)
    window.setCanHide_(False)
    return window

def setup_esc_listener():
    def on_key(event):
        try:
            if event.keyCode() == 53:
                print("[overlay] ESC – stopping.", file=sys.stderr)
                dom_stop_reading()
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