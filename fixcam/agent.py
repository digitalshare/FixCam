"""FixCam agent: video/photo/voice in -> diagnosis -> web research -> annotated fix plan out.

Pipeline (deterministic, so the demo is fast and debuggable):
  1. ingest   - sample key frames from video, transcribe voice/video audio
  2. diagnose - Claude looks at frames + transcript, identifies device, error, search queries
  3. research - Keenable searches the web and extracts repair steps from the top pages
  4. plan     - Claude writes step-by-step fix grounded in sources, pointing at regions of the frames
  5. render   - PIL draws numbered callouts onto the user's own frames; optional TTS voice note
Follow-up texts ("done, what next?") reuse the stored diagnosis and plan.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from PIL import Image
from pydantic import BaseModel, Field

from . import annotate, config, keenable, media, runs, speech

client = anthropic.Anthropic()

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You are FixCam, a friendly expert repair technician that people text over iMessage.
Users send short phone videos or photos of something broken or confusing (appliances, bikes, cars, \
electronics, furniture, robot vacuums, plumbing) plus a short text or voice note.
Be practical and concrete. Speak to a non-expert. Never invent model numbers or part numbers you \
cannot see or that the sources do not state. If something is dangerous (mains electricity, gas, \
refrigerant, airbags, high-voltage capacitors), say so plainly and recommend a professional."""


class Diagnosis(BaseModel):
    object_type: str = Field(description="What the thing is, e.g. 'front-load washing machine'")
    brand: str | None = Field(description="Brand if visible or stated, else null")
    model_number: str | None = Field(description="Model number only if legible or stated, else null")
    visible_error: str | None = Field(description="Error code / light pattern / symptom shown, else null")
    problem_summary: str = Field(description="One or two sentences on what is going wrong")
    likely_causes: list[str] = Field(description="Most likely causes, most probable first")
    confidence: str = Field(description="low | medium | high")
    search_queries: list[str] = Field(description="2-4 specific web search queries to find the fix")
    clarifying_question: str | None = Field(
        description="Ask ONLY if the media is unusable or the problem cannot be identified at all; else null")


class Step(BaseModel):
    instruction: str = Field(description="One clear action, imperative mood, <= 30 words")
    short_label: str = Field(description="<= 32 characters caption to draw on the image")
    frame_index: int = Field(description="Index of the frame that best shows where to act, or -1 if none")
    box: list[int] = Field(description="[x0,y0,x1,y1] in PIXELS of that frame (see its size); [] if none")


class Part(BaseModel):
    name: str
    why: str
    url: str | None = Field(description="Purchase/info URL taken from the sources, else null")


class FixPlan(BaseModel):
    reply_text: str = Field(description="iMessage reply: diagnosis in plain words + numbered steps, <= 700 chars")
    steps: list[Step] = Field(description="3-6 steps, in order")
    parts: list[Part] = Field(description="Replacement parts or tools needed; [] if none")
    source_urls: list[str] = Field(description="1-3 most useful source URLs actually used")
    voice_script: str = Field(description="Warm spoken summary of the fix, <= 60 words")
    safety_warning: str | None
    needs_professional: bool


@dataclass
class Reply:
    text: str
    images: list[Path] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    voice_wav: Path | None = None
    timings: dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    dir: Path
    frames: list[Path] = field(default_factory=list)
    frame_times: list[float] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    diagnosis: Diagnosis | None = None
    plan: FixPlan | None = None
    history: list[dict] = field(default_factory=list)  # [{"role": "user"|"assistant", "content": str}]
    lock: threading.Lock = field(default_factory=threading.Lock)


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


def get_session(session_id: str) -> Session:
    with _sessions_lock:
        if session_id not in _sessions:
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
            _sessions[session_id] = Session(id=session_id, dir=config.DATA_DIR / "sessions" / safe)
        return _sessions[session_id]


def reset_session(session_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(session_id, None)


# ---------------------------------------------------------------- ingest

def ingest(session: Session, media_paths: list[Path]) -> int:
    """Turn uploaded files into frames + transcripts. Returns number of new frames."""
    new_frames: list[tuple[Path, float]] = []
    batch = f"b{int(time.time() * 1000)}"
    for i, path in enumerate(media_paths):
        kind = media.classify(path)
        out_dir = session.dir / batch / f"m{i}"
        if kind == "image":
            new_frames.append((media.image_to_frame(path, out_dir, "photo"), 0.0))
        elif kind == "video":
            new_frames += media.sample_frames(path, out_dir, config.MAX_FRAMES)
        if kind in ("video", "audio"):
            wav = media.extract_audio_wav(path, out_dir / "audio.wav")
            if wav and (text := speech.transcribe(wav)):
                session.transcripts.append(text)
    if new_frames:  # a fresh video replaces the old visual context
        session.frames = [p for p, _ in new_frames]
        session.frame_times = [t for _, t in new_frames]
    return len(new_frames)


def _frame_blocks(session: Session) -> list[dict]:
    blocks: list[dict] = []
    for i, (p, t) in enumerate(zip(session.frames, session.frame_times)):
        with Image.open(p) as im:
            w, h = im.size
        blocks.append({"type": "text", "text": f"Frame {i} (t={t:.1f}s, {w}x{h} px):"})
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(p.read_bytes()).decode()}})
    return blocks


def _user_context(session: Session) -> str:
    said = [m["content"] for m in session.history if m["role"] == "user"]
    parts = []
    if said:
        parts.append("User's messages:\n" + "\n".join(f"- {s}" for s in said))
    if session.transcripts:
        parts.append("Transcribed voice/video audio:\n" + "\n".join(f"- {t}" for t in session.transcripts))
    return "\n\n".join(parts) or "The user sent media without any text."


def _parse(output_format: type[BaseModel], content: list[dict], effort: str) -> BaseModel | None:
    resp = client.beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=output_format,
        output_config={"effort": effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


# ---------------------------------------------------------------- pipeline

def diagnose(session: Session) -> Diagnosis | None:
    content = _frame_blocks(session) + [{"type": "text", "text": (
        f"{_user_context(session)}\n\nIdentify the object and what is wrong. Read any visible labels, "
        "displays, error codes, or blinking lights. Then propose web search queries that would find "
        "the official fix (include brand/model/error code when known).")}]
    return _parse(Diagnosis, content, config.CLAUDE_EFFORT)


def plan_fix(session: Session, diagnosis: Diagnosis, sources: list[dict]) -> FixPlan | None:
    src_text = "\n\n".join(
        f"[{i}] {s['title']} — {s['url']}\n{(s.get('content') or s['snippet'])[:3500]}"
        for i, s in enumerate(sources)
    ) or "No web sources were found; rely on general expertise and say so."
    content = _frame_blocks(session) + [{"type": "text", "text": (
        f"{_user_context(session)}\n\nDiagnosis:\n{diagnosis.model_dump_json(indent=1)}\n\n"
        f"Web research (Keenable):\n{src_text}\n\n"
        "Write the fix plan. Ground steps in the sources where possible. For each step, pick the frame "
        "that best shows WHERE to act and give a tight bounding box around that spot as pixel "
        "coordinates x0,y0,x1,y1 within that frame's stated width and height. Keep reply_text short "
        "enough for a phone screen.")}]
    return _parse(FixPlan, content, config.CLAUDE_EFFORT)


def render(session: Session, plan: FixPlan) -> list[Path]:
    out = session.dir / f"annotated_{int(time.time())}"
    images = []
    for n, step in enumerate(plan.steps, 1):
        if 0 <= step.frame_index < len(session.frames):
            dst = out / f"step_{n}.jpg"
            images.append(annotate.draw_step(session.frames[step.frame_index], dst, n, step.short_label, step.box))
    return images[:4]


def follow_up(session: Session, text: str) -> str:
    context = (f"Diagnosis:\n{session.diagnosis.model_dump_json(indent=1) if session.diagnosis else 'none'}\n\n"
               f"Fix plan you already sent:\n{session.plan.model_dump_json(indent=1) if session.plan else 'none'}")
    messages = [{"role": "user", "content": f"[Context]\n{context}"},
                {"role": "assistant", "content": "Understood."}]
    messages += session.history[-12:]
    resp = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4000,
        system=SYSTEM + "\nYou are mid-conversation over iMessage. Reply in <= 4 short sentences.",
        messages=messages,
        output_config={"effort": "low"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        return "Sorry, I can't help with that one. A local repair pro is the safest bet."
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def handle_turn(session_id: str, text: str, media_paths: list[Path], want_voice: bool = True,
                allow_text_only: bool = False, source: str = "chat") -> Reply:
    """allow_text_only: run the full diagnosis from a spoken/typed description with no media.
    source labels the run record (phone, camera, voice, web, imessage, chat)."""
    session = get_session(session_id)
    with session.lock:
        t0 = time.time()
        timings = {}
        if text:
            session.history.append({"role": "user", "content": text})
        # Record a run whenever this turn will (likely) diagnose, so the dashboard can show progress.
        run = None
        if media_paths or (session.plan is None and (session.frames or (allow_text_only and text))):
            run = runs.start(session_id, source, text)
            runs.stage(run, "ingest", "running", files=len(media_paths))
        new_frames = ingest(session, media_paths)
        timings["ingest"] = round(time.time() - t0, 1)
        runs.stage(run, "ingest", "done", frames=new_frames, transcripts=len(session.transcripts))
        runs.update(run, frames=[runs.media_url(p) for p in session.frames], transcripts=session.transcripts)

        try:
            if not session.frames and session.plan is None and not (allow_text_only and text):
                reply = Reply("Hi! I'm FixCam 🔧 Send me a short video (or photo) of what's broken and "
                              "tell me what's going on. I'll figure out the fix and mark it up for you.")
                runs.finish(run, "skipped")
            elif new_frames or session.plan is None:
                reply = _full_pipeline(session, timings, want_voice, run)
            else:
                reply = Reply(follow_up(session, text))
                runs.finish(run, "skipped")
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            print(f"[fixcam] transient Claude error: {e!r}")
            reply = Reply("I'm a bit overloaded right now. Give me a minute and send that again?")
            runs.finish(run, "error", repr(e))
        except Exception as e:
            runs.finish(run, "error", repr(e))
            raise

        session.history.append({"role": "assistant", "content": reply.text})
        timings["total"] = round(time.time() - t0, 1)
        reply.timings = timings
        print(f"[fixcam] {session_id} timings={timings}")
        return reply


def _full_pipeline(session: Session, timings: dict, want_voice: bool, run: dict | None = None) -> Reply:
    refused = "Sorry, I can't help with that one. A local repair pro is the safest bet."
    t = time.time()
    runs.stage(run, "diagnose", "running", model=config.CLAUDE_MODEL, frames=len(session.frames))
    diagnosis = diagnose(session)
    timings["diagnose"] = round(time.time() - t, 1)
    if diagnosis is None:
        runs.stage(run, "diagnose", "error", reason="refused")
        runs.finish(run, "error", "refused")
        return Reply(refused)
    session.diagnosis = diagnosis
    runs.update(run, diagnosis=diagnosis.model_dump())
    runs.stage(run, "diagnose", "done", object=diagnosis.object_type, error_code=diagnosis.visible_error,
               confidence=diagnosis.confidence)
    if diagnosis.clarifying_question:
        for s in ("research", "plan", "render", "guide"):
            runs.stage(run, s, "skipped")
        runs.finish(run, "needs_info")
        return Reply(diagnosis.clarifying_question)

    t = time.time()
    runs.stage(run, "research", "running", queries=diagnosis.search_queries)
    sources = keenable.research(diagnosis.search_queries)
    timings["research"] = round(time.time() - t, 1)
    runs.update(run, sources=[{k: v for k, v in s.items() if k != "content"} | {"extracted": bool(s.get("content"))}
                              for s in sources])
    runs.stage(run, "research", "done", sources=len(sources), pages_read=sum(bool(s.get("content")) for s in sources))

    t = time.time()
    runs.stage(run, "plan", "running", model=config.CLAUDE_MODEL)
    plan = plan_fix(session, diagnosis, sources)
    timings["plan"] = round(time.time() - t, 1)
    if plan is None:
        runs.stage(run, "plan", "error", reason="refused")
        runs.finish(run, "error", "refused")
        return Reply(refused)
    session.plan = plan
    runs.update(run, plan=plan.model_dump())
    runs.stage(run, "plan", "done", steps=len(plan.steps), parts=len(plan.parts))

    t = time.time()
    runs.stage(run, "render", "running")
    images = render(session, plan)
    it = iter(images)  # render() draws one image per step that points at a valid frame, in order
    step_images = [runs.media_url(next(it, "")) if 0 <= s.frame_index < len(session.frames) else ""
                   for s in plan.steps]
    runs.update(run, images=[runs.media_url(p) for p in images], step_images=step_images)
    runs.stage(run, "render", "done", images=len(images))
    runs.finish(run, "done")
    text = plan.reply_text
    if plan.safety_warning:
        text = f"⚠️ {plan.safety_warning}\n\n{text}"
    links = [{"url": p.url, "title": p.name} for p in plan.parts if p.url][:2]
    if not links and plan.source_urls:
        links = [{"url": plan.source_urls[0], "title": None}]
    voice = speech.synthesize(plan.voice_script, session.dir / f"voice_{int(time.time())}.wav") if want_voice else None
    timings["render"] = round(time.time() - t, 1)
    _save_trace(session, sources)
    return Reply(text=text, images=images, links=links, voice_wav=voice)


def _save_trace(session: Session, sources: list[dict]) -> None:
    session.dir.mkdir(parents=True, exist_ok=True)
    (session.dir / "trace.json").write_text(json.dumps({
        "diagnosis": session.diagnosis.model_dump() if session.diagnosis else None,
        "plan": session.plan.model_dump() if session.plan else None,
        "sources": [{k: v for k, v in s.items() if k != "content"} for s in sources],
        "transcripts": session.transcripts,
        "frames": [str(p) for p in session.frames],
    }, indent=2))
