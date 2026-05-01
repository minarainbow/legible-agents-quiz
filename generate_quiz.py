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

    for ev in events:
        kind = ev.get("kind")
        if kind == "reasoning":
            pending_reasoning.append(ev["text"])
        elif kind == "agent_screenshot":
            last_shot_sec = frame_to_sec(ev["frame"])
        elif kind == "action" and last_shot_sec is not None:
            act = ev.get("action", ev.get("params", {}).get("action", ""))
            if act not in MEANINGFUL_ACTIONS:
                continue  # skip screenshot, scroll, wait, mouse_move
            params = ev.get("params", ev)
            detail = ""
            if act == "type":
                detail = f" '{params.get('text', '')[:60]}'"
            elif act == "key":
                detail = f" {params.get('text', '')}"
            elif act in ("left_click", "right_click", "double_click", "left_click_drag"):
                detail = f" {params.get('coordinate', '')}"
            moments.append({
                "video_sec": last_shot_sec,
                "timestamp_label": fmt_time(last_shot_sec),
                "reasoning": "; ".join(pending_reasoning) or "(no reasoning logged)",
                "actions": [f"{act}{detail}"],
            })
            # Reset so same screenshot isn't reused for the next action
            last_shot_sec = None
            pending_reasoning.clear()

    return moments


def build_synthetic_timeline(log: dict, n: int) -> list[dict]:
    """Fallback when no agent_screenshot events exist.

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
        moments.append({
            "video_sec": sec,
            "timestamp_label": fmt_time(sec),
            "reasoning": f"(no step log — infer from task goal and summary) {summary[:300] if i == 1 else ''}".strip(),
            "actions": ["(inferred from task context)"],
        })
    return moments


def build_prompt(task_name: str, task_goal: str, moments: list[dict], n_probes: int) -> str:
    timeline_text = ""
    for i, m in enumerate(moments):
        # The screenshot at video_sec[i] shows the result AFTER the actions listed.
        # To pause BEFORE the action, use the previous screenshot's time.
        prev_sec = moments[i - 1]["video_sec"] if i > 0 else max(0, m["video_sec"] - 5)
        pause_sec = int(round(prev_sec))
        pause_label = fmt_time(pause_sec)
        timeline_text += (
            f"\n[State at {pause_label} / {pause_sec}s → then agent does:]\n"
            f"  Reasoning: {m['reasoning']}\n"
            f"  Actions:   {', '.join(m['actions'])}\n"
            f"  ↳ Suggested pause_time_sec for a probe here: {pause_sec}\n"
        )

    return f"""\
You are a research assistant designing quiz probes for a study on AI agent legibility.

A computer-use agent completed the following task:
Task name: {task_name}
Task goal: {task_goal}

Below is a timeline of the agent's reasoning and actions, each anchored to a video timestamp:
{timeline_text}

Your job: produce exactly {n_probes} next-action-prediction quiz probes spread across the timeline.

Rules:
- ALL probes must be type "next_action_prediction". Do NOT include any "goal_legibility" probes.
- Choose {n_probes} moments spread across the timeline — avoid bunching at start or end.
- Each probe asks: what is the NEXT MEANINGFUL action the agent will take at task-step level?
- ONLY place a probe at moments where the next action is a meaningful UI interaction:
    ✅ GOOD: clicking a button, selecting an item from a list, choosing among alternatives,
            clicking "Add to cart", opening a dropdown, submitting a form, navigating to a page,
            clicking a search result, selecting a filter or option
    ❌ NEVER place a probe when the next action is "screenshot" — skip those moments entirely.
    ❌ NEVER place a probe when the next action is waiting, scrolling aimlessly,
            thinking/reasoning only, or moving the mouse with no click.
- Prefer moments where the agent faces a CHOICE between alternatives (e.g. which product to click,
  which filter to apply, which link to follow) — these make the most interesting probes.
- TIMING IS CRITICAL: pause_time_sec must be the moment BEFORE the action happens —
  use the "Suggested pause_time_sec" value shown in the timeline for that action.
  The video pauses there, the participant sees the screen state BEFORE the decision,
  and is asked to predict what happens next. The action occurs AFTER the pause.
- pause_time_sec must be an integer (seconds into the video).
- Do NOT reveal what happens after the pause time in the anchor or question.
- Provide realistic accepted_answers (2-4 items), partial_answers (1-3 items),
  and reject_examples with keys "too_broad", "too_low_level", and "wrong" (1-3 each).

Respond with a JSON array ONLY (no markdown, no extra text). Each element must have:
{{
  "id": "P1", "P2", ... "P{n_probes}",
  "type": "next_action_prediction",
  "pause_time_sec": <int>,
  "timestamp_label": "<M:SS>",
  "anchor": "<one sentence describing what is visible on screen at this moment>",
  "question": "What is the next meaningful action the agent will likely take in the interface?",
  "reference_answer": "<the ideal answer>",
  "accepted_answers": ["<str>", ...],
  "partial_answers": ["<str>", ...],
  "reject_examples": {{
    "too_broad": ["<str>", ...],
    "too_low_level": ["<str>", ...],
    "wrong": ["<str>", ...]
  }}
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
    parser.add_argument("--n", type=int, default=6, help="Number of probes to generate (default: 6)")
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
        print(
            f"[generate_quiz] No agent_screenshot events found — using synthetic timeline fallback.",
            file=sys.stderr,
        )
        moments = build_synthetic_timeline(log, args.n)
        print(f"[generate_quiz] Synthetic moments: {len(moments)}", file=sys.stderr)
    elif len(moments) < args.n:
        print(
            f"[generate_quiz] Warning: only {len(moments)} screenshots — "
            f"reducing probes to {len(moments)}",
            file=sys.stderr,
        )
        args.n = len(moments)

    prompt = build_prompt(task_name, task_goal, moments, args.n)

    print(f"[generate_quiz] Calling LLM for {args.n} probes…", file=sys.stderr)
    raw = generate(prompt, args.model)

    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

    try:
        probes = json.loads(raw)
    except json.JSONDecodeError as e:
        print("❌  LLM returned invalid JSON. Raw response:\n", raw, file=sys.stderr)
        sys.exit(f"JSON parse error: {e}")

    # Enforce type and re-number IDs in timeline order
    probes = [p for p in probes if isinstance(p, dict)]
    probes.sort(key=lambda p: p.get("pause_time_sec", 0))
    for idx, p in enumerate(probes, start=1):
        p["type"] = "next_action_prediction"
        p["id"] = f"P{idx}"

    out_path = task_dir / "quiz.json"
    out_path.write_text(json.dumps(probes, indent=2, ensure_ascii=False))
    print(f"✅  Wrote {len(probes)} probes → {out_path}", file=sys.stderr)

    # Print a short summary
    for p in probes:
        print(f"  [{p['id']}] {p['type']} @ {p['timestamp_label']} — {p['question'][:60]}…")


if __name__ == "__main__":
    main()
