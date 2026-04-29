from __future__ import annotations

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

import mss
import pyperclip
import tempfile
from PIL import Image

import objc
import pyautogui
from dotenv import load_dotenv
import anthropic
from elevenlabs import ElevenLabs

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBorderlessWindowMask,
    NSColor,
    NSEvent,
    NSSound,
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
# Recording (optional — enabled with --record flag)
# ─────────────────────────────────────────────────────────────

from workflow_recorder import WorkflowRecorder  # noqa: E402

_recorder: WorkflowRecorder | None = None
_record_enabled: bool = False

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


# ── Sound effects ─────────────────────────────────────────────
_ns_sounds: dict = {}  # path -> NSSound, lazy-init after NSApp starts

def _play(path: str, volume: float = 1.0):
    """Play a system sound via NSSound (low latency, lazy-init)."""
    if path not in _ns_sounds:
        s = NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
        if s:
            s.setVolume_(volume)
        _ns_sounds[path] = s
    s = _ns_sounds.get(path)
    if s:
        s.stop()
        s.play()

# ElevenLabs SFX: pre-generated mp3 paths (filled by _init_sfx in background)
_sfx: dict = {}  # "typing" | "reading" -> tmp mp3 path

def _init_sfx():
    """Pre-generate ElevenLabs sound effects at startup (runs in background thread)."""
    sounds = {
        "typing":  "quick mechanical keyboard clicks, crisp and light",
        "reading": "barely audible soft ambient tick, like a distant clock, very subtle and calm",
    }
    for key, prompt in sounds.items():
        try:
            audio = _eleven.text_to_sound_effects.convert(
                text=prompt, duration_seconds=0.5, prompt_influence=0.2
            )
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                for chunk in audio:
                    f.write(chunk)
                _sfx[key] = f.name
            print(f"[SFX] generated: {key}", file=sys.stderr)
        except Exception as e:
            print(f"[SFX] {key} failed: {e}", file=sys.stderr)

_sfx_afplay: dict = {}  # key -> current Popen, for stopping overlaps

def _play_sfx_or_system(key: str, fallback_path: str, volume: float = 1.0):
    """Play ElevenLabs SFX if ready, else fall back to system sound."""
    if key in _sfx:
        # kill previous instance of same sfx so they don't stack
        prev = _sfx_afplay.get(key)
        if prev and prev.poll() is None:
            prev.terminate()
        vol = "0.15" if key == "reading" else "0.4"
        _sfx_afplay[key] = subprocess.Popen(["afplay", "-v", vol, _sfx[key]])
    else:
        _play(fallback_path, volume)

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
    "speech_done_ts":     None,   # set by TTS thread when audio finishes
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
    "cursor_state":       "default",  # "default" | "reading" | "thinking" | "clicking"
    "last_thought":       "",          # Claude's raw text (for high-stakes fallback)
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

_FILLER_PREFIXES = (
    "excellent", "great", "sure", "of course", "certainly", "perfect",
    "okay", "ok", "absolutely", "good", "wonderful", "alright", "got it",
    "understood", "noted", "i see", "i understand",
)

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


def meaningful_thought(text, max_chars=80):
    """Return one concise, meaningful sentence from Claude's response."""
    if not text:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    for s in sentences:
        low = s.strip().lower()
        is_filler = any(low.startswith(f) for f in _FILLER_PREFIXES) and len(s) < 60
        if not is_filler:
            s = s.strip()
            if len(s) > max_chars:
                cut = s[:max_chars].rfind(' ')
                s = s[:cut] + "…" if cut > 10 else s[:max_chars] + "…"
            return s
    # fallback: last sentence, truncated
    s = sentences[-1].strip()
    if len(s) > max_chars:
        cut = s[:max_chars].rfind(' ')
        s = s[:cut] + "…" if cut > 10 else s[:max_chars] + "…"
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

def set_system_cursor(cursor_state: str):
    """Request a system cursor change. Applied on the main thread via state dict."""
    with state_lock:
        state["cursor_state"] = cursor_state

def set_progress(step, total, label=""):
    play_sound("Tink.aiff")
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
    """Type char-by-char: starts slow, gradually speeds up, with keyboard click sounds."""
    for i, char in enumerate(text):
        pyautogui.press(char) if len(char) == 1 and char.isprintable() else pyautogui.write(char)
        _play_sfx_or_system("typing", "/System/Library/Sounds/Tock.aiff", 0.25)
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
    total_clicks = max(8, min(abs(dy) * 3, 35))
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


def _reading_sound_loop():
    """Play a soft pop sound periodically while DOM reading animation is active."""
    time.sleep(0.3)  # let reading start first
    while True:
        with state_lock:
            done = state.get("reading_done", False)
            reasoning = state.get("reasoning", False)
        if done or not reasoning:
            break
        _play_sfx_or_system("reading", "/System/Library/Sounds/Tock.aiff", 0.06)
        time.sleep(1.1)  # subtle background rhythm, not distracting


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
                play_sound("Pop.aiff")
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


# ── Stakes detection ───────────────────────────────────────────

_HIGH_STAKES_KEYWORDS = {
    "buy", "add to cart", "checkout", "order now", "purchase",
    "place order", "submit", "send", "confirm", "delete", "remove",
    "pay", "proceed", "complete purchase", "sign up", "subscribe",
}

def _is_high_stakes(label: str) -> bool:
    low = label.lower()
    return any(kw in low for kw in _HIGH_STAKES_KEYWORDS)



# ── DOM legibility: click target preview ───────────────────────

def _get_element_label(sx: int, sy: int) -> str:
    """Get readable label of DOM element at screen position for narration."""
    js = (
        "(function(){"
        f"var ex={sx}-window.screenX,"
        f"ey={sy}-window.screenY-(window.outerHeight-window.innerHeight);"
        "var el=document.elementFromPoint(ex,ey);"
        "if(!el)return '';"
        "var tag=el.tagName;"
        "if(tag==='STYLE'||tag==='SCRIPT'||tag==='HTML'||tag==='BODY')return '';"
        "var t=(el.getAttribute('aria-label')||el.getAttribute('value')||el.getAttribute('title')||el.getAttribute('alt')||'').trim();"
        "if(!t)t=(el.innerText||el.textContent||'').trim();"
        "if(!t&&el.parentElement){var p=el.parentElement;"
        "t=(p.getAttribute('aria-label')||p.innerText||p.textContent||'').trim();}"
        "return t.replace(/\\s+/g,' ').slice(0,50);"
        "})()"
    )
    try:
        return _chrome_js_sync(js, timeout=1.0)
    except Exception:
        return ""


def orbit_mouse(cx: int, cy: int, stop_event: threading.Event, radius: int = 55, min_revolutions: float = 0.25):
    """Orbit mouse around (cx, cy) while TTS plays, then return smoothly.
    min_revolutions: minimum full circles to complete before stopping (e.g. 1.0 = full circle)."""
    sx, sy = mouse_pos()
    angle = math.atan2(sy - cy, sx - cx)
    entry_x = cx + int(radius * math.cos(angle))
    entry_y = cy + int(radius * math.sin(angle))
    human_move_to(entry_x, entry_y, speed_factor=2.0)

    traversed = 0.0
    min_angle = min_revolutions * 2 * math.pi
    step = 0.07
    while not stop_event.is_set() or traversed < min_angle:
        px = cx + int(radius * math.cos(angle))
        py = cy + int(radius * math.sin(angle))
        pyautogui.moveTo(px, py, duration=0)
        angle += step
        traversed += step
        time.sleep(0.06)
    human_move_to(cx, cy, speed_factor=3.0)


def dom_click_preview(sx: int, sy: int, ms=1200, high_stakes=False):
    """Before clicking: dramatic grow + warm glow + expanding radar ring.
    high_stakes=True uses red/orange warning colors + double ring."""
    if high_stakes:
        el_color  = "rgba(255,70,50"    # red-orange for warning
        ring_color = "rgba(255,70,50"
        scale_peak = "1.35"
        ring_border = "4px"
        el_anim_dur = "1.4s"
        ms = max(ms, 2500)
    else:
        el_color  = "rgba(255,195,60"   # amber for normal
        ring_color = "rgba(255,195,60"
        scale_peak = "1.25"
        ring_border = "3px"
        el_anim_dur = "1.1s"

    js = (
        "(function(){"
        f"var ex={sx}-window.screenX,"
        f"ey={sy}-window.screenY-(window.outerHeight-window.innerHeight);"
        "var el=document.elementFromPoint(ex,ey);"
        "if(!el||el.tagName==='HTML'||el.tagName==='BODY')return;"
        # Inject / update keyframes
        "var s=document.getElementById('_cr_attn_style');"
        "if(!s){s=document.createElement('style');s.id='_cr_attn_style';document.head.appendChild(s);}"
        f"s.textContent='"
        "@keyframes _cr_attn{"
        f"0%  {{transform:scale(1);    filter:brightness(1)   drop-shadow(0 0 0px  {el_color},0));}}"
        f"15% {{transform:scale({scale_peak}); filter:brightness(1.8) drop-shadow(0 0 24px {el_color},1.0));}}"
        f"40% {{transform:scale(1.18); filter:brightness(1.5) drop-shadow(0 0 16px {el_color},0.8));}}"
        f"65% {{transform:scale(1.22); filter:brightness(1.7) drop-shadow(0 0 20px {el_color},0.9));}}"
        f"100%{{transform:scale(1);    filter:brightness(1)   drop-shadow(0 0 0px  {el_color},0));}}"
        "}"
        "@keyframes _cr_ring{"
        "0%  {opacity:1.0;transform:scale(1.0);}"
        "100%{opacity:0.0;transform:scale(1.7);}"
        "}"
        "@keyframes _cr_ring2{"
        "0%  {opacity:0.8;transform:scale(1.0);}"
        "100%{opacity:0.0;transform:scale(2.1);}"
        "}"
        "';"
        # Element glow
        "el.style.transformOrigin='center';"
        f"el.style.animation='_cr_attn {el_anim_dur} ease-in-out';"
        f"setTimeout(function(){{el.style.animation='';}},{ms});"
        # Ring 1
        "var rect=el.getBoundingClientRect();"
        "var ring=document.createElement('div');"
        "ring.style.cssText='position:fixed;"
        "left:'+(rect.left-12)+'px;top:'+(rect.top-12)+'px;"
        "width:'+(rect.width+24)+'px;height:'+(rect.height+24)+'px;"
        f"border:{ring_border} solid {ring_color},0.95);"
        "border-radius:10px;pointer-events:none;z-index:2147483647;"
        "animation:_cr_ring 1.0s ease-out forwards;';"
        "document.body.appendChild(ring);"
        f"setTimeout(function(){{if(ring.parentNode)ring.parentNode.removeChild(ring);}},{ms});"
    )

    if high_stakes:
        # Second ring with delay — double ping for warning
        js += (
            "var ring2=document.createElement('div');"
            "ring2.style.cssText='position:fixed;"
            "left:'+(rect.left-16)+'px;top:'+(rect.top-16)+'px;"
            "width:'+(rect.width+32)+'px;height:'+(rect.height+32)+'px;"
            f"border:3px solid {ring_color},0.7);"
            "border-radius:12px;pointer-events:none;z-index:2147483646;"
            "animation:_cr_ring2 1.2s 0.5s ease-out forwards;opacity:0;';"
            "document.body.appendChild(ring2);"
            f"setTimeout(function(){{if(ring2.parentNode)ring2.parentNode.removeChild(ring2);}},{ms});"
            # Red text + underline on the element itself
            "var _origColor=el.style.color,_origDeco=el.style.textDecoration,_origWeight=el.style.fontWeight;"
            "el.style.color='#e8002d';el.style.textDecoration='underline';el.style.fontWeight='bold';"
            f"setTimeout(function(){{el.style.color=_origColor;el.style.textDecoration=_origDeco;el.style.fontWeight=_origWeight;}},{ms});"
        )

    js += "})()"
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


_eleven = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY", ""))

def speak(text: str):
    """Blocking TTS — shows bubble, waits for audio to finish, then continues."""
    with state_lock:
        state["reasoning_text"] = text
        state["reasoning_end_ts"] = now()
        state["speech_done_ts"] = None   # bubble stays visible while None
        state["reasoning"] = False
    tmp = None
    try:
        audio = _eleven.text_to_speech.convert(
            text=text,
            voice_id="XrExE9yKIg1WjnnlVkGX",  # Matilda
            model_id="eleven_turbo_v2_5",
            output_format="mp3_44100_128",
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            for chunk in audio:
                f.write(chunk)
            tmp = f.name
        subprocess.run(["afplay", tmp])
    except Exception as e:
        print(f"[TTS] error: {e}, falling back to say", file=sys.stderr)
        subprocess.run(["say", "-v", "Samantha", "-r", "210", text])
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass
        with state_lock:
            state["speech_done_ts"] = now()  # bubble starts fade timer

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
        pass  # DOM reading handled by task_loop

    elif action == "mouse_move":
        x, y = sc(params["coordinate"])
        human_move_to(x, y, speed_factor=base_speed)

    elif action == "left_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        label = _get_element_label(x, y)
        high_stakes = _is_high_stakes(label)
        # Fallback: check if Claude's narration explicitly says "click [high-stakes-word]"
        if not high_stakes:
            with state_lock:
                last_thought = state.get("last_thought", "").lower()
            for kw in _HIGH_STAKES_KEYWORDS:
                idx = last_thought.find(kw)
                if idx >= 0 and "click" in last_thought[max(0, idx - 40):idx]:
                    high_stakes = True
                    if not label:
                        label = kw  # use keyword as display label
                    break
        print(f"[CLICK] label={label!r} high_stakes={high_stakes}", file=sys.stderr)
        dom_click_preview(x, y, high_stakes=high_stakes)
        if high_stakes:
            stop_ev = threading.Event()
            orbit_t = threading.Thread(target=orbit_mouse, args=(x, y, stop_ev), kwargs={"min_revolutions": 1.0}, daemon=True)
            orbit_t.start()
            speak(f"Heads up — I'm about to click '{label}'. This may be hard to undo!")
            stop_ev.set()
            orbit_t.join(timeout=5.0)
            time.sleep(1.5)  # grace period after orbit — user can intervene
        elif label:
            stop_ev = threading.Event()
            orbit_t = threading.Thread(target=orbit_mouse, args=(x, y, stop_ev), daemon=True)
            orbit_t.start()
            speak(f"I'll click '{label}'.")
            stop_ev.set()
            orbit_t.join(timeout=1.5)
        else:
            time.sleep(1.0)
        click_with_preview(x, y, speed_factor=base_speed)
        dom_click_ripple(x, y)

    elif action == "double_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        label = _get_element_label(x, y)
        high_stakes = _is_high_stakes(label) if label else False
        dom_click_preview(x, y, high_stakes=high_stakes)
        if high_stakes:
            speak(f"I'm about to double-click '{label}'. This may be hard to undo.")
            time.sleep(3.0)
        elif label:
            stop_ev = threading.Event()
            orbit_t = threading.Thread(target=orbit_mouse, args=(x, y, stop_ev), daemon=True)
            orbit_t.start()
            speak(f"I'll double-click '{label}'.")
            stop_ev.set()
            orbit_t.join(timeout=1.5)
        else:
            time.sleep(1.0)
        click_with_preview(x, y, double=True, speed_factor=base_speed)
        dom_click_ripple(x, y)

    elif action == "right_click":
        x, y = sc(params["coordinate"])
        activate_chrome()
        label = _get_element_label(x, y)
        if label:
            speak(f"Right-clicking '{label}'.")
        dom_click_preview(x, y)
        time.sleep(0.55)
        human_move_to(x, y, speed_factor=base_speed)
        pyautogui.rightClick()

    elif action == "type":
        text = params["text"]
        short = text[:40] + ("…" if len(text) > 40 else "")
        speak(f"Typing: {short}")   # keep — shows what's being entered
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
    threading.Thread(target=_init_sfx, daemon=True).start()

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Task selection ────────────────────────────────────────
    TASKS = {
        # ── Transactional (T1–T3) ──────────────────────────────
        "1": {
            "name": "T1 — Sephora: Foundation, Mascara & Lip Gloss",
            "url":  "google.com",
            "site": "Sephora website",
            "goal": (
                "Your task: find 3 makeup products on Sephora's website.\n\n"
                "Start by browsing Foundation. You will be guided to the next item.\n\n"
                "Preferences (apply to all items):\n"
                "- Prefer hypoallergenic, fragrance-free, or sensitive-skin formulas\n"
                "- Avoid products with known irritants (fragrances, parabens, harsh dyes)\n\n"
                "Browse and add your pick to cart. "
                "Do NOT write your final response yet — you will be told when to do that."
            ),
            "iteration_checkpoints": {
                15: (
                    "Good. Commit to one foundation now — pick the option and add it to cart.\n"
                    "Then navigate to the Mascara section. Browse mascaras, look for "
                    "hypoallergenic or sensitive-eye formulas. Change colors if needed."
                ),
                30: (
                    "Good. Commit to one mascara and add it to cart.\n"
                    "Then navigate to Lip Gloss with color. Browse the lip gloss section, "
                    "prefer fragrance-free or gentle formulas with a tinted/colored finish."
                ),
                45: (
                    "Good. Commit to one lip gloss with color and add it to cart.\n"
                    "You now have all 3 items."
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
                8: (
                    "Good. Choose a gluten-free spaghetti and add it to cart. "
                    "Then search for 'diced tomatoes' — find an organic 14.5 oz option (2 cans)."
                ),
                16: (
                    "Good. Add the organic diced tomatoes (2 cans) to cart. "
                    "Then search for 'fresh basil' and add a bunch."
                ),
                24: (
                    "Good. Add the fresh basil to cart. "
                    "Then search for 'parmesan cheese shredded' and add a 6 oz option."
                ),
                32: (
                    "Good. Add the parmesan to cart. "
                    "Then search for 'extra virgin olive oil' and add a 16 oz bottle."
                ),
                40: (
                    "Good. Add the olive oil to cart. "
                    "Then search for 'garlic' and add one head of garlic."
                ),
                45: (
                    "Good. Add the garlic to cart. All 6 items should now be in the cart. "
                    "Do NOT use any tools — write your final confirmation of what was added."
                ),
            },
        },
        # ── Information Synthesis (S1–S3) ──────────────────────
        "4": {
            "name": "S1 — NY Grad School Financial Aid Comparison (NYU / Columbia / Cornell Tech)",
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
                    "(Verizon, AT&T, T-Mobile), followed by a side-by-side summary table "
                    "with columns: Carrier | Cheapest Unlimited Price | Data Policy | Hotspot | "
                    "International Benefits | Perks | Autopay Requirement | Extra Fees. "
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
                    "(Japan, South Korea, China), followed by a side-by-side summary table "
                    "with columns: Country | Visa Required | Passport Validity | Entry Forms | "
                    "Health Requirements | Cash Limit | Travel Advisory Level. "
                    "Do NOT use any tools — just write the final comparison text now."
                ),
            },
        },
        # ── Optional / warm-up tasks ───────────────────────────
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
            "name": "Zocdoc — Dermatology Appointment (optional)",
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
            "name": "Covered California — Insurance Plan Recommendation (optional)",
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
        _recorder = WorkflowRecorder(task_id=choice)
        _recorder.start(task_name=task["name"], task_goal=task["goal"])

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
        "- To search on Google: click the search box on the page, type your query, press Enter. "
        "Do NOT use the Chrome address bar to search — use the Google search box on screen.\n"
        "- To navigate to a new URL: use command+l, type the URL, press Enter.\n"
        "- Use 'command' for macOS shortcuts.\n"
        "- Never take two screenshots in a row.\n"
        "- When scrolling, use delta_y of 5–8."
    )

    goal = task["goal"]

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

    set_progress(1, 4, f"Navigate to {task['url']}")

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
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": DISPLAY_W,
        "display_height_px": DISPLAY_H,
    }]

    MAX_ITER = 60
    consec_shots = 0
    summary_text = ""
    checkpoints = task.get("iteration_checkpoints", {})

    for iteration in range(MAX_ITER):
        with state_lock:
            if not state["running_demo"]:
                return

        # ── Inject checkpoint instruction at country/topic boundaries ──
        if iteration in checkpoints:
            msg = checkpoints[iteration]
            print(f"[CU] Checkpoint at iter {iteration}: {msg[:60]}…", file=sys.stderr)
            messages.append({"role": "user", "content": msg})

        print(f"[CU] iter {iteration + 1}", file=sys.stderr)

        with state_lock:
            state["reasoning"]          = True
            state["reasoning_start_ts"] = now()
            state["reasoning_end_ts"]   = None
            state["reasoning_text"]     = ""
            state["reading_done"]       = False

        dom_start_reading()
        threading.Thread(target=_poll_reading_done, daemon=True).start()
        threading.Thread(target=_reading_sound_loop, daemon=True).start()

        response = client.beta.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=strip_old_screenshots(messages),
            betas=["computer-use-2025-11-24"],
        )

        dom_stop_reading()

        raw = " ".join(
            b.text.strip() for b in response.content
            if getattr(b, "type", "") == "text" and b.text.strip()
        )
        thought = meaningful_thought(raw)

        if raw:
            print(f"\n[CLAUDE raw] {raw}", file=sys.stderr)
            if _recorder is not None:
                _recorder.log_reasoning(raw)

        # Speak Claude's own reasoning aloud before acting.
        # If Claude didn't write a narration, build one from the first action.
        # Skip for click actions — _execute_action_inner will speak the specific element label.
        _CLICK_ACTIONS = {"left_click", "double_click", "right_click"}
        first_action = next(
            (b for b in response.content if getattr(b, "type", "") == "tool_use"), None
        )
        first_action_type = first_action.input.get("action", "") if first_action else ""

        if not thought and first_action_type not in _CLICK_ACTIONS:
            if first_action:
                a = first_action_type
                thought = {
                    "screenshot":     "Looking at the screen.",
                    "scroll":         "Scrolling the page.",
                    "type":           f"Typing: {first_action.input.get('text','')[:30]}",
                    "key":            f"Pressing {first_action.input.get('text','')}.",
                    "mouse_move":     "Moving the mouse.",
                    "wait":           "Waiting.",
                }.get(a, f"Performing {a}.")
            print(f"[CLAUDE] fallback narration: {thought!r}", file=sys.stderr)

        # For click actions, skip generic thought — per-action speak uses real element label
        if thought and first_action_type not in _CLICK_ACTIONS:
            speak(thought)
        elif thought and first_action_type in _CLICK_ACTIONS:
            # Still update the bubble text with Claude's thought (shown visually)
            # but don't speak it — _execute_action_inner will speak the element label
            with state_lock:
                state["reasoning_text"] = thought
                state["speech_done_ts"] = now()  # start fade timer immediately

        with state_lock:
            state["reasoning"]        = False
            state["reasoning_end_ts"] = now()
            state["reasoning_text"]   = thought
            state["last_thought"]     = raw  # full raw text for high-stakes fallback

        messages.append({"role": "assistant", "content": response.content})

        # end_turn = agent wrote summary (no more tool calls)
        if response.stop_reason == "end_turn":
            summary_text = raw
            print(f"\n[CLAUDE] === SUMMARY ===\n{summary_text}\n", file=sys.stderr)
            set_progress(3, 4, "Summary ready")
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            action = block.input.get("action", "")
            print(f"[ACTION] {action}  {block.input}", file=sys.stderr)
            if _recorder is not None:
                _recorder.log_action(action, block.input)

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
        print("[CU] Max iterations.", file=sys.stderr)

    # ── Phase 3: Open Google Doc + paste summary (S tasks only) ──
    if not summary_text:
        summary_text = f"{task['name']}\n(Agent could not retrieve summary)"

    if task.get("write_doc"):
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
    play_sound("Glass.aiff")

    if _recorder is not None:
        _recorder.stop(summary=summary_text)

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
            x, y         = state["cursor_pos"]
            is_camera    = state.get("screenshot_action", False)
            reasoning    = state.get("reasoning", False)
            cursor_state = state.get("cursor_state", "default")

        t = now()
        if is_camera:
            # Pulsing iris — "seeing"
            pulse = 0.5 + 0.5 * math.sin(t * 8)
            self.glow(x, y, 20, self.cyan, 0.15 * pulse, steps=5)
            self.stroke_circle(x, y, 14, 1.2, self.cyan, 0.55 * pulse)
            self.draw_circle(x, y, 4, self.cyan, 0.85)
            self.draw_circle(x, y, 2, self.white, 0.9)
        elif cursor_state == "clicking":
            # Expanding ring — "about to click"
            pulse = 0.5 + 0.5 * math.sin(t * 10)
            self.glow(x, y, 22, self.cyan, 0.20 * pulse, steps=5)
            self.stroke_circle(x, y, 16, 2.0, self.cyan, 0.70)
            self.stroke_circle(x, y, 8, 1.5, self.cyan, 0.50 * pulse)
            self.draw_circle(x, y, 3, self.cyan, 0.90)
        elif cursor_state == "reading":
            # Slow amber pulse — "reading"
            pulse = 0.5 + 0.5 * math.sin(t * 2.5)
            self.glow(x, y, 18, self.amber, 0.18 * pulse, steps=5)
            self.stroke_circle(x, y, 10, 1.5, self.amber, 0.60)
            self.draw_circle(x, y, 3, self.amber, 0.85)
        elif reasoning:
            # Breathing glow — "thinking"
            pulse = 0.3 + 0.7 * math.sin(t * 3.5)
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
        lines = lines[:5]

        pad_x, pad_y, line_h, cw = 22, 16, 24, 10.0
        box_w = min(max(len(l) for l in lines) * cw + pad_x * 2, 560)
        box_h = len(lines) * line_h + pad_y * 2
        tail_h = 10

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
        attrs[NSFontAttributeName] = NSFont.monospacedSystemFontOfSize_weight_(16.0, 0.0)
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
        self.draw_speech_bubble(SCREEN_W // 2, SCREEN_H - 60, text,
                                self.cyan, alpha * 0.88, max_chars=48,
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
            with state_lock:
                speech_done_ts = state.get("speech_done_ts")
            if speech_done_ts is None:
                alpha = 0.85
            else:
                age = t - speech_done_ts
                if age > 3.0 + BUBBLE_FADE_OUT:
                    with state_lock:
                        state["reasoning_text"] = ""
                    return
                alpha = 0.85 if age < 3.0 else 0.85 * (1.0 - (age - 3.0) / BUBBLE_FADE_OUT)
            slide = 1.0
            display = text
        else:
            return
        if not display:
            return
        color_fn = self.soft_blue
        self.draw_speech_bubble(cx, cy, display, color_fn, alpha,
                                max_chars=56, above=True, slide=slide)

# ─────────────────────────────────────────────────────────────
# Timer / Window
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

def main():
    global _recorder, _record_enabled

    parser = argparse.ArgumentParser(description="Legible agent (legible_agent.py)")
    parser.add_argument(
        "--record", action="store_true",
        help="Enable workflow recording. Task id is taken from the interactive task selector. "
             "Saves frames, log.json, report.md, and video.mp4 to recordings/<task_id>/",
    )
    args, _ = parser.parse_known_args()

    if args.record:
        _record_enabled = True
        print("[main] recording enabled — task folder will be set after task selection", file=sys.stderr)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    build_window()
    timer_target = TimerTarget.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0 / FPS, timer_target, "tick:", None, True,
    )
    threading.Thread(target=sample_mouse_loop, daemon=True).start()
    threading.Thread(target=task_loop,         daemon=True).start()
    app.run()

if __name__ == "__main__":
    main()