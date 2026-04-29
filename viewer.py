"""
viewer.py — Workflow recording viewer

Starts a local web server at http://localhost:5050
Browse recordings side-by-side: video + step timeline.

Usage:
    python viewer.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, abort, jsonify, send_file, send_from_directory

RECORDINGS_DIR = Path(__file__).parent / "recordings"

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────

@app.route("/api/recordings")
def list_recordings():
    if not RECORDINGS_DIR.exists():
        return jsonify([])
    items = []
    for d in sorted(RECORDINGS_DIR.iterdir()):
        if not d.is_dir():
            continue
        log_path = d / "log.json"
        if not log_path.exists():
            continue
        try:
            data = json.loads(log_path.read_text())
            events = data.get("events", [])
            start  = next((e for e in events if e["kind"] == "session_start"), {})
            end    = next((e for e in events if e["kind"] == "session_end"),   {})
            items.append({
                "id":         d.name,
                "task_name":  start.get("task_name", d.name),
                "started_at": start.get("started_at", ""),
                "duration":   end.get("duration_sec", ""),
                "has_video":  (d / "video.mp4").exists(),
            })
        except Exception:
            pass
    return jsonify(items)


@app.route("/api/recordings/<task_id>/log")
def get_log(task_id):
    log_path = RECORDINGS_DIR / task_id / "log.json"
    if not log_path.exists():
        abort(404)
    return jsonify(json.loads(log_path.read_text()))


@app.route("/recordings/<task_id>/video.mp4")
def serve_video(task_id):
    video_path = RECORDINGS_DIR / task_id / "video.mp4"
    if not video_path.exists():
        abort(404)
    return send_file(str(video_path), mimetype="video/mp4", conditional=True)


@app.route("/recordings/<task_id>/frames/<filename>")
def serve_frame(task_id, filename):
    frames_dir = RECORDINGS_DIR / task_id / "frames"
    return send_from_directory(str(frames_dir), filename)


# ─────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Workflow Viewer</title>
<style>
  :root {
    --bg: #0e0e12;
    --surface: #16161d;
    --border: #2a2a38;
    --accent: #5b8cff;
    --accent2: #43d9ad;
    --text: #d4d4e0;
    --muted: #6b6b82;
    --reasoning-bg: #1a1f2e;
    --action-bg: #12201a;
    --screenshot-bg: #1a1620;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px;
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  /* ── Header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-shrink: 0;
  }
  header h1 { font-size: 14px; color: var(--accent); letter-spacing: 0.05em; }
  #rec-select {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 10px;
    border-radius: 6px;
    font-family: inherit;
    font-size: 13px;
    cursor: pointer;
    min-width: 320px;
  }
  #rec-select:focus { outline: none; border-color: var(--accent); }
  #meta { color: var(--muted); font-size: 12px; }

  /* ── Main layout ── */
  main {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* ── Left: video / frame panel ── */
  #media-panel {
    width: 55%;
    flex-shrink: 0;
    background: #000;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--border);
    position: relative;
  }
  #video-wrap {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  #video-el {
    max-width: 100%;
    max-height: 100%;
    display: none;
  }
  #frame-el {
    max-width: 100%;
    max-height: 100%;
    display: none;
    object-fit: contain;
  }
  #media-placeholder {
    color: var(--muted);
    font-size: 12px;
  }

  /* timestamp overlay */
  #time-badge {
    position: absolute;
    bottom: 8px;
    right: 10px;
    background: rgba(0,0,0,0.65);
    color: var(--accent2);
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
    pointer-events: none;
  }

  /* progress bar under video */
  #progress-wrap {
    height: 3px;
    background: var(--border);
    cursor: pointer;
    flex-shrink: 0;
  }
  #progress-bar {
    height: 100%;
    background: var(--accent);
    width: 0%;
    transition: width 0.2s linear;
  }

  /* ── Right: timeline panel ── */
  #timeline-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #timeline-header {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    flex-shrink: 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  #filter-btns { display: flex; gap: 8px; }
  .filter-btn {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 3px 10px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 11px;
    font-family: inherit;
    transition: all 0.15s;
  }
  .filter-btn.active { border-color: var(--accent); color: var(--accent); }

  #timeline {
    flex: 1;
    overflow-y: auto;
    padding: 8px 0;
  }
  #timeline::-webkit-scrollbar { width: 5px; }
  #timeline::-webkit-scrollbar-track { background: transparent; }
  #timeline::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .step {
    padding: 10px 16px;
    border-left: 3px solid transparent;
    cursor: pointer;
    transition: background 0.1s;
    position: relative;
  }
  .step:hover { background: rgba(255,255,255,0.03); }
  .step.active {
    background: rgba(91,140,255,0.08);
    border-left-color: var(--accent);
  }

  .step-time {
    font-size: 10px;
    color: var(--muted);
    margin-bottom: 4px;
  }
  .step-kind {
    display: inline-block;
    font-size: 9px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding: 1px 6px;
    border-radius: 3px;
    margin-bottom: 6px;
    font-weight: 600;
  }
  .kind-reasoning  { background: #1e2d50; color: #7aadff; }
  .kind-action     { background: #1a2e20; color: #5dba78; }
  .kind-screenshot { background: #25182e; color: #c084fc; }
  .kind-summary    { background: #2e2518; color: #f59e0b; }

  .step-text {
    color: var(--text);
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .step-img {
    margin-top: 8px;
    max-width: 100%;
    border-radius: 6px;
    border: 1px solid var(--border);
    cursor: zoom-in;
    display: block;
  }

  /* action pill */
  .action-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--action-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12px;
    color: var(--accent2);
    font-family: 'SF Mono', monospace;
    flex-wrap: wrap;
  }
  .action-pill .param { color: var(--muted); font-size: 11px; }

  /* lightbox */
  #lightbox {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.88);
    z-index: 999;
    align-items: center;
    justify-content: center;
    cursor: zoom-out;
  }
  #lightbox.open { display: flex; }
  #lightbox img { max-width: 92vw; max-height: 92vh; border-radius: 6px; }

  /* empty state */
  #empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    flex-direction: column;
    gap: 8px;
  }
  #empty-state span { font-size: 32px; }
</style>
</head>
<body>

<header>
  <h1>⬡ workflow viewer</h1>
  <select id="rec-select"><option value="">— select a recording —</option></select>
  <span id="meta"></span>
</header>

<main>
  <div id="media-panel">
    <div id="video-wrap">
      <video id="video-el" controls></video>
      <img id="frame-el" alt="frame"/>
      <span id="media-placeholder">select a recording</span>
    </div>
    <div id="progress-wrap"><div id="progress-bar"></div></div>
    <div id="time-badge" style="display:none"></div>
  </div>

  <div id="timeline-panel">
    <div id="timeline-header">
      <span>STEPS</span>
      <div id="filter-btns">
        <button class="filter-btn active" data-kind="all">All</button>
        <button class="filter-btn" data-kind="reasoning">Reasoning</button>
        <button class="filter-btn" data-kind="action">Actions</button>
        <button class="filter-btn" data-kind="screenshot">Screenshots</button>
      </div>
    </div>
    <div id="timeline">
      <div id="empty-state"><span>🎬</span><p>No recording loaded</p></div>
    </div>
  </div>
</main>

<div id="lightbox"><img id="lightbox-img" src="" alt=""/></div>

<script>
const recSelect   = document.getElementById('rec-select');
const metaEl      = document.getElementById('meta');
const videoEl     = document.getElementById('video-el');
const frameEl     = document.getElementById('frame-el');
const placeholder = document.getElementById('media-placeholder');
const progressBar = document.getElementById('progress-bar');
const timeBadge   = document.getElementById('time-badge');
const timeline    = document.getElementById('timeline');
const lightbox    = document.getElementById('lightbox');
const lightboxImg = document.getElementById('lightbox-img');

let events       = [];
let steps        = [];      // rendered step elements with .ts metadata
let startTs      = 0;
let duration     = 0;
let hasVideo     = false;
let activeFilter = 'all';

// ── Load recording list ──────────────────────────────────────
async function loadList() {
  const res  = await fetch('/api/recordings');
  const list = await res.json();
  list.forEach(r => {
    const opt = document.createElement('option');
    opt.value       = r.id;
    const dur = r.duration ? ` · ${Math.round(r.duration)}s` : '';
    opt.textContent = `Task ${r.id}: ${r.task_name}${dur}`;
    recSelect.appendChild(opt);
  });
}

// ── Load a recording ─────────────────────────────────────────
recSelect.addEventListener('change', () => {
  const id = recSelect.value;
  if (id) loadRecording(id);
});

async function loadRecording(id) {
  const res  = await fetch(`/api/recordings/${id}/log`);
  const data = await res.json();
  events = data.events || [];

  const startEv = events.find(e => e.kind === 'session_start') || {};
  const endEv   = events.find(e => e.kind === 'session_end')   || {};
  startTs  = startEv.ts || events[0]?.ts || 0;
  duration = endEv.duration_sec || 0;

  metaEl.textContent = startEv.started_at
    ? `${startEv.started_at}  ·  ${Math.round(duration)}s`
    : '';

  // Try video first, fall back to frame scrubbing
  hasVideo = false;
  videoEl.style.display = 'none';
  frameEl.style.display = 'none';
  placeholder.style.display = 'none';

  const videoUrl = `/recordings/${id}/video.mp4`;
  try {
    const probe = await fetch(videoUrl, { method: 'HEAD' });
    if (probe.ok) {
      hasVideo = true;
      videoEl.src = videoUrl;
      videoEl.style.display = 'block';
      timeBadge.style.display = 'block';
    }
  } catch(_) {}

  if (!hasVideo) {
    frameEl.style.display = 'block';
    timeBadge.style.display = 'block';
    showFrameAt(id, 0);
  }

  renderTimeline(id);
}

// ── Timeline rendering ───────────────────────────────────────
function renderTimeline(id) {
  timeline.innerHTML = '';
  steps = [];

  const eventsToShow = events.filter(e => {
    if (e.kind === 'session_start' || e.kind === 'session_end') return false;
    if (activeFilter === 'all') return true;
    if (activeFilter === 'reasoning')  return e.kind === 'reasoning';
    if (activeFilter === 'action')     return e.kind === 'action';
    if (activeFilter === 'screenshot') return e.kind === 'agent_screenshot';
    return true;
  });

  if (eventsToShow.length === 0) {
    timeline.innerHTML = '<div id="empty-state"><span>🔍</span><p>No events match filter</p></div>';
    return;
  }

  eventsToShow.forEach((ev, i) => {
    const relSec = ev.ts - startTs;
    const div = document.createElement('div');
    div.className = 'step';
    div.dataset.ts = ev.ts;
    div.dataset.rel = relSec;

    let kindLabel = ev.kind;
    let kindClass = 'kind-reasoning';
    let body = '';

    if (ev.kind === 'reasoning') {
      kindLabel = 'reasoning';
      kindClass = 'kind-reasoning';
      body = `<div class="step-text">${escHtml(ev.text)}</div>`;
    } else if (ev.kind === 'action') {
      kindLabel = 'action';
      kindClass = 'kind-action';
      const params = ev.params || {};
      const coord  = params.coordinate ? `(${params.coordinate[0]}, ${params.coordinate[1]})` : '';
      const text   = params.text ? `"${escHtml(String(params.text).slice(0, 60))}"` : '';
      const extra  = coord || text;
      body = `<div class="action-pill">
        <strong>${escHtml(ev.action)}</strong>
        ${extra ? `<span class="param">${extra}</span>` : ''}
      </div>`;
    } else if (ev.kind === 'agent_screenshot') {
      kindLabel = 'screenshot';
      kindClass = 'kind-screenshot';
      body = `<img class="step-img" src="/recordings/${id}/frames/${ev.frame}"
                   loading="lazy" data-src="${ev.frame}" alt="screenshot"/>`;
    } else if (ev.kind === 'session_end') {
      kindLabel = 'summary';
      kindClass = 'kind-summary';
      body = `<div class="step-text">${escHtml(ev.summary || '')}</div>`;
    }

    div.innerHTML = `
      <div class="step-time">+${relSec.toFixed(1)}s</div>
      <span class="step-kind ${kindClass}">${kindLabel}</span>
      ${body}
    `;

    div.addEventListener('click', () => seekTo(ev.ts, id, div));

    // lightbox for screenshot images
    div.querySelectorAll('.step-img').forEach(img => {
      img.addEventListener('click', e => {
        e.stopPropagation();
        lightboxImg.src = img.src;
        lightbox.classList.add('open');
      });
    });

    timeline.appendChild(div);
    steps.push({ el: div, ts: ev.ts });
  });
}

// ── Seek ─────────────────────────────────────────────────────
function seekTo(ts, id, el) {
  const relSec = ts - startTs;

  // highlight step
  steps.forEach(s => s.el.classList.remove('active'));
  if (el) el.classList.add('active');

  if (hasVideo) {
    videoEl.currentTime = relSec;
    videoEl.play();
  } else {
    showFrameAt(id, relSec);
  }
  updateProgress(relSec);
}

// ── Frame scrubbing (when no video) ──────────────────────────
const FRAME_INTERVAL = 0.5;

function showFrameAt(id, relSec) {
  // find the nearest agent_screenshot frame
  const screenshots = events.filter(e => e.kind === 'agent_screenshot');
  if (screenshots.length === 0) return;

  let best = screenshots[0];
  for (const ev of screenshots) {
    if (Math.abs(ev.ts - startTs - relSec) < Math.abs(best.ts - startTs - relSec)) {
      best = ev;
    }
  }
  frameEl.src = `/recordings/${id}/frames/${best.frame}`;
}

// ── Video → progress + active step sync ──────────────────────
videoEl.addEventListener('timeupdate', () => {
  const t = videoEl.currentTime;
  updateProgress(t);
  timeBadge.textContent = `${t.toFixed(1)}s`;

  // highlight the most recent step
  const absTs = startTs + t;
  let active = null;
  for (const s of steps) {
    if (s.ts <= absTs) active = s;
  }
  steps.forEach(s => s.el.classList.remove('active'));
  if (active) {
    active.el.classList.add('active');
    active.el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
});

function updateProgress(relSec) {
  if (!duration) return;
  progressBar.style.width = Math.min(relSec / duration * 100, 100) + '%';
  timeBadge.textContent = `${relSec.toFixed(1)}s`;
}

// ── Progress bar click to seek ────────────────────────────────
document.getElementById('progress-wrap').addEventListener('click', e => {
  if (!duration) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const pct  = (e.clientX - rect.left) / rect.width;
  const relSec = pct * duration;
  if (hasVideo) videoEl.currentTime = relSec;
  else showFrameAt(recSelect.value, relSec);
  updateProgress(relSec);
});

// ── Filter buttons ────────────────────────────────────────────
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeFilter = btn.dataset.kind;
    const id = recSelect.value;
    if (id) renderTimeline(id);
  });
});

// ── Lightbox ─────────────────────────────────────────────────
lightbox.addEventListener('click', () => lightbox.classList.remove('open'));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') lightbox.classList.remove('open');
});

// ── Util ─────────────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Boot ─────────────────────────────────────────────────────
loadList();
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
    import webbrowser, threading
    port = 5050
    url  = f"http://localhost:{port}"
    print(f"\n  Workflow Viewer → {url}\n  Press Ctrl+C to stop.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
