"""Run records: one per diagnosis, with per-stage progress, persisted for history and fix reports.

Stages, in order: ingest -> diagnose -> research -> plan -> render -> guide.
Each stage is {status: pending|running|done|error|skipped, started, ended, detail}.
Records live in data/runs/<id>.json and are rewritten on every update (small files).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import config

RUNS_DIR = config.DATA_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
STAGES = ["ingest", "diagnose", "research", "plan", "render", "guide"]
LIVE_SOURCES = {"phone", "camera", "voice"}  # runs started during a FaceTime call

_lock = threading.Lock()
_active: dict[str, dict] = {}  # session_id -> newest run for that session


def media_url(path: str | Path) -> str:
    """URL under the server's /media mount for a file inside DATA_DIR ('' if outside)."""
    try:
        return "/media/" + str(Path(path).resolve().relative_to(config.DATA_DIR.resolve()))
    except ValueError:
        return ""


def _save(run: dict) -> None:
    (RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, indent=1, default=str))


def start(session_id: str, source: str, note: str) -> dict:
    run = {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        "session_id": session_id, "source": source, "note": note,
        "status": "running", "started_at": time.time(), "ended_at": None,
        "stages": {s: {"status": "pending", "started": None, "ended": None, "detail": {}} for s in STAGES},
        "frames": [], "transcripts": [], "diagnosis": None, "sources": [], "plan": None,
        "images": [], "current_step": None, "error": None,
    }
    with _lock:
        _active[session_id] = run
        _save(run)
    return run


def stage(run: dict | None, name: str, status: str, **detail) -> None:
    if run is None:
        return
    with _lock:
        st = run["stages"][name]
        now = time.time()
        if status == "running":
            st["started"] = now
        elif st["started"] is None:
            st["started"] = now
        if status in ("done", "error", "skipped"):
            st["ended"] = now
        st["status"] = status
        st["detail"].update(detail)
        _save(run)


def update(run: dict | None, **fields) -> None:
    if run is None:
        return
    with _lock:
        run.update(fields)
        _save(run)


def finish(run: dict | None, status: str = "done", error: str | None = None) -> None:
    if run is None:
        return
    with _lock:
        if status == "skipped":  # turned out to be a greeting or follow-up, not a diagnosis
            (RUNS_DIR / f"{run['id']}.json").unlink(missing_ok=True)
            return
        run["status"], run["error"], run["ended_at"] = status, error, time.time()
        for st in run["stages"].values():
            if st["status"] == "running":
                st["status"], st["ended"] = ("error" if status == "error" else "done"), time.time()
        if run["source"] not in LIVE_SOURCES or status != "done":
            run["stages"]["guide"]["status"] = "skipped"  # only FaceTime runs are talked through
        _save(run)


def mark_step(session_id: str, step: int) -> None:
    """Called when the voice agent shows a step on the caller's screen (guide stage)."""
    with _lock:
        run = _active.get(session_id)
        if run is None or run["plan"] is None:
            return
        run["current_step"] = step
        g = run["stages"]["guide"]
        now = time.time()
        if g["started"] is None:
            g["started"] = now
        g["status"] = "done" if step >= len(run["plan"]["steps"]) else "running"
        if g["status"] == "done":
            g["ended"] = now
        g["detail"]["steps_shown"] = sorted(set(g["detail"].get("steps_shown", [])) | {step})
        _save(run)


def get(run_id: str) -> dict | None:
    path = RUNS_DIR / f"{Path(run_id).name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def listing(limit: int = 100) -> list[dict]:
    out = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            r = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue  # being written
        d = r.get("diagnosis") or {}
        out.append({
            "id": r["id"], "source": r["source"], "status": r["status"], "started_at": r["started_at"],
            "duration": (r["ended_at"] - r["started_at"]) if r.get("ended_at") else None,
            "object_type": d.get("object_type"), "visible_error": d.get("visible_error"),
            "problem": d.get("problem_summary"), "steps": len((r.get("plan") or {}).get("steps", [])),
            "thumb": (r.get("images") or r.get("frames") or [""])[0],
            "needs_professional": (r.get("plan") or {}).get("needs_professional"),
        })
    return out
