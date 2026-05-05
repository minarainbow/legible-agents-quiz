"""
quiz_app.py — Agent recording quiz reviewer

Usage:
    python quiz_app.py            # opens task 8 by default
    python quiz_app.py --task 10
    python quiz_app.py --task 8 --port 5051
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

RECORDINGS_DIR = Path(__file__).parent / "recordings"
FRAME_INTERVAL = 0.5   # seconds per frame (must match workflow_recorder.py)
QUIZ_TASK_IDS = ["s1", "s2", "s3", "t1", "t2", "t3", "legible_t1", "legible_t1_3", "legible_t1_9", "legible_t1_19", "legible_t1_19_cropped", "legible_t4_26"]
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://legible-agents-default-rtdb.firebaseio.com").rstrip("/")
FIREBASE_DB_SECRET = os.environ.get("FIREBASE_DB_SECRET", "").strip()

app = Flask(__name__)

# Load .env if present so OPENAI_API_KEY / ANTHROPIC_API_KEY work without exporting
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ─────────────────────────────────────────────────────────────
# LLM evaluation helper
# ─────────────────────────────────────────────────────────────

EVAL_PROMPT = """\
You are a research assistant scoring a participant's answer in a study on AI agent legibility.

Quiz type: {quiz_type}
Anchor context: {anchor}
Question: {question}

Participant answer: "{user_answer}"

Reference answer: "{reference_answer}"
Accepted answers (full credit): {accepted}
Partial credit answers: {partial}
Reject examples: {rejected}

Score on this scale:
  2 = Correct   — matches the reference or accepted answers in substance
  1 = Partial   — captures some key aspects but misses important details
  0 = Incorrect — wrong, off-topic, too vague, or "I don't know"

Respond with JSON only, no extra text:
{{"score": <0-2>, "label": "<correct|partial|incorrect>", "explanation": "<one concise sentence>"}}"""


def _call_openai(prompt: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=200,
    )
    return json.loads(resp.choices[0].message.content.strip())


def _call_anthropic(prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # strip markdown fences if any
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def evaluate_answer(q: dict, user_answer: str) -> dict:
    """Call LLM to score user_answer against quiz item q. Returns score dict."""
    rej = q.get("reject_examples", {})
    rej_flat = []
    for v in rej.values():
        if isinstance(v, list):
            rej_flat.extend(v)

    prompt = EVAL_PROMPT.format(
        quiz_type=q.get("type", ""),
        anchor=q.get("anchor", ""),
        question=q.get("question", ""),
        user_answer=user_answer,
        reference_answer=q.get("reference_answer", ""),
        accepted=json.dumps(q.get("accepted_answers", [])),
        partial=json.dumps(q.get("partial_answers", [])),
        rejected=json.dumps(rej_flat),
    )

    if os.environ.get("OPENAI_API_KEY"):
        return _call_openai(prompt)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        return _call_anthropic(prompt)
    else:
        raise RuntimeError("No OPENAI_API_KEY or ANTHROPIC_API_KEY found in environment.")


# ─────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────

def _events(task_id: str):
    p = RECORDINGS_DIR / task_id / "log.json"
    if not p.exists():
        abort(404)
    return json.loads(p.read_text()).get("events", [])


def _safe_key(s: str) -> str:
    # Firebase keys cannot contain: . $ # [ ] /
    return "".join("_" if c in ".#$[]/" else c for c in s)


def _firebase_put(path: str, payload: dict) -> tuple[bool, str]:
    """PUT payload to Firebase RTDB path. Returns (ok, detail)."""
    try:
        url = f"{FIREBASE_DB_URL}/{path}.json"
        if FIREBASE_DB_SECRET:
            q = urllib.parse.urlencode({"auth": FIREBASE_DB_SECRET})
            url = f"{url}?{q}"
        req = urllib.request.Request(
            url=url,
            method="PUT",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            _ = resp.read()
        return True, path
    except Exception as exc:
        return False, str(exc)


def _load_quiz_items(task_id: str) -> list[dict]:
    p = RECORDINGS_DIR / task_id / "quiz.json"
    if not p.exists():
        return []
    return json.loads(p.read_text())


def _fallback_action_quiz_from_log(task_id: str, target_n: int = 6) -> list[dict]:
    evs = _events(task_id)
    start_ev = next((e for e in evs if e.get("kind") == "session_start"), {})
    end_ev = next((e for e in evs if e.get("kind") == "session_end"), {})
    duration = float(end_ev.get("duration_sec", 0) or 0)
    candidates = []

    # Build candidates from reasoning/action trace.
    for i, ev in enumerate(evs):
        kind = ev.get("kind")
        if kind not in ("reasoning", "action"):
            continue
        # Find nearest reasoning text and next action after this point.
        reasoning = ev.get("text", "") if kind == "reasoning" else ""
        next_action = None
        for nxt in evs[i + 1:]:
            if nxt.get("kind") == "action":
                next_action = nxt.get("action")
                break
        if not reasoning:
            # Try prior reasoning
            for prv in reversed(evs[:i]):
                if prv.get("kind") == "reasoning" and prv.get("text"):
                    reasoning = prv.get("text")
                    break
        if reasoning or next_action:
            rel_sec = 0
            if start_ev.get("ts") and ev.get("ts"):
                rel_sec = max(0, round(float(ev["ts"]) - float(start_ev["ts"])))
            candidates.append({
                "anchor": (reasoning or f"Agent is interacting with the interface around action: {next_action}.")[:240],
                "reference": f"The agent will likely {next_action.replace('_', ' ')} next." if next_action else "The agent will take the next meaningful UI step.",
                "pause_time_sec": rel_sec,
            })

    if not candidates:
        # Generic fallback if logs are minimal.
        step = max(5, int(duration / max(target_n, 1))) if duration else 10
        candidates = [{
            "anchor": "The agent is progressing through the task flow.",
            "reference": "The agent will take the next meaningful UI action.",
            "pause_time_sec": (i + 1) * step,
        } for i in range(target_n)]

    out = []
    idx = 0
    while len(out) < target_n:
        c = candidates[idx % len(candidates)]
        sec = int(c.get("pause_time_sec", 0))
        out.append({
            "id": f"P{len(out) + 1}",
            "type": "next_action_prediction",
            "pause_time_sec": sec,
            "timestamp_label": f"{sec // 60}:{str(sec % 60).zfill(2)}",
            "anchor": c["anchor"],
            "question": "What is the agent's next meaningful action at this moment?",
            "reference_answer": c["reference"],
            "accepted_answers": [c["reference"]],
            "partial_answers": ["The agent will continue by taking the next UI step."],
            "reject_examples": {
                "too_broad": ["The agent will keep going."],
                "too_low_level": ["The cursor will move."],
                "wrong": ["The agent will stop and finish right now."],
            },
        })
        idx += 1
    return out


def _build_action_quiz_set(task_id: str, target_n: int = 6) -> list[dict]:
    """Return action-only quiz items, expanded to target_n when possible.

    Strategy:
    1) keep existing action items
    2) convert non-action items into action-form probes
    3) if still short, clone action probes with a follow-up wording
    """
    source = _load_quiz_items(task_id)
    action_items = []
    converted = []

    for it in source:
        if it.get("type") == "next_action_prediction":
            action_items.append(dict(it))
        else:
            c = dict(it)
            c["type"] = "next_action_prediction"
            c["question"] = "What is the agent's next meaningful action at this moment?"
            converted.append(c)

    out = action_items + converted

    # If still short, duplicate from action-style probes with new IDs/wording.
    i = 0
    base = out[:] if out else action_items[:]
    while len(out) < target_n and base:
        src = dict(base[i % len(base)])
        src["question"] = "Follow-up: what will the agent do next?"
        out.append(src)
        i += 1

    # Final cleanup: cap to target_n and normalize IDs to P1..Pn in timeline order.
    out = out[:target_n]
    if not out:
        return _fallback_action_quiz_from_log(task_id, target_n=target_n)
    out.sort(key=lambda it: it.get("pause_time_sec", 0))
    for idx, it in enumerate(out, start=1):
        it["id"] = f"P{idx}"
        it["type"] = "next_action_prediction"
    return out


@app.route("/api/info/<task_id>")
def get_info(task_id):
    evs   = _events(task_id)
    start = next((e for e in evs if e["kind"] == "session_start"), {})
    end   = next((e for e in evs if e["kind"] == "session_end"),   {})
    return jsonify({
        "task_id":      task_id,
        "task_name":    start.get("task_name", f"Task {task_id}"),
        "started_at":   start.get("started_at", ""),
        "duration_sec": end.get("duration_sec", 0),
        "start_ts":     start.get("ts", 0),
    })


@app.route("/api/log/<task_id>")
def get_log(task_id):
    """Return log events with frame-accurate video_time pre-computed."""
    evs      = _events(task_id)
    start_ts = next((e["ts"] for e in evs if e["kind"] == "session_start"), 0)

    enriched = []
    for e in evs:
        entry = dict(e)
        entry["log_rel"] = round(e["ts"] - start_ts, 3)
        if e["kind"] == "agent_screenshot":
            frame_num = int(e["frame"].split("_")[0])
            entry["video_time"] = round((frame_num - 1) * FRAME_INTERVAL, 2)
        else:
            entry["video_time"] = None
        enriched.append(entry)

    # Forward-fill: carry the last known screenshot time forward to
    # reasoning/action events that follow it.
    last_vid_t = 0.0
    for entry in enriched:
        if entry["video_time"] is not None:
            last_vid_t = entry["video_time"]
        else:
            entry["video_time"] = last_vid_t

    return jsonify({"start_ts": start_ts, "events": enriched})


@app.route("/api/quiz/<task_id>")
def get_quiz(task_id):
    items = _load_quiz_items(task_id)
    if items:
        return jsonify(items)
    return jsonify(_build_action_quiz_set(task_id, target_n=6))


@app.route("/api/tasks")
def list_tasks():
    items = []
    for task_id in QUIZ_TASK_IDS:
        video_path = RECORDINGS_DIR / task_id / "video.mp4"
        if not video_path.exists():
            continue
        task_name = f"Task {task_id}"
        try:
            evs = _events(task_id)
            start = next((e for e in evs if e.get("kind") == "session_start"), {})
            task_name = start.get("task_name", task_name)
        except Exception:
            pass
        items.append({"task_id": task_id, "task_name": task_name})
    return jsonify(items)


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    """Score a single answer with an LLM. Body: {task_id, quiz_id, user_answer}"""
    data = request.get_json(force=True)
    task_id     = data.get("task_id", "8")
    quiz_id     = data.get("quiz_id")
    user_answer = (data.get("user_answer") or "").strip()

    if not user_answer:
        return jsonify({"error": "No answer provided"}), 400

    quiz_path = RECORDINGS_DIR / task_id / "quiz.json"
    if not quiz_path.exists():
        return jsonify({"error": "quiz.json not found"}), 404

    quiz_items = json.loads(quiz_path.read_text())
    q = next((x for x in quiz_items if x["id"] == quiz_id), None)
    if not q:
        return jsonify({"error": f"Quiz id {quiz_id} not found"}), 404

    try:
        result = evaluate_answer(q, user_answer)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/save_responses", methods=["POST"])
def api_save_responses():
    """Save all participant answers/scores to recordings/<task_id>/scores/."""
    data        = request.get_json(force=True)
    task_id     = data.get("task_id", "8")
    participant = (data.get("participant") or "anonymous").strip().replace(" ", "_")
    answers     = data.get("answers", [])

    out_dir = RECORDINGS_DIR / task_id / "scores"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename  = f"{participant}_scores_{timestamp}.json"
    out_path  = out_dir / filename

    payload = {
        "participant":    participant,
        "task_id":        task_id,
        "task_name":      data.get("task_name", ""),
        "submitted_at":   data.get("submitted_at", ""),
        "answers":        answers,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    ts_key = time.strftime("%Y%m%d_%H%M%S")
    fb_path = f"scores/{_safe_key(task_id)}/{_safe_key(participant)}/{ts_key}"
    ok, detail = _firebase_put(fb_path, payload)
    if not ok:
        print(f"[firebase] save_responses failed: {detail}", file=sys.stderr)
    return jsonify({
        "saved": str(out_path.relative_to(Path(__file__).parent)),
        "filename": filename,
        "firebase_saved": ok,
        "firebase_path": detail if ok else None,
        "firebase_error": None if ok else detail,
    })


@app.route("/api/save_progress", methods=["POST"])
def api_save_progress():
    """Write each answered question to its own Firebase path and local file.

    Firebase hierarchy:
      participants/<name>/<task_type>/<question_id>/
        user_response, confidence, score, score_label,
        action_evaluation, answer_time_sec, answered_at
    """
    data        = request.get_json(force=True)
    task_type   = (data.get("task_type") or data.get("task_id") or "unknown").strip()
    participant = (data.get("participant") or "anonymous").strip().replace(" ", "_")
    responses   = data.get("responses", [])

    # ── Local flat file per save ──────────────────────────────────
    out_dir = RECORDINGS_DIR / task_type / "scores"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_path  = out_dir / f"{participant}_{timestamp}.json"
    out_path.write_text(json.dumps(responses, indent=2, ensure_ascii=False))

    # ── Firebase: one write per question  ─────────────────────────
    # Path: participants/<participant>/<task_type>/<question_id>
    errors = []
    saved_paths = []
    for row in responses:
        # Use numeric index (1, 2, 3…) when available; fall back to raw id
        qid = str(
            row.get("question_index")
            or str(row.get("question_id", "")).lstrip("Pp")
            or row.get("question_id", "")
        )
        if not qid:
            continue
        fb_data = {
            "user_response":     row.get("user_response"),
            "confidence":        row.get("confidence"),
            "score":             row.get("score"),
            "score_label":       row.get("score_label"),
            "action_evaluation": row.get("action_evaluation"),
            "answer_time_sec":   row.get("answer_time_sec"),
            "answered_at":       row.get("answered_at"),
        }
        fb_data = {k: v for k, v in fb_data.items() if v is not None}
        fb_path = (
            f"participants/{_safe_key(participant)}"
            f"/{_safe_key(task_type)}"
            f"/{qid}"
        )
        ok, detail = _firebase_put(fb_path, fb_data)
        if ok:
            saved_paths.append(fb_path)
        else:
            errors.append(f"{qid}: {detail}")
            print(f"[firebase] {fb_path} failed: {detail}", file=sys.stderr)

    return jsonify({
        "saved_local": str(out_path.relative_to(Path(__file__).parent)),
        "firebase_saved": len(saved_paths),
        "firebase_errors": errors,
    })


@app.route("/recordings/<task_id>/video.mp4")
def serve_video(task_id):
    video_path = RECORDINGS_DIR / task_id / "video.mp4"
    if not video_path.exists():
        abort(404)

    file_size    = video_path.stat().st_size
    range_header = request.headers.get("Range")

    if range_header:
        byte1, byte2 = 0, file_size - 1
        rng   = range_header.strip().split("=")[1]
        parts = rng.split("-")
        byte1 = int(parts[0])
        if parts[1]:
            byte2 = int(parts[1])
        length = byte2 - byte1 + 1

        with open(video_path, "rb") as f:
            f.seek(byte1)
            data = f.read(length)

        resp = Response(data, 206, mimetype="video/mp4")
        resp.headers["Content-Range"]  = f"bytes {byte1}-{byte2}/{file_size}"
        resp.headers["Accept-Ranges"]  = "bytes"
        resp.headers["Content-Length"] = str(length)
        return resp

    return send_file(str(video_path), mimetype="video/mp4", conditional=True)


# ─────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Agent Quiz Reviewer</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:        #f0f0f5;
    --surface:   #ffffff;
    --border:    #dde0e8;
    --text:      #1a1a2e;
    --muted:     #6b7280;
    --accent:    #4f46e5;
    --accent-lt: #ede9fe;
    --green:     #16a34a;  --green-lt:  #dcfce7;
    --amber:     #d97706;  --amber-lt:  #fef3c7;
    --red:       #dc2626;  --red-lt:    #fee2e2;
    --blue:      #0ea5e9;  --blue-lt:   #e0f2fe;
    --radius: 10px;
    --shadow: 0 1px 4px rgba(0,0,0,.10), 0 4px 16px rgba(0,0,0,.06);
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 14px;
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  /* ── Header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 9px 18px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }
  header h1 { font-size: 14px; font-weight: 700; color: var(--accent); white-space: nowrap; }
  #task-name { font-size: 12px; color: var(--muted); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #task-select {
    font-size: 12px;
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #fff;
    color: var(--text);
    min-width: 140px;
  }
  #task-select:focus { border-color: var(--accent); outline: none; }

  /* Participant input */
  #participant-wrap {
    display: flex; align-items: center; gap: 6px; flex-shrink: 0;
  }
  #participant-wrap label { font-size: 11px; color: var(--muted); white-space: nowrap; }
  #participant-input {
    width: 120px; padding: 4px 9px;
    border: 1.5px solid var(--border); border-radius: 7px;
    font-family: inherit; font-size: 12px; color: var(--text);
    background: #fafafe; outline: none; transition: border-color .15s;
  }
  #participant-input:focus { border-color: var(--accent); background: #fff; }
  #participant-input::placeholder { color: #9ca3af; }

  .hdr-btn {
    padding: 5px 13px; border-radius: 7px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-size: 12px;
    cursor: pointer; white-space: nowrap; font-family: inherit; transition: background .12s;
    flex-shrink: 0;
  }
  .hdr-btn:hover { background: var(--bg); }
  .hdr-btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .hdr-btn.primary:hover { background: #4338ca; }
  .hdr-btn.save    { background: var(--green); color: #fff; border-color: var(--green); }
  .hdr-btn.save:hover { background: #15803d; }
  .hdr-btn.danger  { color: var(--red); border-color: #fca5a5; }
  .hdr-btn.danger:hover  { background: var(--red-lt); }

  /* ── Main layout ── */
  main { display: flex; flex: 1; overflow: hidden; }

  /* ── Left: video panel ── */
  #video-panel {
    width: 58%; flex-shrink: 0;
    background: #111;
    display: flex; flex-direction: column;
    border-right: 1px solid var(--border);
  }
  #video-wrap {
    flex: 1; display: flex; align-items: center; justify-content: center;
    overflow: hidden; position: relative;
  }
  #vid { max-width: 100%; max-height: 100%; display: block; }

  /* controls bar */
  #controls {
    background: #1a1a2e; padding: 8px 14px;
    display: flex; align-items: center; gap: 10px; flex-shrink: 0;
  }
  #btn-play {
    background: none; border: none; color: #fff;
    font-size: 18px; cursor: pointer; padding: 0 4px; line-height: 1;
  }
  #progress-wrap {
    flex: 1; height: 5px; background: #3a3a5c;
    border-radius: 3px; cursor: pointer; position: relative;
  }
  #progress-fill { height: 100%; background: var(--accent); border-radius: 3px; width: 0%; pointer-events: none; }
  .progress-marker {
    position: absolute; top: -3px; width: 3px; height: 11px;
    background: #f59e0b; border-radius: 2px;
    transform: translateX(-50%); pointer-events: none;
  }
  #time-display { color: #9ca3af; font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  #btn-log-toggle {
    background: none; border: 1px solid #3a3a5c; color: #9ca3af;
    font-size: 11px; padding: 3px 9px; border-radius: 5px; cursor: pointer;
    font-family: inherit; white-space: nowrap; flex-shrink: 0;
    transition: border-color .12s, color .12s;
  }
  #btn-log-toggle.active { border-color: #a78bfa; color: #a78bfa; }

  /* ── Log strip (collapsible) ── */
  #log-strip {
    background: #11111f;
    border-top: 1px solid #2a2a40;
    padding: 8px 14px;
    flex-shrink: 0;
    display: none;
    max-height: 120px;
    overflow-y: auto;
  }
  #log-strip.visible { display: block; }
  #log-strip::-webkit-scrollbar { width: 3px; }
  #log-strip::-webkit-scrollbar-thumb { background: #3a3a5c; }
  .log-step-hdr {
    font-size: 10px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: #6b7280; margin-bottom: 5px;
  }
  .log-item {
    display: flex; gap: 8px; align-items: baseline;
    margin-bottom: 4px; font-size: 12px; line-height: 1.45;
  }
  .log-item-kind {
    font-size: 9px; font-weight: 700; letter-spacing: .07em;
    text-transform: uppercase; padding: 1px 6px; border-radius: 10px;
    flex-shrink: 0;
  }
  .log-item-kind.reasoning { background: #1e2d50; color: #7aadff; }
  .log-item-kind.action    { background: #1a2e20; color: #5dba78; }
  .log-item-text { color: #c4c4d8; flex: 1; }

  /* ── Right: quiz panel ── */
  #quiz-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }

  /* Active quiz card */
  #active-zone {
    flex-shrink: 0; max-height: 68%;
    overflow-y: auto; padding: 14px;
    border-bottom: 1px solid var(--border);
  }
  #active-zone::-webkit-scrollbar { width: 4px; }
  #active-zone::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  #no-quiz-msg {
    padding: 24px 16px; color: var(--muted);
    text-align: center; font-size: 13px;
  }
  #no-quiz-msg .icon { font-size: 26px; display: block; margin-bottom: 6px; }

  .quiz-card { background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
  .quiz-card-header {
    padding: 10px 14px; display: flex; align-items: center; gap: 10px;
    border-bottom: 1px solid var(--border);
  }
  .type-badge {
    font-size: 10px; font-weight: 700; letter-spacing: .07em;
    text-transform: uppercase; padding: 3px 9px; border-radius: 20px;
  }
  .type-badge.goal { background: #ede9fe; color: #6d28d9; }
  .type-badge.pred { background: #cffafe; color: #0e7490; }
  .quiz-id  { font-size: 12px; font-weight: 700; color: var(--muted); }
  .quiz-ts  { font-size: 12px; color: var(--muted); margin-left: auto; font-variant-numeric: tabular-nums; }

  .quiz-card-body { padding: 13px 14px; }
  .quiz-instruction {
    font-size: 12px; color: var(--muted); font-style: italic;
    margin-bottom: 8px; line-height: 1.5;
  }
  .quiz-anchor {
    font-size: 11px; color: var(--muted); background: #f8f8fb;
    border-left: 3px solid var(--border); padding: 5px 10px;
    border-radius: 0 5px 5px 0; margin-bottom: 10px; line-height: 1.5;
  }
  .quiz-question { font-size: 14px; font-weight: 600; line-height: 1.55; margin-bottom: 11px; }
  #answer-textarea {
    width: 100%; min-height: 68px;
    border: 1.5px solid var(--border); border-radius: 7px;
    padding: 8px 11px; font-family: inherit; font-size: 14px;
    color: var(--text); resize: vertical; outline: none;
    background: #fafafe; transition: border-color .15s;
  }
  #answer-textarea:focus { border-color: var(--accent); background: #fff; }
  #answer-textarea::placeholder { color: #9ca3af; }

  /* Score badge displayed after evaluation */
  .score-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;
    margin-top: 8px;
  }
  .score-badge.correct   { background: var(--green-lt);  color: var(--green); }
  .score-badge.partial   { background: var(--amber-lt);  color: var(--amber); }
  .score-badge.incorrect { background: var(--red-lt);    color: var(--red); }
  .score-explanation { font-size: 12px; color: var(--muted); margin-top: 5px; line-height: 1.5; font-style: italic; }
  .score-spinner { display: inline-block; width: 14px; height: 14px;
    border: 2px solid #dde0e8; border-top-color: var(--accent);
    border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .quiz-actions { display: flex; gap: 8px; margin-top: 9px; flex-wrap: wrap; align-items: center; }
  .q-btn {
    padding: 6px 14px; border-radius: 7px; border: 1px solid var(--border);
    background: var(--surface); font-family: inherit; font-size: 13px;
    cursor: pointer; font-weight: 500; transition: background .12s; color: var(--text);
    white-space: nowrap;
  }
  .q-btn:hover  { background: var(--bg); }
  .q-btn:disabled { opacity: .5; cursor: default; }
  .q-btn.cont   { background: var(--accent); color: #fff; border-color: var(--accent); }
  .q-btn.cont:hover  { background: #4338ca; }
  .q-btn.score  { background: #0ea5e9; color: #fff; border-color: #0ea5e9; }
  .q-btn.score:hover { background: #0284c7; }
  .workflow-note { margin-top: 8px; font-size: 11px; color: var(--muted); }
  .conf-wrap, .action-eval-wrap {
    margin-top: 10px; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: 8px; background: #fafafe;
  }
  .action-eval-wrap.required {
    border-color: var(--accent); background: var(--accent-lt);
  }
  .action-eval-wrap.pending {
    opacity: 0.45; pointer-events: none;
  }
  .conf-label, .action-eval-label { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
  .conf-scale, .action-eval-options { display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px; }
  .conf-scale label, .action-eval-options label { display: inline-flex; align-items: center; gap: 4px; }

  /* Reference panel */
  #reference-panel { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 11px; }
  .ref-section { margin-bottom: 9px; }
  .ref-label { font-size: 10px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
  .ref-answer { font-size: 13px; line-height: 1.5; padding: 8px 11px; border-radius: 7px; border-left: 3px solid; }
  .ref-answer.reference { background: var(--green-lt); border-color: var(--green); color: #14532d; }
  .ref-tags { display: flex; flex-wrap: wrap; gap: 5px; }
  .ref-tag { font-size: 11px; padding: 2px 8px; border-radius: 20px; font-weight: 500; }
  .ref-tag.accepted { background: var(--green-lt); color: var(--green); }
  .ref-tag.partial  { background: var(--amber-lt); color: var(--amber); }
  .ref-tag.rejected { background: var(--red-lt);   color: var(--red); }
  .reject-group { margin-bottom: 5px; }
  .reject-group-label { font-size: 11px; color: var(--muted); margin-bottom: 3px; }

  /* ── Quiz list ── */
  #quiz-list-zone { flex: 1; overflow-y: auto; }
  #quiz-list-zone::-webkit-scrollbar { width: 4px; }
  #quiz-list-zone::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  #list-header {
    padding: 7px 14px; font-size: 10px; font-weight: 700;
    letter-spacing: .07em; text-transform: uppercase; color: var(--muted);
    background: var(--bg); border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0; z-index: 1;
  }
  .quiz-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 14px; border-bottom: 1px solid var(--border);
    cursor: pointer; background: var(--surface); transition: background .1s;
  }
  .quiz-row:hover      { background: #f5f3ff; }
  .quiz-row.active-row { background: var(--accent-lt); }
  .row-id  { font-size: 11px; font-weight: 700; color: var(--muted); min-width: 26px; }
  .row-ts  { font-size: 11px; color: var(--muted); min-width: 38px; font-variant-numeric: tabular-nums; }
  .row-q   { font-size: 12px; color: var(--text); flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .status-dot.unseen   { background: #d1d5db; }
  .status-dot.active   { background: var(--accent); box-shadow: 0 0 0 3px #c7d2fe; }
  .status-dot.answered { background: var(--green); }
  .status-dot.revealed { background: #a78bfa; }
  .status-dot.skipped  { background: var(--amber); }
  .row-score-pill {
    font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 20px;
    white-space: nowrap; flex-shrink: 0;
  }
  .row-score-pill.correct   { background: var(--green-lt);  color: var(--green); }
  .row-score-pill.partial   { background: var(--amber-lt);  color: var(--amber); }
  .row-score-pill.incorrect { background: var(--red-lt);    color: var(--red); }
  .row-jump {
    font-size: 11px; padding: 2px 8px; border-radius: 5px;
    border: 1px solid var(--border); background: var(--bg);
    cursor: pointer; font-family: inherit; color: var(--muted); flex-shrink: 0;
  }
  .row-jump:hover { border-color: var(--accent); color: var(--accent); }

  #quiz-bottom-actions {
    position: sticky;
    bottom: 0;
    padding: 10px 14px;
    border-top: 1px solid var(--border);
    background: #fff;
    display: flex;
    justify-content: flex-end;
  }
  #btn-save-bottom {
    padding: 7px 12px;
    border-radius: 8px;
    border: 1px solid var(--green);
    background: var(--green);
    color: #fff;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  #btn-save-bottom:hover { background: #15803d; }

  /* ── Save confirmation toast ── */
  #toast {
    position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
    background: #1a1a2e; color: #fff; padding: 10px 20px; border-radius: 8px;
    font-size: 13px; box-shadow: 0 4px 20px rgba(0,0,0,.25);
    opacity: 0; transition: opacity .3s; pointer-events: none; z-index: 999;
  }
  #toast.show { opacity: 1; }
</style>
</head>
<body>

<header>
  <h1>⬡ Quiz Reviewer</h1>
  <select id="task-select" title="Choose task"></select>
  <span id="task-name">Loading…</span>
  <div id="participant-wrap">
    <label for="participant-input">Participant:</label>
    <input id="participant-input" type="text" placeholder="name" autocomplete="off"/>
  </div>
  <button class="hdr-btn danger"  id="btn-reset">Reset</button>
</header>

<main>
  <!-- Left: video -->
  <div id="video-panel">
    <div id="video-wrap">
      <video id="vid" preload="auto"></video>
    </div>
    <div id="controls">
      <button id="btn-play" title="Space">▶</button>
      <div id="progress-wrap">
        <div id="progress-fill"></div>
      </div>
      <span id="time-display">0:00 / 0:00</span>
      <button id="btn-log-toggle" title="Toggle agent log">Show log</button>
    </div>
    <div id="log-strip">
      <div class="log-step-hdr" id="log-step-hdr">Agent log</div>
      <div id="log-items"></div>
    </div>
  </div>

  <!-- Right: quiz panel -->
  <div id="quiz-panel">
    <div id="active-zone">
      <div id="no-quiz-msg"><span class="icon">🎬</span>Play the video — quiz probes appear automatically.</div>
    </div>
    <div id="quiz-list-zone">
      <div id="list-header">
        <span>ALL PROBES</span>
        <span id="progress-summary"></span>
      </div>
      <div id="quiz-list"></div>
    </div>
  </div>
</main>

<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────
const S = {
  taskId: null, taskName: '',
  quizItems: [],
  logEvents: [],
  answers:    {},      // id -> {userAnswer, answeredAt, score, score_label, score_explanation}
  seenIds:    new Set(),
  revealedIds: new Set(),
  refShownIds: new Set(),
  activeId: null,
  duration: 0,
  logVisible: false,
  evaluating: new Set(),  // ids currently being scored
};

// ── DOM refs ────────────────────────────────────────────────────
const vid          = document.getElementById('vid');
const btnPlay      = document.getElementById('btn-play');
const progressWrap = document.getElementById('progress-wrap');
const progressFill = document.getElementById('progress-fill');
const timeDisplay  = document.getElementById('time-display');
const activeZone   = document.getElementById('active-zone');
const quizListEl   = document.getElementById('quiz-list');
const taskNameEl   = document.getElementById('task-name');
const progSummary  = document.getElementById('progress-summary');
const logStrip     = document.getElementById('log-strip');
const logItemsEl   = document.getElementById('log-items');
const logStepHdr   = document.getElementById('log-step-hdr');
const btnLogToggle = document.getElementById('btn-log-toggle');
const taskSelectEl  = document.getElementById('task-select');
const participantEl = document.getElementById('participant-input');
const toast         = document.getElementById('toast');
const QUIZ_TASK_IDS = ['s1', 's2', 's3', 't1', 't2', 't3', 'legible_t1', 'legible_t1_3'];
let _autosaveTimer = null;
let _autosaveDirty = false;

// ── Init ────────────────────────────────────────────────────────
async function init() {
  const params = new URLSearchParams(location.search);
  const requestedTask = params.get('task');
  startAutosave();
  await loadTaskOptions();
  const defaultTask = (requestedTask && QUIZ_TASK_IDS.includes(requestedTask))
    ? requestedTask
    : (taskSelectEl.value || 's1');
  taskSelectEl.value = defaultTask;
  await loadTask(defaultTask);
}

async function loadTaskOptions() {
  const res = await fetch('/api/tasks');
  const tasks = res.ok ? await res.json() : [];
  const options = tasks.length
    ? tasks
    : QUIZ_TASK_IDS.map(task_id => ({ task_id, task_name: `Task ${task_id}` }));
  taskSelectEl.innerHTML = options.map(
    t => `<option value="${t.task_id}">${t.task_id.toUpperCase()} — ${t.task_name}</option>`
  ).join('');
}

function resetTaskState(taskId) {
  Object.assign(S, {
    taskId,
    taskName: '',
    quizItems: [],
    logEvents: [],
    answers: {},
    seenIds: new Set(),
    revealedIds: new Set(),
    refShownIds: new Set(),
    activeId: null,
    duration: 0,
    evaluating: new Set(),
    answerStartAt: {},
  });
  vid.pause();
  vid.currentTime = 0;
}

async function loadTask(taskId) {
  resetTaskState(taskId);
  taskNameEl.textContent = `Loading task ${taskId}...`;

  const [infoRes, quizRes, logRes] = await Promise.all([
    fetch(`/api/info/${taskId}`),
    fetch(`/api/quiz/${taskId}`),
    fetch(`/api/log/${taskId}`),
  ]);
  if (!infoRes.ok || !quizRes.ok || !logRes.ok) {
    taskNameEl.textContent = `Error loading task ${taskId}`;
    renderQuizList();
    renderActiveQuiz();
    return;
  }

  const info = await infoRes.json();
  const quiz = await quizRes.json();
  const log = await logRes.json();

  S.taskName = info.task_name;
  S.quizItems = quiz.slice().sort((a, b) => a.pause_time_sec - b.pause_time_sec);
  S.logEvents = (log.events || []).filter(e => e.video_time !== null && e.video_time !== undefined);

  taskNameEl.textContent = `Task ${taskId}: ${info.task_name}`;
  vid.src = `/recordings/${taskId}/video.mp4`;

  renderQuizList();
  renderActiveQuiz();
}

// ── Video events ────────────────────────────────────────────────
vid.addEventListener('loadedmetadata', () => {
  S.duration = vid.duration;
  renderQuizMarkers();
});

vid.addEventListener('timeupdate', () => {
  const t = vid.currentTime;
  if (S.duration > 0) progressFill.style.width = (t / S.duration * 100) + '%';
  timeDisplay.textContent = `${fmt(t)} / ${fmt(S.duration)}`;

  if (!vid.paused) {
    for (const q of S.quizItems) {
      if (!S.seenIds.has(q.id) &&
          t >= q.pause_time_sec - 0.1 &&
          t <= q.pause_time_sec + 2.0) {
        triggerQuiz(q.id);
        break;
      }
    }
  }

  if (S.logVisible) updateLogStrip(t);
});

vid.addEventListener('play',  () => { btnPlay.textContent = '⏸'; });
vid.addEventListener('pause', () => { btnPlay.textContent = '▶'; });

btnPlay.addEventListener('click', togglePlay);

progressWrap.addEventListener('click', e => {
  if (!S.duration) return;
  const rect = progressWrap.getBoundingClientRect();
  vid.currentTime = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * S.duration;
});

btnLogToggle.addEventListener('click', () => {
  S.logVisible = !S.logVisible;
  logStrip.classList.toggle('visible', S.logVisible);
  btnLogToggle.classList.toggle('active', S.logVisible);
  btnLogToggle.textContent = S.logVisible ? 'Hide log' : 'Show log';
  if (S.logVisible) updateLogStrip(vid.currentTime);
});

// ── Keyboard shortcuts ──────────────────────────────────────────
document.addEventListener('keydown', e => {
  const inText = ['TEXTAREA','INPUT'].includes(document.activeElement.tagName);
  if (e.code === 'Space' && !inText) { e.preventDefault(); togglePlay(); }
});

document.getElementById('btn-reset').addEventListener('click', resetAll);
taskSelectEl.addEventListener('change', async () => {
  await loadTask(taskSelectEl.value);
  queueAutosave();
});
participantEl.addEventListener('change', queueAutosave);

function queueAutosave() { _autosaveDirty = true; }

function syncActiveDraft() {
  if (!S.activeId) return;
  const ta = document.getElementById('answer-textarea');
  if (!ta) return;
  const txt = (ta.value || '').trim();
  if (!S.answers[S.activeId]) S.answers[S.activeId] = {};
  if (txt) {
    S.answers[S.activeId].userAnswer = txt;
    S.answers[S.activeId].answeredAt = S.answers[S.activeId].answeredAt || new Date().toISOString();
  }
}

async function saveProgress(reason = 'autosave') {
  syncActiveDraft();
  if (!_autosaveDirty && reason === 'autosave') return;
  const payload = buildExportPayload();
  payload.participant = participantEl.value.trim() || 'anonymous';
  payload.save_reason = reason;
  try {
    await fetch('/api/save_progress', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    _autosaveDirty = false;
  } catch (_err) {
    // Keep quiet in UI; next interval will retry.
  }
}

function startAutosave() {
  if (_autosaveTimer) clearInterval(_autosaveTimer);
  _autosaveTimer = setInterval(() => { saveProgress('autosave'); }, 15000);
}

// ── Core actions ────────────────────────────────────────────────
function togglePlay() { vid.paused ? vid.play() : vid.pause(); }

function triggerQuiz(id) {
  vid.pause();
  S.seenIds.add(id);
  if (!S.answerStartAt[id]) {
    S.answerStartAt[id] = new Date().toISOString();
  }
  S.activeId = id;
  renderActiveQuiz();
  renderQuizList();
  queueAutosave();
  document.querySelector(`[data-quiz-id="${id}"]`)
    ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function continueVideo() {
  const id = S.activeId;
  const a = S.answers[id] || {};
  if (a.score == null) {
    showToast('Score this response first.', 2200);
    return;
  }
  if (!a.actionEvaluation) {
    showToast('Please rate action performance before continuing.', 2400);
    return;
  }
  S.activeId = null;
  renderActiveQuiz();
  renderQuizList();
  queueAutosave();
  vid.play();
}

function saveCurrentAnswer(id) {
  const ta = document.getElementById('answer-textarea');
  const txt = ta?.value.trim() || '';
  if (txt) {
    if (!S.answers[id]) S.answers[id] = {};
    S.answers[id].userAnswer = txt;
    S.answers[id].answeredAt = S.answers[id].answeredAt || new Date().toISOString();
    queueAutosave();
  }
}

async function scoreAnswer(id) {
  saveCurrentAnswer(id);
  const answerObj = S.answers[id] || {};
  const userAnswer = answerObj.userAnswer || '';
  if (!userAnswer) {
    showToast('Type an answer first before scoring.', 2000);
    return;
  }
  const confidence = Number(answerObj.confidence || 0);
  if (!confidence || confidence < 1 || confidence > 7) {
    showToast('Select confidence 1–7 before scoring.', 2200);
    return;
  }

  S.evaluating.add(id);
  renderActiveQuiz();  // show spinner

  try {
    const res = await fetch('/api/evaluate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: S.taskId, quiz_id: id, user_answer: userAnswer }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    if (!S.answers[id]) S.answers[id] = { userAnswer, answeredAt: new Date().toISOString() };
    S.answers[id].score             = data.score;
    S.answers[id].score_label       = data.label;
    S.answers[id].score_explanation = data.explanation;
    S.revealedIds.add(id);
    S.refShownIds.add(id);
    queueAutosave();
  } catch (err) {
    showToast(`Scoring failed: ${err.message}`, 4000);
  } finally {
    S.evaluating.delete(id);
    renderActiveQuiz();
    renderQuizList();
    // Scroll action eval section into view so user doesn't miss it
    setTimeout(() => {
      document.getElementById('action-eval-section')
        ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 80);
  }
}

function toggleReference(id) {
  saveCurrentAnswer(id);
  S.revealedIds.add(id);
  if (S.refShownIds.has(id)) {
    S.refShownIds.delete(id);
  } else {
    S.refShownIds.add(id);
  }
  renderActiveQuiz();
  renderQuizList();
}

function jumpToQuiz(id) {
  const q = S.quizItems.find(q => q.id === id);
  if (!q) return;
  S.seenIds.delete(id);
  S.activeId = null;
  renderActiveQuiz();
  vid.currentTime = Math.max(0, q.pause_time_sec - 1);
  vid.play();
}

function jumpToNextUnanswered() {
  const next = S.quizItems.find(q => {
    const a = S.answers[q.id];
    return !a || a.score == null;
  });
  if (next) jumpToQuiz(next.id);
}

function resetAll() {
  if (!confirm('Clear all answers and reset seen status?')) return;
  Object.assign(S, {
    answers: {}, seenIds: new Set(), revealedIds: new Set(),
    refShownIds: new Set(), activeId: null, evaluating: new Set(),
  });
  renderActiveQuiz();
  renderQuizList();
  queueAutosave();
}

window.addEventListener('beforeunload', () => { saveProgress('beforeunload'); });

function buildExportPayload() {
  syncActiveDraft();
  const participant = participantEl.value.trim() || 'anonymous';
  const rows = [];
  for (const q of S.quizItems) {
    const a = S.answers[q.id] || {};
    const hasUserData = !!(
      a.userAnswer ||
      a.confidence != null ||
      a.score != null ||
      a.actionEvaluation
    );
    if (!hasUserData) continue;

    const startIso = S.answerStartAt[q.id] || null;
    const endIso = a.answeredAt || null;
    let elapsed = null;
    if (startIso && endIso) {
      const dt = (new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000;
      if (!Number.isNaN(dt)) elapsed = Math.max(0, Math.round(dt));
    }

    rows.push({
      participant: participant,
      task_type: S.taskId,
      question_id: q.id,
      question_index: Number(String(q.id).replace(/^P/i, "")) || null,
      user_response: a.userAnswer || null,
      confidence: a.confidence ?? null,
      score: a.score ?? null,
      score_label: a.score_label || null,
      action_evaluation: a.actionEvaluation || null,
      answer_started_at: startIso,
      answered_at: endIso,
      answer_time_sec: elapsed,
    });
  }
  return {
    participant,
    task_type:    S.taskId,
    task_name:    S.taskName,
    submitted_at: new Date().toISOString(),
    responses: rows,
  };
}

// ── Toast helper ────────────────────────────────────────────────
let _toastTimer = null;
function showToast(msg, ms = 3000) {
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.remove('show'), ms);
}

// ── Render: active quiz card ────────────────────────────────────
function renderActiveQuiz() {
  activeZone.innerHTML = '';
  if (!S.activeId) {
    const done = Object.values(S.answers).filter(a => a.userAnswer).length;
    const scored = Object.values(S.answers).filter(a => a.score != null).length;
    const evald = Object.values(S.answers).filter(a => a.actionEvaluation).length;
    const scoredNote = scored > 0 ? ` · ${scored} scored` : '';
    activeZone.innerHTML = `<div id="no-quiz-msg">
      <span class="icon">${evald === S.quizItems.length && S.quizItems.length > 0 ? '✅' : '🎬'}</span>
      ${evald === S.quizItems.length && S.quizItems.length > 0
        ? `All probes completed${scoredNote} — click <strong>Save Results</strong>.`
        : 'Play the video — quiz probes appear automatically.'}
    </div>`;
    return;
  }

  const q        = S.quizItems.find(q => q.id === S.activeId);
  const existing = S.answers[q.id]?.userAnswer || '';
  const isGoal   = q.type === 'goal_legibility';
  const scoreData = S.answers[q.id];
  const isEvaluating = S.evaluating.has(q.id);
  const locked = scoreData?.score != null;
  const confidence = scoreData?.confidence || '';
  const actionEval = scoreData?.actionEvaluation || '';
  const canContinue = locked && !!actionEval;

  const instruction = isGoal
    ? '💡 Answer the <strong>immediate local subgoal</strong>.'
    : '';

  // score display HTML
  let scoreHTML = '';
  if (isEvaluating) {
    scoreHTML = `<div style="margin-top:8px;display:flex;align-items:center;gap:8px">
      <div class="score-spinner"></div>
      <span style="font-size:12px;color:var(--muted)">Scoring with LLM…</span>
    </div>`;
  } else if (scoreData?.score != null) {
    const lbl = scoreData.score_label || 'incorrect';
    const num = scoreData.score;
    scoreHTML = `
      <div class="score-badge ${lbl}">
        ${scoreIcon(lbl)} ${capitalize(lbl)} — ${num}/3
      </div>
      <div class="score-explanation">${esc(scoreData.score_explanation || '')}</div>`;
  }

  const card = document.createElement('div');
  card.className = 'quiz-card';
  card.innerHTML = `
    <div class="quiz-card-header">
      <span class="type-badge ${isGoal ? 'goal' : 'pred'}">${isGoal ? 'Goal Legibility' : 'Next Action'}</span>
      <span class="quiz-id">${q.id}</span>
      <span class="quiz-ts">⏱ ${q.timestamp_label}</span>
    </div>
    <div class="quiz-card-body">
      <p class="quiz-instruction">${instruction}</p>
      <div class="quiz-anchor">📍 ${esc(q.anchor)}</div>
      <p class="quiz-question">${esc(q.question)}</p>
      <textarea id="answer-textarea" placeholder="Type your answer here…" rows="3" ${locked ? 'disabled' : ''}>${esc(existing)}</textarea>
      <div class="conf-wrap">
        <div class="conf-label">How confident are you in your prediction? (1 = low, 7 = high)</div>
        <div class="conf-scale">
          ${[1,2,3,4,5,6,7].map(n => `
            <label>
              <input type="radio" name="confidence" value="${n}" ${String(confidence) === String(n) ? 'checked' : ''} ${locked ? 'disabled' : ''}/>
              ${n}
            </label>`).join('')}
        </div>
      </div>
      ${scoreHTML}
      <div class="quiz-actions">
        <button class="q-btn score" id="btn-score" ${(isEvaluating || locked) ? 'disabled' : ''}>
          ${isEvaluating ? '…' : (locked ? '✅ Scored' : '🤖 Score')}
        </button>
      </div>
      <div id="reference-panel" style="display:${locked ? 'block' : 'none'}">
        ${locked ? buildRefHTML(q) : ''}
      </div>
      <div id="action-eval-section" class="action-eval-wrap ${!locked ? 'pending' : (!actionEval ? 'required' : '')}">
        <div class="action-eval-label">${locked ? '⭐ Required — ' : ''}Was this a good action toward the task goal?${!locked ? '<span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">(available after scoring)</span>' : ''}</div>
        <div class="action-eval-options">
          <label><input type="radio" name="action-eval" value="good" ${actionEval === 'good' ? 'checked' : ''} ${!locked ? 'disabled' : ''}/> Good action</label>
          <label><input type="radio" name="action-eval" value="bad" ${actionEval === 'bad' ? 'checked' : ''} ${!locked ? 'disabled' : ''}/> Bad action</label>
          <label><input type="radio" name="action-eval" value="not_sure" ${actionEval === 'not_sure' ? 'checked' : ''} ${!locked ? 'disabled' : ''}/> Not sure</label>
        </div>
      </div>
      <div class="quiz-actions" style="margin-top:10px">
        <button class="q-btn cont" id="btn-continue" ${canContinue ? '' : 'disabled'}>▶ Continue video</button>
        ${!canContinue && locked ? `<span style="font-size:11px;color:var(--muted);align-self:center">${!actionEval ? 'Rate the action above to continue' : ''}</span>` : ''}
      </div>
    </div>`;
  activeZone.appendChild(card);

  document.getElementById('btn-continue').addEventListener('click', continueVideo);
  document.getElementById('btn-score').addEventListener('click', () => scoreAnswer(S.activeId));
  document.getElementById('answer-textarea')?.addEventListener('input', () => {
    syncActiveDraft();
    queueAutosave();
  });
  document.querySelectorAll('input[name="confidence"]').forEach(r => {
    r.addEventListener('change', () => {
      if (!S.answers[S.activeId]) S.answers[S.activeId] = {};
      S.answers[S.activeId].confidence = Number(r.value);
      queueAutosave();
    });
  });
  document.querySelectorAll('input[name="action-eval"]').forEach(r => {
    r.addEventListener('change', () => {
      if (!S.answers[S.activeId]) S.answers[S.activeId] = {};
      S.answers[S.activeId].actionEvaluation = r.value;
      queueAutosave();
      renderQuizList();
      renderActiveQuiz();
    });
  });

  if (!existing) document.getElementById('answer-textarea')?.focus();
}

function scoreIcon(label) {
  return { correct: '✓', partial: '~', incorrect: '✗' }[label] || '✗';
}
function capitalize(s) { return s ? s[0].toUpperCase() + s.slice(1) : ''; }

function buildRefHTML(q) {
  let h = `<div class="ref-section">
    <div class="ref-label">Reference answer</div>
    <div class="ref-answer reference">${esc(q.reference_answer)}</div>
  </div>`;
  if (q.accepted_answers?.length) {
    h += `<div class="ref-section"><div class="ref-label">Accepted ✓</div>
      <div class="ref-tags">${q.accepted_answers.map(a => `<span class="ref-tag accepted">${esc(a)}</span>`).join('')}</div></div>`;
  }
  if (q.partial_answers?.length) {
    h += `<div class="ref-section"><div class="ref-label">Partial ~</div>
      <div class="ref-tags">${q.partial_answers.map(a => `<span class="ref-tag partial">${esc(a)}</span>`).join('')}</div></div>`;
  }
  const rej = q.reject_examples || {};
  const rejKeys = Object.keys(rej);
  if (rejKeys.length) {
    h += `<div class="ref-section"><div class="ref-label">Reject examples ✗</div>`;
    for (const k of rejKeys) {
      h += `<div class="reject-group">
        <div class="reject-group-label">${esc(k.replace(/_/g,' '))}:</div>
        <div class="ref-tags">${(rej[k]||[]).map(a=>`<span class="ref-tag rejected">${esc(a)}</span>`).join('')}</div>
      </div>`;
    }
    h += `</div>`;
  }
  return h;
}

// ── Render: log strip ───────────────────────────────────────────
function updateLogStrip(t) {
  const relevant = S.logEvents.filter(e =>
    (e.kind === 'reasoning' || e.kind === 'action') && e.video_time <= t + 0.5
  );
  const lastReasoning = [...relevant].reverse().find(e => e.kind === 'reasoning');
  const lastAction    = [...relevant].reverse().find(e => e.kind === 'action');

  const shots = S.logEvents.filter(e => e.kind === 'agent_screenshot' && e.video_time <= t + 0.5);
  const stepN = shots.length;
  const total = S.logEvents.filter(e => e.kind === 'agent_screenshot').length;
  logStepHdr.textContent = `Agent log — step ${stepN} / ${total}`;

  let html = '';
  if (lastReasoning) {
    html += `<div class="log-item">
      <span class="log-item-kind reasoning">reasoning</span>
      <span class="log-item-text">${esc(lastReasoning.text || '')}</span>
    </div>`;
  }
  if (lastAction) {
    const p = lastAction.params || {};
    const coord = p.coordinate ? ` (${p.coordinate[0]}, ${p.coordinate[1]})` : '';
    const txt   = p.text ? ` "${String(p.text).slice(0, 40)}"` : '';
    html += `<div class="log-item">
      <span class="log-item-kind action">action</span>
      <span class="log-item-text">${esc(lastAction.action)}${esc(coord || txt)}</span>
    </div>`;
  }
  if (!html) html = `<span style="color:#6b7280;font-size:12px">No events yet at this timestamp.</span>`;
  logItemsEl.innerHTML = html;
}

// ── Render: quiz list ───────────────────────────────────────────
function renderQuizList() {
  const answered = Object.values(S.answers).filter(a => a.userAnswer).length;
  const scored   = Object.values(S.answers).filter(a => a.score != null).length;
  progSummary.textContent = `${answered}/${S.quizItems.length} answered · ${scored} scored`;
  quizListEl.innerHTML = '';

  for (const q of S.quizItems) {
    const isGoal  = q.type === 'goal_legibility';
    const isActive = q.id === S.activeId;
    const status   = getStatus(q.id);
    const scoreData = S.answers[q.id];
    const row = document.createElement('div');
    row.className = 'quiz-row' + (isActive ? ' active-row' : '');
    row.dataset.quizId = q.id;

    let scorePill = '';
    if (scoreData?.score != null) {
      const lbl = scoreData.score_label || 'incorrect';
      scorePill = `<span class="row-score-pill ${lbl}">${scoreIcon(lbl)} ${scoreData.score}/3</span>`;
    }

    row.innerHTML = `
      <span class="row-id">${esc(q.id)}</span>
      <span class="type-badge ${isGoal ? 'goal' : 'pred'}" style="font-size:9px;padding:2px 7px">${isGoal ? 'Goal' : 'Pred'}</span>
      <span class="row-ts">${esc(q.timestamp_label)}</span>
      <span class="row-q" title="${esc(q.question)}">${esc(q.question)}</span>
      ${scorePill}
      <span class="status-dot ${status}" title="${status}"></span>
      <button class="row-jump" data-id="${esc(q.id)}">Jump</button>`;
    quizListEl.appendChild(row);
  }

  quizListEl.querySelectorAll('.row-jump').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); jumpToQuiz(btn.dataset.id); });
  });
  quizListEl.querySelectorAll('.quiz-row').forEach(row => {
    row.addEventListener('click', () => jumpToQuiz(row.dataset.quizId));
  });
}

function getStatus(id) {
  if (id === S.activeId)         return 'active';
  const a = S.answers[id];
  if (!a)                        return 'unseen';
  if (a.score != null)           return 'revealed';
  if (a.userAnswer)              return 'answered';
  return 'skipped';
}

// ── Render: progress markers ────────────────────────────────────
function renderQuizMarkers() {
  progressWrap.querySelectorAll('.progress-marker').forEach(m => m.remove());
  if (!S.duration) return;
  for (const q of S.quizItems) {
    const pct = (q.pause_time_sec / S.duration * 100).toFixed(2);
    const m   = document.createElement('div');
    m.className = 'progress-marker';
    m.style.left = pct + '%';
    m.title = `${q.id} — ${q.timestamp_label}`;
    progressWrap.appendChild(m);
  }
}

// ── Util ────────────────────────────────────────────────────────
function fmt(s) {
  if (!s || isNaN(s)) return '0:00';
  return `${Math.floor(s/60)}:${Math.floor(s%60).toString().padStart(2,'0')}`;
}
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

init();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return HTML


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import threading
    import webbrowser

    parser = argparse.ArgumentParser(description="Agent recording quiz reviewer")
    parser.add_argument("--task", default="8", metavar="ID")
    parser.add_argument("--port", type=int, default=5051)
    args = parser.parse_args()

    url = f"http://localhost:{args.port}/?task={args.task}"
    print(f"\n  Quiz Reviewer → {url}\n  Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False)
