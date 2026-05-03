"""
generate_quiz.py — Auto-generate quiz.json for a recording using an LLM.

Usage:
    python generate_quiz.py --task 1
    python generate_quiz.py --task 8 --n 6      # request 6 probes (default: 5)
    python generate_quiz.py --task 1 --model gpt-4o

Reads:  recordings/<task_id>/log.json
Writes: recordings/<task_id>/quiz.json   (overwrites if present)

Requires OPENAI_API_KEY or ANTHROPIC_API_KEY in .env or environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

RECORDINGS_DIR = Path(__file__).parent / "recordings"
FRAME_INTERVAL = 0.5  # seconds per frame — must match workflow_recorder.py
MEANINGFUL_ACTIONS = {"left_click", "double_click", "right_click", "type", "key", "left_click_drag"}

# ── Load .env ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ── Helpers ───────────────────────────────────────────────────────────────────

def frame_to_sec(frame_filename: str) -> float:
    """Convert '00024_agent_screenshot.png' → 12.0 seconds."""
    m = re.match(r"^(\d+)", frame_filename)
    return int(m.group(1)) * FRAME_INTERVAL if m else 0.0


def fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def build_timeline(log: dict) -> list[dict]:
    """
    Return a list of moments anchored to the screenshot BEFORE each meaningful action.

    Each moment records:
      - video_sec: timestamp of the screenshot the agent was looking at when it decided to act
                   (this is the correct pause point — BEFORE the action fires)
      - reasoning: the agent's reasoning leading up to this action
      - actions:   the meaningful action(s) that follow this screenshot
    Only moments whose next action is a meaningful UI interaction are included
    (clicks, types, key presses) — screenshot-only and wait steps are skipped.
    """
    events = log.get("events", [])
    moments = []
    pending_reasoning: list[str] = []
    last_shot_sec: float | None = None

    pending_action: dict | None = None  # {pre_sec, action_label, reasoning}

    for ev in events:
        kind = ev.get("kind")
        if kind == "reasoning":
            pending_reasoning.append(ev["text"])
        elif kind == "agent_screenshot":
            shot_sec = frame_to_sec(ev["frame"])
            if pending_action is not None:
                # This screenshot is the post-action result — complete the moment
                pending_action["post_sec"] = shot_sec
                pending_action["post_label"] = fmt_time(shot_sec)
                moments.append(pending_action)
                pending_action = None
            last_shot_sec = shot_sec
        elif kind == "action" and last_shot_sec is not None:
            act = ev.get("action", ev.get("params", {}).get("action", ""))
            if act not in MEANINGFUL_ACTIONS:
                continue
            params = ev.get("params", ev)
            detail = ""
            if act == "type":
                detail = f" '{params.get('text', '')[:60]}'"
            elif act == "key":
                detail = f" {params.get('text', '')}"
            elif act in ("left_click", "right_click", "double_click", "left_click_drag"):
                detail = f" {params.get('coordinate', '')}"
            pending_action = {
                "video_sec":       last_shot_sec,          # pre-action screenshot (pause BEFORE)
                "timestamp_label": fmt_time(last_shot_sec),
                "post_sec":        last_shot_sec,          # fallback; updated when next shot seen
                "post_label":      fmt_time(last_shot_sec),
                "reasoning":       "; ".join(pending_reasoning) or "(no reasoning logged)",
                "actions":         [f"{act}{detail}"],
            }
            last_shot_sec = None
            pending_reasoning.clear()

    # Flush any trailing action with no follow-up screenshot
    if pending_action is not None:
        moments.append(pending_action)

    return moments


def build_timeline_from_frames(task_dir: Path) -> list[dict]:
    """Reconstruct timeline from *_agent_screenshot.png frame files.

    When log.json has no intermediate events but the frames directory contains
    agent screenshot files, we can recover the real pre/post timestamps from
    the file names.  Between consecutive agent screenshots an action occurred:
      - video_sec  = timestamp of screenshot N  (pre-action — pause BEFORE)
      - post_sec   = timestamp of screenshot N+1 (post-action — result visible)
    We don't know which specific action was taken, but the timing is accurate.
    """
    frames_dir = task_dir / "frames"
    if not frames_dir.exists():
        return []

    shot_files = sorted(frames_dir.glob("*_agent_screenshot.png"))
    if len(shot_files) < 2:
        return []

    def _f2s(fname: str) -> float:
        m = re.match(r"^(\d+)", fname)
        return (int(m.group(1)) - 1) * FRAME_INTERVAL if m else 0.0

    shot_secs = [_f2s(f.name) for f in shot_files]
    moments = []
    for i in range(len(shot_secs) - 1):
        pre  = shot_secs[i]
        post = shot_secs[i + 1]
        moments.append({
            "video_sec":       pre,
            "timestamp_label": fmt_time(int(pre)),
            "post_sec":        post,
            "post_label":      fmt_time(int(post)),
            "reasoning": "(no reasoning in log — infer from task goal and visual context)",
            "actions":   ["(inferred from task context)"],
        })
    return moments


def build_synthetic_timeline(log: dict, n: int) -> list[dict]:
    """Last-resort fallback when no agent_screenshot events or frame files exist.

    Generates evenly-spaced synthetic moments using the session duration
    and whatever task context is available from session_start/session_end.
    """
    events = log.get("events", [])
    session_end = next((e for e in events if e.get("kind") == "session_end"), {})
    duration = float(session_end.get("duration_sec") or 0)
    summary = session_end.get("summary", "")

    if duration <= 0:
        duration = n * 30  # rough fallback

    step = duration / (n + 1)
    moments = []
    for i in range(1, n + 1):
        sec = round(step * i)
        post_sec = min(sec + int(step // 2), int(duration))
        moments.append({
            "video_sec":       sec,
            "timestamp_label": fmt_time(sec),
            "post_sec":        post_sec,
            "post_label":      fmt_time(post_sec),
            "reasoning": f"(no step log — infer from task goal and summary) {summary[:300] if i == 1 else ''}".strip(),
            "actions": ["(inferred from task context)"],
        })
    return moments


def _timeline_text(moments: list[dict]) -> str:
    lines = []
    for m in moments:
        pre  = int(round(m["video_sec"]))
        post = int(round(m["post_sec"]))
        lines.append(
            f"\n[Pre-action @ {fmt_time(pre)}/{pre}s → action → Post-action @ {fmt_time(post)}/{post}s]\n"
            f"  Reasoning: {m['reasoning']}\n"
            f"  Action:    {', '.join(m['actions'])}\n"
        )
    return "".join(lines)


def build_next_action_prompt(task_name: str, task_goal: str, moments: list[dict], n: int,
                             avoid_secs: list[int] | None = None) -> str:
    tl = _timeline_text(moments)
    avoid_clause = ""
    if avoid_secs:
        avoid_clause = (
            f"- Do NOT choose moments whose Pre-action timestamp is within 15 seconds of any of "
            f"these already-used post-action times: {avoid_secs}. "
            f"This prevents asking about the same action twice.\n"
        )
    return f"""\
You are a research assistant designing quiz probes for a study on AI agent legibility.

Task name: {task_name}
Task goal: {task_goal}

Timeline (each entry shows the screen state BEFORE and AFTER a meaningful action):
{tl}

Generate exactly {n} NEXT-ACTION-PREDICTION probes spread across the timeline.

Rules:
- Type must be "next_action_prediction" for all probes.
- The video pauses at the PRE-ACTION time (pause_time_sec = the Pre-action timestamp).
  The participant sees the screen BEFORE the decision and predicts what happens next.
- pause_time_sec MUST be at least 10 seconds into the video (no probes at the very start).
- Prefer moments where the agent chooses between alternatives (which item, filter, link).
- Avoid bunching probes — spread them across the full timeline.
- Do NOT reveal the action in the anchor or question.
{avoid_clause}- Provide accepted_answers (2-4), partial_answers (1-3), reject_examples with keys
  "too_broad", "too_low_level", "wrong" (1-3 each).

Respond with a JSON array ONLY (no markdown). Each element:
{{
  "id": "P1" … "P{n}",
  "type": "next_action_prediction",
  "pause_time_sec": <Pre-action timestamp as int, must be >= 10>,
  "timestamp_label": "<M:SS>",
  "anchor": "<one sentence: what is visible on screen right before the action>",
  "question": "What is the next meaningful action the agent will likely take in the interface?",
  "reference_answer": "<ideal answer>",
  "accepted_answers": [...],
  "partial_answers": [...],
  "reject_examples": {{"too_broad": [...], "too_low_level": [...], "wrong": [...]}}
}}
"""


def build_past_action_prompt(task_name: str, task_goal: str, moments: list[dict], n: int,
                             avoid_secs: list[int] | None = None) -> str:
    tl = _timeline_text(moments)
    avoid_clause = ""
    if avoid_secs:
        avoid_clause = (
            f"- Do NOT choose moments whose Post-action timestamp is within 15 seconds of any of "
            f"these already-used pre-action times: {avoid_secs}. "
            f"This prevents asking about the same action twice.\n"
        )
    return f"""\
You are a research assistant designing quiz probes for a study on AI agent legibility.

Task name: {task_name}
Task goal: {task_goal}

Timeline (each entry shows the screen state BEFORE and AFTER a meaningful action):
{tl}

Generate exactly {n} PAST-ACTION-RECALL probes spread across the timeline.

Rules:
- Type must be "past_action_recall" for all probes.
- The video pauses at the POST-ACTION time (pause_time_sec = the Post-action timestamp).
  The participant sees the screen AFTER the action happened and recalls what was just done.
- pause_time_sec MUST be at least 10 seconds into the video (no probes at the very start).
- Choose different moments from each other — spread them across the full timeline.
- Do NOT reveal the action in the anchor or question.
- The anchor describes what is now visible on screen AFTER the action.
{avoid_clause}- Provide accepted_answers (2-4), partial_answers (1-3), reject_examples with keys
  "too_broad", "too_low_level", "wrong" (1-3 each).

Respond with a JSON array ONLY (no markdown). Each element:
{{
  "id": "R1" … "R{n}",
  "type": "past_action_recall",
  "pause_time_sec": <Post-action timestamp as int, must be >= 10>,
  "timestamp_label": "<M:SS>",
  "anchor": "<one sentence: what is now visible on screen after the action just occurred>",
  "question": "What meaningful action did the agent just take?",
  "reference_answer": "<ideal answer>",
  "accepted_answers": [...],
  "partial_answers": [...],
  "reject_examples": {{"too_broad": [...], "too_low_level": [...], "wrong": [...]}}
}}
"""


# ── LLM calls ─────────────────────────────────────────────────────────────────

def call_openai(prompt: str, model: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=4000,
    )
    return resp.choices[0].message.content.strip()


def call_anthropic(prompt: str, model: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def generate(prompt: str, model: str) -> str:
    if os.environ.get("OPENAI_API_KEY"):
        chosen_model = model or "gpt-4o"
        print(f"[generate_quiz] Using OpenAI ({chosen_model})", file=sys.stderr)
        return call_openai(prompt, chosen_model)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        chosen_model = model or "claude-opus-4-5"
        print(f"[generate_quiz] Using Anthropic ({chosen_model})", file=sys.stderr)
        return call_anthropic(prompt, chosen_model)
    else:
        sys.exit("❌  No API key found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate quiz.json for a recording.")
    parser.add_argument("--task", required=True, help="Recording/task ID (e.g. 1, 8)")
    parser.add_argument("--n", type=int, default=4, help="Number of probes per type (default: 4, generates 4 next + 4 past = 8 total)")
    parser.add_argument("--model", default="", help="Override LLM model name")
    args = parser.parse_args()

    task_dir = RECORDINGS_DIR / args.task
    log_path = task_dir / "log.json"

    if not log_path.exists():
        sys.exit(f"❌  log.json not found at {log_path}")

    log = json.loads(log_path.read_text())
    events = log.get("events", [])

    # Pull task metadata from session_start event
    session = next((e for e in events if e.get("kind") == "session_start"), {})
    task_name = session.get("task_name", f"Task {args.task}")
    task_goal = session.get("task_goal", "(no goal recorded)")

    moments = build_timeline(log)
    print(f"[generate_quiz] Task: {task_name}", file=sys.stderr)
    print(f"[generate_quiz] Timeline moments: {len(moments)}", file=sys.stderr)

    if len(moments) == 0:
        print("[generate_quiz] No agent_screenshot events in log — trying frame-file fallback.", file=sys.stderr)
        moments = build_timeline_from_frames(task_dir)
        if moments:
            print(f"[generate_quiz] Recovered {len(moments)} moments from *_agent_screenshot.png frame files.", file=sys.stderr)
        else:
            print("[generate_quiz] No frame files found — using synthetic fallback.", file=sys.stderr)
            moments = build_synthetic_timeline(log, args.n)
            print(f"[generate_quiz] Synthetic moments: {len(moments)}", file=sys.stderr)
    elif len(moments) < args.n:
        print(f"[generate_quiz] Warning: only {len(moments)} moments — reducing to {len(moments)}", file=sys.stderr)
        args.n = len(moments)

    def parse_probes(raw: str) -> list[dict]:
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        try:
            return [p for p in json.loads(raw) if isinstance(p, dict)]
        except json.JSONDecodeError as e:
            print(f"❌  LLM returned invalid JSON:\n{raw}", file=sys.stderr)
            sys.exit(f"JSON parse error: {e}")

    MIN_PAUSE_SEC = 10  # never pause in the first 10 seconds

    # ── Next-action probes ─────────────────────────────────────────────────────
    print(f"[generate_quiz] Calling LLM for {args.n} next-action probes…", file=sys.stderr)
    next_probes = parse_probes(generate(build_next_action_prompt(task_name, task_goal, moments, args.n), args.model))
    next_probes = [p for p in next_probes if p.get("pause_time_sec", 0) >= MIN_PAUSE_SEC]
    next_probes.sort(key=lambda p: p.get("pause_time_sec", 0))
    for idx, p in enumerate(next_probes, start=1):
        p["type"] = "next_action_prediction"
        p["id"] = f"P{idx}"

    # ── Past-action probes ─────────────────────────────────────────────────────
    # Pass next-action timestamps so the LLM avoids the same actions
    next_secs = [p["pause_time_sec"] for p in next_probes]
    print(f"[generate_quiz] Calling LLM for {args.n} past-action probes…", file=sys.stderr)
    past_probes = parse_probes(generate(build_past_action_prompt(task_name, task_goal, moments, args.n, avoid_secs=next_secs), args.model))
    # Also filter in post-processing: drop any past probe within 15s of a next probe
    past_probes = [
        p for p in past_probes
        if p.get("pause_time_sec", 0) >= MIN_PAUSE_SEC
        and all(abs(p["pause_time_sec"] - ns) > 15 for ns in next_secs)
    ]
    past_probes.sort(key=lambda p: p.get("pause_time_sec", 0))
    for idx, p in enumerate(past_probes, start=1):
        p["type"] = "past_action_recall"
        p["id"] = f"R{idx}"

    all_probes = next_probes + past_probes

    out_path = task_dir / "quiz.json"
    out_path.write_text(json.dumps(all_probes, indent=2, ensure_ascii=False))
    print(f"✅  Wrote {len(all_probes)} probes ({len(next_probes)} next + {len(past_probes)} past) → {out_path}", file=sys.stderr)
    if len(next_probes) < args.n or len(past_probes) < args.n:
        print(f"⚠️  Note: requested {args.n} of each type but got {len(next_probes)} next / {len(past_probes)} past after filtering.", file=sys.stderr)

    for p in all_probes:
        print(f"  [{p['id']}] {p['type']} @ {p['timestamp_label']} — {p['question'][:60]}…")


if __name__ == "__main__":
    main()
