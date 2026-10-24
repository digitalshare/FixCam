"""FastAPI app: Chert webhook, MatrAIx chat-eval endpoint, and a browser demo page."""

from __future__ import annotations

import io
import shutil
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, channels, chert, config, live, runs

app = FastAPI(title="FixCam")
app.mount("/media", StaticFiles(directory=config.DATA_DIR), name="media")
app.include_router(live.router)


def _media_url(path: Path) -> str:
    return f"{config.PUBLIC_BASE_URL}/media/{path.resolve().relative_to(config.DATA_DIR.resolve())}"


@app.get("/health")
@app.get("/v1/health")
def health():
    return {"ok": True}


# ------------------------------------------------------------- Chert webhook

@app.post("/webhooks/chert")
async def chert_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    sig = request.headers.get("x-webhook-signature") or request.headers.get("x-chert-signature")
    if not chert.verify_webhook(raw, sig):
        raise HTTPException(401, "bad signature")
    background.add_task(channels.handle_event, await request.json())
    return {"ok": True}  # ack fast; Chert retries on 5xx


# ------------------------------------------------- MatrAIx chat-task contract
# POST /v1/messages {sessionId, message, caseId?} -> {sessionId, reply}
# caseId attaches samples/<caseId>/* as if the user had texted that video.

class ChatIn(BaseModel):
    message: str = ""
    sessionId: str | None = None
    caseId: str | None = None


def _describe_images(reply: agent.Reply, session: agent.Session) -> str:
    if not reply.images or not session.plan:
        return ""
    lines = [f"[Photo {i}: your video frame marked up — \"{s.short_label}\"]"
             for i, s in enumerate((s for s in session.plan.steps if 0 <= s.frame_index < len(session.frames)), 1)]
    return "\n" + "\n".join(lines[: len(reply.images)])


@app.post("/v1/messages")
def chat_message(body: ChatIn):
    session_id = body.sessionId or str(uuid.uuid4())
    session = agent.get_session(session_id)
    paths: list[Path] = []
    if body.caseId and not session.frames:
        case_dir = (config.SAMPLES_DIR / body.caseId).resolve()
        if config.SAMPLES_DIR.resolve() not in case_dir.parents or not case_dir.is_dir():
            raise HTTPException(404, f"unknown caseId {body.caseId}")
        paths = sorted(p for p in case_dir.iterdir() if p.is_file() and not p.name.startswith((".", "case")))
    reply = agent.handle_turn(session_id, body.message.strip(), paths, want_voice=False,
                              source="eval" if body.caseId else "chat")
    text = reply.text + _describe_images(reply, session)
    text += "".join(f"\n🔗 {l.get('title') or ''} {l['url']}".rstrip() for l in reply.links)
    return {"sessionId": session_id, "reply": text,
            "images": [_media_url(p) for p in reply.images], "timings": reply.timings}


@app.get("/v1/conversation")
def conversation(sessionId: str):
    session = agent.get_session(sessionId)
    return {"messages": [{"role": "customer" if m["role"] == "user" else "support", "content": m["content"]}
                         for m in session.history]}


# ---------------------------------------------------------- browser demo

@app.get("/")
def demo_page():
    return FileResponse(Path(__file__).parent / "static" / "demo.html")


@app.post("/demo/turn")
def demo_turn(sessionId: str = Form(...), message: str = Form(""), files: list[UploadFile] = File(default=[])):
    session = agent.get_session(sessionId)
    inbox = session.dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        if not f.filename:
            continue
        dst = inbox / f"{uuid.uuid4().hex[:8]}_{Path(f.filename).name}"
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        paths.append(dst)
    reply = agent.handle_turn(sessionId, message.strip(), paths, want_voice=True, source="web")
    return JSONResponse({
        "reply": reply.text,
        "images": [_media_url(p) for p in reply.images],
        "links": reply.links,
        "voice": _media_url(reply.voice_wav) if reply.voice_wav else None,
        "timings": reply.timings,
    })


@app.post("/demo/reset")
def demo_reset(sessionId: str = Form(...)):
    agent.reset_session(sessionId)
    return {"ok": True}


# ---------------------------------------------------------- dashboard: live workflow, history, reports

@app.get("/dashboard")
def dashboard_page():
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


@app.get("/api/runs")
def api_runs(limit: int = 100):
    return {"runs": runs.listing(limit)}


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/live")
def api_live():
    newest = next(iter(runs.listing(1)), None)
    if newest and newest["started_at"] < live.LIVE.cleared_at:
        newest = None  # cleared for a fresh demo; History still has it
    return {"callId": live.LIVE.call_id, "inCall": live.LIVE.call_id != "idle", "busy": live.LIVE.busy,
            "screenVersion": live.LIVE.screen_version, "latestRunId": newest["id"] if newest else None,
            "camera": {"status": live.LIVE.camera_status,
                       "age": round(time.time() - live.LIVE.camera_at, 1) if live.LIVE.camera_at else None},
            "phoneUrl": f"{config.PUBLIC_BASE_URL}/phone"}


@app.get("/live/screen.png")
def live_screen_png():
    """What the caller currently sees on FaceTime (the agent's video)."""
    buf = io.BytesIO()
    with live.LIVE.lock:
        live.LIVE.screen.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})
