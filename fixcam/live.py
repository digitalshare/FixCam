"""Live FaceTime call support.

On lines with caller capture enabled, the voice agent grabs frames from the caller's FaceTime camera
and posts them to /live/frames. As a fallback, the caller can send photos/videos from a phone web page
(/phone) while staying on the call. The FaceTime voice agent:
  - POST /live/start          when a call begins (resets the shared live session)
  - GET  /live/events?after=N polls for "processing" / "result" / "error" events
  - POST /live/show           switches what the caller sees on FaceTime (a step's marked-up frame)
  - POST /live/ask            asks Claude a follow-up (sync), or starts a voice-only diagnosis (async)
  - GET  /live/screen         raw RGBA bytes of the current screen, for the agent's video track
  - POST /live/frames         raw RGBA frames from the caller's camera -> diagnosis (async)
One Chert line answers one call at a time, so a single live session is enough.
"""

from __future__ import annotations

import io
import shutil
import textwrap
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel

from . import agent, annotate, config, runs

router = APIRouter()

SCREEN_W, SCREEN_H = 960, 540
BG, FG, MUTED, ACCENT = (18, 18, 20), (245, 245, 247), (152, 152, 157), annotate.ACCENT


class _Live:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cleared_at = 0.0
        self.reset_camera("none")
        self.reset("idle")

    def reset_camera(self, status: str) -> None:
        """Caller-camera preview for the dashboard: none (no call) | waiting | connected | live | muted."""
        self.camera_frame: Image.Image | None = None
        self.camera_at = 0.0
        self.camera_status = status

    def reset(self, call_id: str) -> None:
        self.call_id = call_id
        self.session_id = f"call-{call_id}"
        self.events: list[dict] = []
        self.steps: list[dict] = []
        self.busy = False
        self.last_result_at = 0.0
        self.screen_version = 0
        self.screen = render_card("FixCam", ["Snap a photo of what's broken", f"{config.PUBLIC_BASE_URL}/phone"])
        agent.reset_session(self.session_id)

    def push(self, kind: str, **data) -> None:
        with self.lock:
            self.events.append({"seq": len(self.events) + 1, "type": kind, **data})

    def set_screen(self, img: Image.Image) -> None:
        with self.lock:
            self.screen = img
            self.screen_version += 1


# ---------------------------------------------------------------- rendering

def _fit(img: Image.Image) -> Image.Image:
    """Letterbox any image into the screen size."""
    canvas = Image.new("RGB", (SCREEN_W, SCREEN_H), BG)
    img = ImageOps.contain(img.convert("RGB"), (SCREEN_W, SCREEN_H))
    canvas.paste(img, ((SCREEN_W - img.width) // 2, (SCREEN_H - img.height) // 2))
    return canvas


def render_card(title: str, lines: list[str], badge: str | None = None) -> Image.Image:
    im = Image.new("RGB", (SCREEN_W, SCREEN_H), BG)
    d = ImageDraw.Draw(im)
    title_font, body_font = annotate._font(56), annotate._font(30)
    x, y = 60, 70
    if badge:
        d.ellipse((x, y, x + 70, y + 70), fill=ACCENT)
        d.text((x + 35, y + 35), badge, fill=FG, font=title_font, anchor="mm")
        x += 95
    d.text((x, y + 35), title, fill=FG, font=title_font, anchor="lm")
    y += 130
    for line in lines:
        for wrapped in textwrap.wrap(line, 48) or [""]:
            d.text((60, y), wrapped, fill=FG if y < 330 else MUTED, font=body_font)
            y += 44
        y += 10
    d.text((SCREEN_W - 40, SCREEN_H - 30), "FixCam", fill=MUTED, font=annotate._font(22), anchor="rs")
    return im


def _step_screen(step: dict) -> Image.Image:
    if step.get("image"):
        return _fit(Image.open(step["image"]))
    return render_card(f"Step {step['n']}", [step["instruction"]], badge=str(step["n"]))


LIVE = _Live()


# ---------------------------------------------------------------- pipeline runs

def _run(note: str, paths: list[Path], session_id: str, source: str) -> None:
    try:
        reply = agent.handle_turn(session_id, note, paths, want_voice=False, allow_text_only=True,
                                 source=source)
        if LIVE.session_id != session_id:
            return  # a new call started while we were working
        session = agent.get_session(LIVE.session_id)
        plan, diagnosis = session.plan, session.diagnosis
        if plan is None:
            LIVE.push("result", summary=reply.text, steps=[], safety=None)
            return
        images = iter(reply.images)
        steps = []
        for n, s in enumerate(plan.steps, 1):
            has_frame = 0 <= s.frame_index < len(session.frames)
            steps.append({"n": n, "instruction": s.instruction, "label": s.short_label,
                          "image": str(next(images, "")) if has_frame else ""})
        LIVE.steps = steps
        LIVE.set_screen(_step_screen(steps[0]) if steps else render_card("FixCam", [reply.text[:200]]))
        LIVE.push("result",
                  summary=f"{diagnosis.object_type}: {diagnosis.problem_summary}" if diagnosis else reply.text,
                  steps=[{k: v for k, v in s.items() if k != "image"} for s in steps],
                  safety=plan.safety_warning, needs_professional=plan.needs_professional,
                  parts=[p.name for p in plan.parts], from_photo=bool(session.frames))
        LIVE.last_result_at = time.time()
    except Exception as e:
        traceback.print_exc()
        LIVE.push("error", message=str(e)[:200])
    finally:
        LIVE.busy = False


def _start_run(note: str, paths: list[Path], source: str) -> bool:
    with LIVE.lock:
        if LIVE.busy:
            return False
        LIVE.busy = True
    threading.Thread(target=_run, args=(note, paths, LIVE.session_id, source), daemon=True).start()
    return True


# ---------------------------------------------------------------- routes

class StartIn(BaseModel):
    callId: str


CARRY_OVER_SECONDS = 30 * 60


@router.post("/live/start")
def live_start(body: StartIn):
    """Carry the latest photo/diagnosis into the new call if it is recent (sent before dialling,
    or from a call that dropped); otherwise start fresh."""
    LIVE.reset_camera("waiting")
    recent = LIVE.busy or (LIVE.call_id == "idle" and LIVE.events) or \
        time.time() - LIVE.last_result_at < CARRY_OVER_SECONDS
    if recent:
        with LIVE.lock:
            LIVE.call_id = body.callId
            results = [e for e in LIVE.events if e["type"] == "result"][-1:]
            LIVE.events = [{**e, "seq": i} for i, e in enumerate(results, 1)]
        if LIVE.busy:
            LIVE.push("processing", source="phone", note="")
    else:
        LIVE.reset(body.callId)
    return {"ok": True, "carriedOver": bool(recent), "phoneUrl": f"{config.PUBLIC_BASE_URL}/phone"}


@router.post("/live/camera")
async def live_camera(request: Request, width: int = 0, height: int = 0, rotation: int = 0, status: str = "live"):
    """Preview of the caller's FaceTime camera for the dashboard (one raw RGBA frame), or a status ping
    (status=connected|muted|none with an empty body)."""
    raw = await request.body()
    if raw and width and height and len(raw) == width * height * 4:
        img = Image.frombytes("RGBA", (width, height), raw).convert("RGB")
        if rotation:
            img = img.rotate(-90 * rotation, expand=True)
        img.thumbnail((640, 640))
        with LIVE.lock:
            LIVE.camera_frame, LIVE.camera_at = img, time.time()
    LIVE.camera_status = status
    return {"ok": True}


@router.get("/live/camera.jpg")
def live_camera_jpg():
    with LIVE.lock:
        img = LIVE.camera_frame
    if img is None:
        return Response(status_code=404)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/live/end")
def live_end():
    """The FaceTime call ended. Keep the diagnosis so a call-back can pick it up."""
    LIVE.call_id = "idle"
    LIVE.camera_status = "none"
    return {"ok": True}


@router.post("/live/reset")
def live_reset():
    """Forget the current diagnosis (e.g. between demo runs)."""
    LIVE.reset("idle")
    LIVE.cleared_at = time.time()  # the dashboard's Live view starts empty again
    return {"ok": True}


@router.get("/live/events")
def live_events(after: int = 0):
    return {"events": [e for e in LIVE.events if e["seq"] > after], "busy": LIVE.busy,
            "screenVersion": LIVE.screen_version}


@router.get("/live/screen")
def live_screen():
    with LIVE.lock:
        data, version = LIVE.screen.convert("RGBA").tobytes(), LIVE.screen_version
    return Response(data, media_type="application/octet-stream",
                    headers={"X-Width": str(SCREEN_W), "X-Height": str(SCREEN_H), "X-Version": str(version)})


class ShowIn(BaseModel):
    step: int


@router.post("/live/show")
def live_show(body: ShowIn):
    step = next((s for s in LIVE.steps if s["n"] == body.step), None)
    if step is None:
        return {"ok": False, "error": f"no step {body.step}; there are {len(LIVE.steps)} steps"}
    LIVE.set_screen(_step_screen(step))
    runs.mark_step(LIVE.session_id, body.step)
    return {"ok": True, "instruction": step["instruction"]}


class AskIn(BaseModel):
    question: str


@router.post("/live/ask")
def live_ask(body: AskIn):
    session = agent.get_session(LIVE.session_id)
    if session.plan is None:
        # No diagnosis yet: research the spoken description in the background.
        started = _start_run(body.question, [], "voice")
        LIVE.push("processing", source="voice") if started else None
        return {"async": True, "started": started}
    session.history.append({"role": "user", "content": body.question})
    answer = agent.follow_up(session, body.question)
    session.history.append({"role": "assistant", "content": answer})
    return {"async": False, "answer": answer}


@router.post("/live/frames")
async def live_frames(request: Request, note: str = "", count: int = 1, width: int = 0, height: int = 0,
                      rotation: int = 0):
    """Raw RGBA frames from the caller's FaceTime camera, concatenated (sent by the voice agent)."""
    raw = await request.body()
    size = width * height * 4
    if not size or len(raw) != size * count:
        return {"ok": False, "error": f"expected {count} frames of {width}x{height} RGBA"}
    out = config.DATA_DIR / "live" / LIVE.call_id / f"cam_{int(time.time() * 1000)}"
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        img = Image.frombytes("RGBA", (width, height), raw[i * size:(i + 1) * size]).convert("RGB")
        if rotation:  # VideoRotation: 1=90, 2=180, 3=270 degrees clockwise
            img = img.rotate(-90 * rotation, expand=True)
        dst = out / f"cam_{i}.jpg"
        img.save(dst, "JPEG", quality=90)
        paths.append(dst)
    if not _start_run(note.strip(), paths, "camera"):
        return {"ok": False, "error": "busy"}
    LIVE.set_screen(_fit(Image.open(paths[-1])))
    LIVE.push("processing", source="camera", note=note.strip(), at=time.time())
    return {"ok": True, "frames": count}


@router.get("/phone")
def phone_page():
    return FileResponse(Path(__file__).parent / "static" / "phone.html")


@router.post("/live/upload")
def live_upload(note: str = Form(""), files: list[UploadFile] = File(default=[])):
    inbox = config.DATA_DIR / "live" / LIVE.call_id
    inbox.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        if f.filename:
            dst = inbox / f"{uuid.uuid4().hex[:8]}_{Path(f.filename).name}"
            with dst.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            paths.append(dst)
    if not paths:
        return {"ok": False, "error": "no file"}
    if not _start_run(note.strip(), paths, "phone"):
        return {"ok": False, "error": "Still working on the last one — hang on a few seconds."}
    try:  # show the caller what we received while we work
        preview = Image.open(paths[0])
    except Exception:
        preview = None
    LIVE.set_screen(render_card("Looking…", ["Got it! Diagnosing and searching for the fix."])
                    if preview is None else _fit(ImageOps.exif_transpose(preview)))
    LIVE.push("processing", source="phone", note=note.strip(), at=time.time())
    return {"ok": True}
