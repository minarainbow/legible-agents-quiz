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
    Return a list of moments, each anchored to a screenshot frame.
    Each moment includes the video timestamp and the reasoning/action
    text that immediately preceded the screenshot.
    """
    events = log.get("events", [])
    moments = []
    pending_reasoning: list[str] = []
    pending_actions: list[str] = []

    for ev in events:
        kind = ev.get("kind")
        if kind == "reasoning":
            pending_reasoning.append(ev["text"])
        elif kind == "action":
            act = ev.get("action", ev.get("params", {}).get("action", ""))
            params = ev.get("params", {})
            detail = ""
            if act == "type":
                detail = f" '{params.get('text', '')[:60]}'"
            elif act == "key":
                detail = f" {params.get('text', '')}"
            elif act in ("left_click", "right_click", "double_click"):
                detail = f" ({params.get('coordinate', '')})"
            elif act == "scroll":
                detail = f" delta_y={params.get('delta_y', '')}"
            pending_actions.append(f"{act}{detail}")
        elif kind == "agent_screenshot":
            frame = ev["frame"]
            vid_sec = frame_to_sec(frame)
            moments.append({
                "video_sec": vid_sec,
                "timestamp_label": fmt_time(vid_sec),
                "reasoning": "; ".join(pending_reasoning) or "(no reasoning logged)",
                "actions": pending_actions.copy() or ["(none)"],
            })
            pending_reasoning.clear()
            pending_actions.clear()

    return moments


def build_prompt(task_name: str, task_goal: str, moments: list[dict], n_probes: int) -> str:
    timeline_text = ""
    for i, m in enumerate(moments):
        timeline_text += (
            f"\n[{m['timestamp_label']} / {m['video_sec']:.1f}s]\n"
            f"  Reasoning: {m['reasoning']}\n"
            f"  Actions:   {', '.join(m['actions'])}\n"
        )

    return f"""\
You are a research assistant designing quiz probes for a study on AI agent legibility.

A computer-use agent completed the following task:
Task name: {task_name}
Task goal: {task_goal}

Below is a timeline of the agent's reasoning and actions, each anchored to a video timestamp:
{timeline_text}

Your job: produce exactly {n_probes} quiz probes spread across the timeline that test
whether a human observer can infer the agent's current goal or predict its next action.

Rules:
- Alternate between the two probe types: "next_action_prediction" (P) and "goal_legibility" (G).
  Start with P, then G, then P, then G, etc.
- Choose moments that are genuinely interesting / non-obvious — avoid the very start or end.
- For next_action_prediction: ask about the NEXT MEANINGFUL action at task-step level
  (not cursor movements). The action should have already happened just AFTER the chosen timestamp.
- For goal_legibility: ask about the IMMEDIATE LOCAL subgoal (not the overall task).
- pause_time_sec must be an integer (seconds into the video).
- Do NOT reveal what happens after the pause time in the anchor or question.
- Provide realistic accepted_answers (2-4 items), partial_answers (1-3 items),
  and reject_examples with keys "too_broad", "too_low_level", and "wrong" (1-3 each).

Respond with a JSON array ONLY (no markdown, no extra text). Each element must have:
{{
  "id": "P1" or "G1" etc,
  "type": "next_action_prediction" | "goal_legibility",
  "pause_time_sec": <int>,
  "timestamp_label": "<M:SS>",
  "anchor": "<one sentence describing what is visible on screen at this moment>",
  "question": "<the question shown to the participant>",
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
    parser.add_argument("--n", type=int, default=5, help="Number of probes to generate (default: 5)")
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

    if len(moments) < args.n:
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

    out_path = task_dir / "quiz.json"
    out_path.write_text(json.dumps(probes, indent=2, ensure_ascii=False))
    print(f"✅  Wrote {len(probes)} probes → {out_path}", file=sys.stderr)

    # Print a short summary
    for p in probes:
        print(f"  [{p['id']}] {p['type']} @ {p['timestamp_label']} — {p['question'][:60]}…")


if __name__ == "__main__":
    main()
