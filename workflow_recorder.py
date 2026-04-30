"""
workflow_recorder.py — Records agent task sessions into recordings/<task_id>/

Creates per-task folders with:
  - frames/     periodic PNG screenshots (captured every ~0.5 s)
  - log.json    structured event log (actions, reasoning, screenshots)
  - report.md   human-readable workflow report
  - video.mp4   compiled video (requires ffmpeg in PATH)

Usage:
    from workflow_recorder import WorkflowRecorder
    rec = WorkflowRecorder(task_id="1")
    rec.start(task_name="UIST 2026", task_goal="Find formatting guidelines…")
    rec.log_action("left_click", {"coordinate": [500, 300]})
    rec.log_reasoning("I need to find the Call for Papers link.")
    rec.log_screenshot_b64(base64_png_string)
    rec.stop(summary="• 10 pages, double-column…")
"""

from __future__ import annotations  # noqa: F401 — enables X | Y unions on Python 3.9

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import mss
from PIL import Image

RECORDINGS_DIR = Path(__file__).parent / "recordings"

# Seconds between periodic background frame captures
FRAME_INTERVAL = 0.5


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ts() -> float:
    return time.time()


def next_recording_id(base: str) -> str:
    """Return the next available folder name under RECORDINGS_DIR.

    If recordings/<base> doesn't exist → return base.
    If it does → try base_1, base_2, ... until a free slot is found.
    """
    if not (RECORDINGS_DIR / base).exists():
        return base
    i = 1
    while (RECORDINGS_DIR / f"{base}_{i}").exists():
        i += 1
    return f"{base}_{i}"


class WorkflowRecorder:
    """Records an agent task session to recordings/<task_id>/.

    Capture target (pick one, or leave both None for primary monitor):
      monitor (int): 1 = primary, 2 = second monitor, 0 = all monitors combined.
      region  (dict): {"top": y, "left": x, "width": w, "height": h} in screen pixels.
                      Overrides monitor if both are given.
    """

    def __init__(
        self,
        task_id: str,
        monitor: int = 1,
        region: Optional[dict] = None,
    ):
        self.task_id    = task_id
        self.task_dir   = RECORDINGS_DIR / task_id
        self.frames_dir = self.task_dir / "frames"
        self._monitor   = monitor
        self._region    = region   # e.g. {"top": 0, "left": 0, "width": 1280, "height": 800}

        self._events: list[dict] = []
        self._frame_count = 0
        self._start_ts: Optional[float] = None
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()

        self._task_name = ""
        self._task_goal = ""
        self._summary   = ""
        self._active    = False

    # ── public API ────────────────────────────────────────────

    def start(self, task_name: str, task_goal: str):
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(exist_ok=True)

        self._task_name = task_name
        self._task_goal = task_goal
        self._start_ts  = _ts()
        self._active    = True

        self._log_event("session_start", {
            "task_id":   self.task_id,
            "task_name": task_name,
            "task_goal": task_goal,
            "started_at": _now_iso(),
        })

        # background periodic frame capture
        self._stop_flag.clear()
        threading.Thread(target=self._frame_loop, daemon=True).start()
        print(f"[recorder] started → {self.task_dir}", file=sys.stderr)

    def log_action(self, action: str, params: dict):
        """Call this right before or after execute_action."""
        if not self._active:
            return
        entry = {"action": action}
        # include only the human-readable params (skip base64 blobs)
        clean = {k: v for k, v in params.items() if k != "data"}
        if clean:
            entry["params"] = clean
        self._log_event("action", entry)

    def log_reasoning(self, text: str):
        """Call with Claude's raw response text (narration / thought)."""
        if not self._active or not text:
            return
        self._log_event("reasoning", {"text": text})

    def log_screenshot_b64(self, b64: str):
        """Save a screenshot that was taken by the agent (from screenshot_base64())."""
        if not self._active or not b64:
            return
        frame_path = self._save_frame_from_b64(b64, label="agent_screenshot")
        self._log_event("agent_screenshot", {"frame": frame_path.name})

    def stop(self, summary: str = ""):
        if not self._active:
            return
        self._active = False
        self._stop_flag.set()
        self._summary = summary

        duration = round(_ts() - self._start_ts, 1) if self._start_ts else 0
        self._log_event("session_end", {
            "ended_at": _now_iso(),
            "duration_sec": duration,
            "summary": summary,
        })

        self._write_log()
        self._write_report()
        self._compile_video()

        print(f"[recorder] stopped — {duration}s  →  {self.task_dir}", file=sys.stderr)

    # ── internal helpers ──────────────────────────────────────

    def _log_event(self, kind: str, data: dict):
        entry = {"ts": round(_ts(), 3), "kind": kind, **data}
        with self._lock:
            self._events.append(entry)

    def _frame_loop(self):
        """Periodically capture the screen to the frames folder."""
        while not self._stop_flag.is_set():
            try:
                self._capture_frame()
            except Exception as exc:
                print(f"[recorder] frame error: {exc}", file=sys.stderr)
            self._stop_flag.wait(FRAME_INTERVAL)

    def _capture_frame(self) -> Path:
        with mss.mss() as sct:
            target = self._region if self._region else sct.monitors[self._monitor]
            raw = sct.grab(target)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img = img.resize((1280, 800), Image.Resampling.LANCZOS)

        with self._lock:
            self._frame_count += 1
            n = self._frame_count
        path = self.frames_dir / f"{n:05d}.png"
        img.save(path, format="PNG", optimize=False)
        return path

    def _save_frame_from_b64(self, b64: str, label: str = "frame") -> Path:
        data = base64.b64decode(b64)
        img  = Image.open(io.BytesIO(data)).convert("RGB")
        img  = img.resize((1280, 800), Image.Resampling.LANCZOS)

        with self._lock:
            self._frame_count += 1
            n = self._frame_count
        path = self.frames_dir / f"{n:05d}_{label}.png"
        img.save(path, format="PNG", optimize=False)
        return path

    def _write_log(self):
        log_path = self.task_dir / "log.json"
        with open(log_path, "w") as f:
            json.dump({"events": self._events}, f, indent=2)
        print(f"[recorder] log → {log_path}", file=sys.stderr)

    def _write_report(self):
        start_iso = next(
            (e["started_at"] for e in self._events if e["kind"] == "session_start"),
            "unknown",
        )
        end_entry  = next((e for e in self._events if e["kind"] == "session_end"), {})
        duration   = end_entry.get("duration_sec", "?")

        lines: list[str] = []
        lines.append(f"# Workflow Report — Task {self.task_id}: {self._task_name}")
        lines.append("")
        lines.append(f"**Started:** {start_iso}  ")
        lines.append(f"**Duration:** {duration} seconds  ")
        lines.append("")
        lines.append("## Goal")
        lines.append("")
        lines.append(self._task_goal)
        lines.append("")
        lines.append("## Step-by-Step Log")
        lines.append("")

        step = 0
        for ev in self._events:
            kind = ev["kind"]
            ts   = ev["ts"]
            rel  = round(ts - self._start_ts, 1) if self._start_ts else 0

            if kind == "session_start":
                continue
            elif kind == "session_end":
                continue
            elif kind == "reasoning":
                step += 1
                lines.append(f"### Step {step} _(+{rel}s)_")
                lines.append("")
                lines.append(f"**Reasoning:** {ev['text']}")
                lines.append("")
            elif kind == "action":
                action = ev["action"]
                params = ev.get("params", {})
                param_str = ", ".join(f"{k}={v!r}" for k, v in params.items())
                lines.append(f"- **Action:** `{action}({param_str})`  _(+{rel}s)_")
                lines.append("")
            elif kind == "agent_screenshot":
                frame = ev.get("frame", "")
                lines.append(f"  ![screenshot](frames/{frame})")
                lines.append("")

        if self._summary:
            lines.append("## Final Summary")
            lines.append("")
            lines.append(self._summary)
            lines.append("")

        lines.append("---")
        lines.append(f"_Generated by workflow_recorder.py_")

        report_path = self.task_dir / "report.md"
        report_path.write_text("\n".join(lines))
        print(f"[recorder] report → {report_path}", file=sys.stderr)

    def _compile_video(self):
        """Try to compile frames into video.mp4 using ffmpeg."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print("[recorder] ffmpeg not found — skipping video compile", file=sys.stderr)
            return

        out_path = self.task_dir / "video.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(round(1.0 / FRAME_INTERVAL)),
            "-pattern_type", "glob",
            "-i", str(self.frames_dir / "*.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "23",
            str(out_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
            if result.returncode == 0 and out_path.exists():
                print(f"[recorder] video → {out_path}", file=sys.stderr)
            else:
                err = (result.stderr or result.stdout or "").strip()
                if err:
                    print(f"[recorder] ffmpeg failed (code {result.returncode}): {err[:500]}", file=sys.stderr)
                else:
                    print(f"[recorder] ffmpeg failed (code {result.returncode}) — no video written", file=sys.stderr)
        except Exception as exc:
            print(f"[recorder] video compile failed: {exc}", file=sys.stderr)
