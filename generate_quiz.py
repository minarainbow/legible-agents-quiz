"""
generate_quiz.py — Auto-generate quiz.json for a recording using an LLM.

Usage:
    python generate_quiz.py --task s1 --n 4      # appends up to 4 new next + 4 new past probes to quiz.json
    python generate_quiz.py --task s1 --past-only --n 4   # append past probes only
    python generate_quiz.py --task s1 --overwrite --n 4   # replace quiz.json (no merge)

Reads:  recordings/<task_id>/log.json
Writes: recordings/<task_id>/quiz.json (merges new probes into existing file by default)

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


def _params_dict(ev: dict) -> dict:
    p = ev.get("params") or {}
    return p if isinstance(p, dict) else {}


def action_should_skip_for_quiz(act: str, ev: dict) -> bool:
    """Exclude scroll and navigation-only keys from quiz moments (still in agent log)."""
    act = (act or "").strip().lower()
    if act == "scroll":
        return True
    if act != "key":
        return False
    params = _params_dict(ev)
    raw = str(params.get("text", "")).strip().lower().replace(" ", "")
    # Navigation / confirm-only keys — not substantive quiz targets
    boring = frozenset({
        "return", "enter", "\r", "\n", "escape", "esc", "tab",
        "keydown", "keyup",
    })
    if raw in boring:
        return True
    # Common spellings
    if "return" in raw or raw == "enter":
        return True
    return False


def moment_line_is_banned(actions: list[str]) -> bool:
    """True if logged action line is scroll or Enter/Return/Escape/Tab key."""
    for a in actions or []:
        al = (a or "").lower()
        if al.startswith("scroll"):
            return True
        if al.startswith("key"):
            if any(k in a for k in ("Return", "Enter", "enter", "Escape", "escape", "Esc", "\r", "\n", "Tab")):
                return True
    return False


_REF_ANSWER_BAN = re.compile(
    r"\b(scroll(ed|ing|s)?|scrolled|mouse\s*wheel|\bwheel\b|"
    r"press(ed)?\s*(enter|return)|\benter\b|\breturn\b|hit\s+enter|return\s+key|escape(\s+key)?|tab\s+key)\b",
    re.I,
)


def probe_reference_is_banned(p: dict) -> bool:
    blob = " ".join([
        str(p.get("reference_answer") or ""),
        " ".join(p.get("accepted_answers") or []) if isinstance(p.get("accepted_answers"), list) else "",
    ])
    return bool(_REF_ANSWER_BAN.search(blob))


def filter_llm_probes(probes: list[dict], moments: list[dict], label: str) -> list[dict]:
    """Remove probes that target banned moments or mention scroll/Enter in the answer text."""
    out = []
    for p in probes:
        mi = p.get("moment_index")
        if mi is not None and isinstance(mi, int) and 0 <= mi < len(moments):
            if moment_line_is_banned(moments[mi].get("actions") or []):
                print(f"[generate_quiz] drop {label} {p.get('id', '?')}: moment #{mi} is scroll/enter", file=sys.stderr)
                continue
        if probe_reference_is_banned(p):
            print(f"[generate_quiz] drop {label} {p.get('id', '?')}: answer text mentions scroll/Enter/navigation key", file=sys.stderr)
            continue
        out.append(p)
    return out

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
    """Legacy fallback: frame_index * FRAME_INTERVAL (does NOT match compiled video).

    workflow_recorder concatenates every PNG in numeric order; for each index N there is
    usually both ``NNNN.png`` and ``NNNN_agent_screenshot.png``, each with duration
    FRAME_INTERVAL — so real video time ≠ frame_index * FRAME_INTERVAL.
    Prefer :func:`build_frame_start_map` when ``recordings/<task>/frames`` exists.
    """
    m = re.match(r"^(\d+)", frame_filename)
    return int(m.group(1)) * FRAME_INTERVAL if m else 0.0


def build_frame_start_map(task_dir: Path) -> dict[str, float]:
    """Map each ``frames/*.png`` basename → start time in compiled ``video.mp4``.

    Order matches ``workflow_recorder._compile_video`` (regular frame before agent screenshot
    at the same numeric prefix).
    """
    frames_dir = task_dir / "frames"
    if not frames_dir.exists():
        return {}
    frames = sorted(
        [f for f in frames_dir.iterdir() if f.suffix == ".png"],
        key=lambda f: (int(re.match(r"(\d+)", f.name).group(1)),
                       1 if "_" in f.name else 0),
    )
    t = 0.0
    out: dict[str, float] = {}
    for f in frames:
        out[f.name] = t
        t += FRAME_INTERVAL
    return out


def fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def build_timeline(log: dict, task_dir: Path | None = None) -> list[dict]:
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
    starts = build_frame_start_map(task_dir) if task_dir else {}
    moments = []
    pending_reasoning: list[str] = []
    last_shot_sec: float | None = None

    pending_action: dict | None = None  # {pre_sec, action_label, reasoning}

    def shot_time(fname: str) -> float:
        return starts.get(fname, frame_to_sec(fname))

    for ev in events:
        kind = ev.get("kind")
        if kind == "reasoning":
            pending_reasoning.append(ev["text"])
        elif kind == "agent_screenshot":
            shot_sec = shot_time(ev["frame"])
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
            if action_should_skip_for_quiz(act, ev):
                continue
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

    moments_out: list[dict] = []
    for m in moments:
        if moment_line_is_banned(m.get("actions") or []):
            continue
        moments_out.append(m)
    return moments_out


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

    starts = build_frame_start_map(task_dir)

    def start_for(fname: str) -> float:
        return starts.get(fname, frame_to_sec(fname))

    moments = []
    for i in range(len(shot_files) - 1):
        pre  = start_for(shot_files[i].name)
        post = start_for(shot_files[i + 1].name)
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
    for i, m in enumerate(moments):
        pre  = int(round(m["video_sec"]))
        post = int(round(m["post_sec"]))
        lines.append(
            f"\n[Moment #{i} | Pre-action @ {fmt_time(pre)}/{pre}s → action → Post-action @ {fmt_time(post)}/{post}s]\n"
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
- Each probe MUST correspond to exactly one Moment from the numbered list above.
  Set moment_index to that Moment's number (e.g. 3 for "Moment #3").
- pause_time_sec MUST equal the Pre-action timestamp of your chosen Moment EXACTLY
  (copy it from the timeline — do NOT invent intermediate values).
- pause_time_sec MUST be >= 10 seconds.
- Prefer moments where the agent chooses between alternatives (which item, filter, link, button).
- Avoid bunching probes — spread them across the full timeline.
- Do NOT reveal the action in the anchor or question.
- The "question" text MUST end with exactly: (Exclude trivial actions such as screenshots or scrolling the page.)
- SKIP moments whose Action line is scroll, mouse-wheel scroll, or a navigation-only key
  (Enter, Return, Escape, Tab). Do NOT choose moments that are only those actions.
- NEVER write reference_answer, accepted_answers, or partial_answers that describe
  scrolling, pressing Enter/Return, Escape, or Tab — even if reasoning text mentions them.
- Only use moments with substantive actions (click, type meaningful text, drag, choose links).
{avoid_clause}- Provide accepted_answers (2-4), partial_answers (1-3), reject_examples with keys
  "too_broad", "too_low_level", "wrong" (1-3 each).

Respond with a JSON array ONLY (no markdown). Each element:
{{
  "id": "P1" … "P{n}",
  "moment_index": <integer Moment # you chose>,
  "type": "next_action_prediction",
  "pause_time_sec": <Pre-action timestamp of that Moment as int, copied exactly>,
  "timestamp_label": "<M:SS>",
  "anchor": "<one sentence: what is visible on screen right before the action>",
  "question": "What is the next meaningful action the agent will likely take in the interface? (Exclude trivial actions such as screenshots or scrolling the page.)",
  "reference_answer": "<ideal answer describing the action IN that chosen Moment>",
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
- Each probe MUST correspond to exactly one Moment from the numbered list above.
  Set moment_index to that Moment's number (e.g. 5 for "Moment #5").
- pause_time_sec MUST equal the Post-action timestamp of your chosen Moment EXACTLY
  (copy it from the timeline — do NOT invent intermediate values).
- pause_time_sec MUST be >= 10 seconds.
- Choose different moments from each other — spread them across the full timeline.
- Do NOT reveal the action in the anchor or question.
- The anchor describes what is visible on screen AFTER the action.
- The reference_answer MUST describe the action connecting Pre-action to Post-action
  of YOUR CHOSEN Moment — NOT an action from a different moment in the timeline.
- The "question" text MUST end with exactly: (Exclude trivial actions such as screenshots or scrolling the page.)
- SKIP moments whose Action line is scroll or Enter/Return/Escape/Tab-only.
- NEVER write reference_answer or accepted_answers that describe scrolling or pressing Enter/Return.
- Only choose moments with substantive actions (click, type, drag, select links/filters).
{avoid_clause}- Provide accepted_answers (2-4), partial_answers (1-3), reject_examples with keys
  "too_broad", "too_low_level", "wrong" (1-3 each).

Respond with a JSON array ONLY (no markdown). Each element:
{{
  "id": "R1" … "R{n}",
  "moment_index": <integer Moment # you chose>,
  "type": "past_action_recall",
  "pause_time_sec": <Post-action timestamp of that Moment as int, copied exactly>,
  "timestamp_label": "<M:SS>",
  "anchor": "<one sentence: what is visible on screen after the action just occurred>",
  "question": "What meaningful action did the agent just take? (Exclude trivial actions such as screenshots or scrolling the page.)",
  "reference_answer": "<ideal answer describing only the action IN that chosen Moment>",
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


def load_existing_quiz(task_dir: Path) -> list[dict]:
    path = task_dir / "quiz.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        print("[generate_quiz] Warning: quiz.json is not valid JSON — treating as empty.", file=sys.stderr)
        return []


def max_id_suffix(existing: list[dict], prefix: str) -> int:
    """Highest N in ids like P7 / R12 (case-insensitive)."""
    rx = re.compile(rf"^{re.escape(prefix)}(\d+)$", re.I)
    mx = 0
    for p in existing:
        m = rx.match(str(p.get("id") or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


def dedupe_key(p: dict) -> tuple[str | None, int]:
    return (p.get("type"), int(round(float(p.get("pause_time_sec", 0)))))


def filter_new_against_keys(new_items: list[dict], keys: set) -> list[dict]:
    """Keep probes whose (type, pause_sec) is not already in keys; update keys."""
    out = []
    for p in new_items:
        k = dedupe_key(p)
        if k in keys:
            print(f"[generate_quiz] skip duplicate: {k[0]} @ {k[1]}s", file=sys.stderr)
            continue
        keys.add(k)
        out.append(p)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate quiz.json for a recording.")
    parser.add_argument("--task", required=True, help="Recording/task ID (e.g. t1, legible_t1_19_cropped)")
    parser.add_argument("--n", type=int, default=4, help="Number of probes per type (default: 4, generates 4 next + 4 past = 8 total)")
    parser.add_argument("--model", default="", help="Override LLM model name")
    parser.add_argument("--past-only", action="store_true",
                        help="Do not generate new next-action probes; only append new past-action probes")
    parser.add_argument("--overwrite", action="store_true",
                        help="Ignore existing quiz.json and write only newly generated probes (old behavior)")
    args = parser.parse_args()

    task_dir = RECORDINGS_DIR / args.task
    log_path = task_dir / "log.json"

    if not log_path.exists():
        sys.exit(f"❌  log.json not found at {log_path}")

    log = json.loads(log_path.read_text())
    events = log.get("events", [])

    existing = [] if args.overwrite else load_existing_quiz(task_dir)
    if existing and not args.overwrite:
        print(f"[generate_quiz] Loaded {len(existing)} existing probe(s) from quiz.json (merge mode).", file=sys.stderr)

    # Pull task metadata from session_start event
    session = next((e for e in events if e.get("kind") == "session_start"), {})
    task_name = session.get("task_name", f"Task {args.task}")
    task_goal = session.get("task_goal", "(no goal recorded)")

    moments = build_timeline(log, task_dir)
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

    def snap_to_timeline(probes: list[dict], use_post: bool) -> list[dict]:
        """Snap each probe's pause_time_sec to the exact timeline value.

        If the LLM honoured moment_index, we use that directly to look up the
        pre_sec (next-action) or post_sec (past-action) of that moment.
        For past-action fallbacks, prefer the moment whose [pre, post] interval
        contains the LLM's pause time, then the smallest post_sec >= pause
        (never snap backward to an earlier moment's post — that caused pauses
        before the described action was visible).
        """
        if not moments:
            return probes
        pre_secs  = [int(round(m["video_sec"])) for m in moments]
        post_secs = [int(round(m["post_sec"]))  for m in moments]
        snapped = []
        for p in probes:
            mi = p.get("moment_index")
            if mi is not None and isinstance(mi, int) and 0 <= mi < len(moments):
                correct = post_secs[mi] if use_post else pre_secs[mi]
            elif use_post:
                pause = int(round(p.get("pause_time_sec", 0)))
                contain_i = None
                for i in range(len(moments)):
                    if pre_secs[i] <= pause <= post_secs[i]:
                        contain_i = i
                        break
                if contain_i is not None:
                    correct = post_secs[contain_i]
                else:
                    forwards = [s for s in post_secs if s >= pause]
                    correct = min(forwards) if forwards else min(post_secs, key=lambda s: abs(s - pause))
            else:
                pool = pre_secs
                correct = min(pool, key=lambda s: abs(s - p.get("pause_time_sec", 0)))
            old = p.get("pause_time_sec", 0)
            if old != correct:
                print(f"  [snap] probe {p.get('id','?')} pause {old}s → {correct}s", file=sys.stderr)
            p["pause_time_sec"] = correct
            p["timestamp_label"] = fmt_time(correct)
            snapped.append(p)
        return snapped

    MIN_PAUSE_SEC = 10  # never pause in the first 10 seconds

    dedupe_keys = {dedupe_key(p) for p in existing}

    next_batch: list[dict] = []

    # ── Next-action probes ─────────────────────────────────────────────────────
    if args.past_only:
        existing_next = [p for p in existing if p.get("type") == "next_action_prediction"]
        if not existing_next:
            print("[generate_quiz] --past-only but no existing next-action probes — generating next + past.", file=sys.stderr)
            args.past_only = False

    if not args.past_only:
        print(f"[generate_quiz] Calling LLM for {args.n} next-action probes…", file=sys.stderr)
        next_batch = parse_probes(generate(build_next_action_prompt(task_name, task_goal, moments, args.n), args.model))
        next_batch = snap_to_timeline(next_batch, use_post=False)
        next_batch = filter_llm_probes(next_batch, moments, "next")
        next_batch = [p for p in next_batch if p.get("pause_time_sec", 0) >= MIN_PAUSE_SEC]
        next_batch.sort(key=lambda p: p.get("pause_time_sec", 0))
        next_batch = filter_new_against_keys(next_batch, dedupe_keys)
        mp = max_id_suffix(existing, "P")
        for p in next_batch:
            p["type"] = "next_action_prediction"
            mp += 1
            p["id"] = f"P{mp}"

    # ── Past-action probes ─────────────────────────────────────────────────────
    next_secs_prompt = [
        int(round(p["pause_time_sec"]))
        for p in existing
        if p.get("type") == "next_action_prediction"
    ]
    next_secs_prompt.extend(int(round(p["pause_time_sec"])) for p in next_batch)

    print(f"[generate_quiz] Calling LLM for {args.n} past-action probes…", file=sys.stderr)
    past_batch = parse_probes(generate(build_past_action_prompt(task_name, task_goal, moments, args.n, avoid_secs=next_secs_prompt), args.model))
    past_batch = snap_to_timeline(past_batch, use_post=True)
    past_batch = filter_llm_probes(past_batch, moments, "past")
    past_batch = [p for p in past_batch if p.get("pause_time_sec", 0) >= MIN_PAUSE_SEC]
    past_batch.sort(key=lambda p: p.get("pause_time_sec", 0))
    past_batch = filter_new_against_keys(past_batch, dedupe_keys)
    mr = max_id_suffix(existing, "R")
    for p in past_batch:
        p["type"] = "past_action_recall"
        mr += 1
        p["id"] = f"R{mr}"

    all_probes = existing + next_batch + past_batch
    all_probes.sort(key=lambda p: p.get("pause_time_sec", 0))

    out_path = task_dir / "quiz.json"
    out_path.write_text(json.dumps(all_probes, indent=2, ensure_ascii=False))
    added_n, added_p = len(next_batch), len(past_batch)
    print(f"✅  quiz.json → {len(all_probes)} total probes (+{added_n} next, +{added_p} past) → {out_path}", file=sys.stderr)
    if added_n < args.n or added_p < args.n:
        print(f"⚠️  Requested {args.n} new of each type but added {added_n} next / {added_p} past (after dedupe / filtering).", file=sys.stderr)

    for p in next_batch + past_batch:
        print(f"  [NEW {p['id']}] {p['type']} @ {p['timestamp_label']} — {p['question'][:60]}…")


if __name__ == "__main__":
    main()
