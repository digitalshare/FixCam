"""Handle one Chert `message.received` event end to end (shared by webhook and SSE listener)."""

from __future__ import annotations

import threading
import traceback
from collections import OrderedDict

from . import agent, chert, config, media

_seen: OrderedDict[str, None] = OrderedDict()
_seen_lock = threading.Lock()

RESET_WORDS = {"reset", "new", "start over", "restart"}


def _first_time(event_id: str) -> bool:
    with _seen_lock:
        if event_id in _seen:
            return False
        _seen[event_id] = None
        while len(_seen) > 5000:
            _seen.popitem(last=False)
        return True


def handle_event(event: dict) -> None:
    if event.get("event_type", event.get("type")) != "message.received":
        return
    if not _first_time(event.get("event_id") or event.get("id") or ""):
        return
    data = event.get("data") or {}
    msg, chat = data.get("message") or {}, data.get("chat") or {}
    if msg.get("direction") != "inbound" or chat.get("is_group"):
        return
    chat_id = chat["id"]
    phone = (msg.get("sender_handle") or {}).get("handle", "")
    try:
        _respond(chat_id, phone, msg.get("parts") or [])
    except Exception:
        traceback.print_exc()
        chert.send_text(chat_id, "Hmm, something went wrong on my side. Mind sending that again?")
    finally:
        chert.typing(chat_id, "stopped")


def _respond(chat_id: str, phone: str, parts: list[dict]) -> None:
    text = " ".join(p.get("value", "") for p in parts if p.get("type") == "text").strip()
    if text.lower() in RESET_WORDS:
        agent.reset_session(chat_id)
        chert.send_text(chat_id, "Fresh start 👍 Send me a video of what needs fixing.")
        return

    session = agent.get_session(chat_id)
    inbox = session.dir / "inbox"
    paths = [chert.download_attachment(p["attachment_id"], inbox)
             for p in parts if p.get("type") == "media" and p.get("attachment_id")]
    has_visual = any(media.classify(p) in ("video", "image") for p in paths)
    if has_visual:
        chert.send_text(chat_id, "Got it, taking a look 👀 give me ~30 seconds.")
    chert.typing(chat_id)

    reply = agent.handle_turn(chat_id, text, paths, want_voice=bool(config.CHERT_PHONE_LINE_ID),
                              source="imessage")
    chert.send_text(chat_id, reply.text)
    chert.send_images(chat_id, reply.images)
    for link in reply.links:
        chert.send_link(chat_id, link["url"], link.get("title"))
    if reply.voice_wav:
        try:
            chert.send_voice_note(phone, reply.voice_wav, media.wav_duration(reply.voice_wav))
        except Exception as e:
            print(f"[chert] voice note failed: {e}")
