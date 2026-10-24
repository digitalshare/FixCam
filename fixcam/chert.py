"""Chert iMessage API client: inbound attachments, outbound text/media/links/voice notes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import re
import time
import uuid
from pathlib import Path

import httpx

from . import config


def enabled() -> bool:
    return bool(config.CHERT_SIGNING_SECRET)


def _headers(json_body: bool = True) -> dict[str, str]:
    h = {"Authorization": f"Bearer {config.CHERT_SIGNING_SECRET}"}
    if config.CHERT_TENANT_SLUG:
        h["x-chert-tenant"] = config.CHERT_TENANT_SLUG
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.CHERT_BASE_URL, timeout=120)


def verify_webhook(raw_body: bytes, header: str | None) -> bool:
    """Accepts both `t=<ts>,v1=<hex>` and legacy `v1,<ts>,<hex>` signature formats."""
    if not config.CHERT_WEBHOOK_SECRET:
        return True  # verification disabled for local development
    m = re.match(r"^v1,(\d+),([a-f0-9]+)$", header or "") or re.match(r"^t=(\d+),v1=([a-f0-9]+)$", header or "")
    if not m:
        return False
    ts, sig = m.group(1), m.group(2)
    if abs(time.time() - int(ts)) > 300:
        return False
    expected = hmac.new(config.CHERT_WEBHOOK_SECRET.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def download_attachment(attachment_id: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with _client() as c:
        r = c.get(f"/api/v1/attachments/{attachment_id}/content", headers=_headers(json_body=False))
        r.raise_for_status()
    m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition", ""), re.I)
    name = Path(m.group(1)).name if m else attachment_id
    path = dest_dir / f"{attachment_id}_{name}"
    path.write_bytes(r.content)
    return path


def upload_attachment(path: Path) -> str:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = path.read_bytes()
    with _client() as c:
        r = c.post("/api/v1/attachments", headers=_headers(),
                   json={"filename": path.name, "content_type": content_type, "size_bytes": len(data)})
        r.raise_for_status()
        slot = r.json()
        put_headers = _headers(json_body=False) | {"Content-Type": content_type}
        c.put(slot["upload_url"], headers=put_headers, content=data).raise_for_status()
    return slot["attachment_id"]


def send_parts(chat_id: str, parts: list[dict], parallel_attachments: bool = False) -> dict:
    body: dict = {"message": {"parts": parts, "idempotency_key": str(uuid.uuid4())}}
    if parallel_attachments:
        body["parallel_attachments"] = True
    with _client() as c:
        r = c.post(f"/api/v1/chats/{chat_id}/messages", headers=_headers(), json=body)
        r.raise_for_status()
        return r.json()


def send_text(chat_id: str, text: str) -> dict:
    return send_parts(chat_id, [{"type": "text", "value": text}])


def send_images(chat_id: str, paths: list[Path]) -> None:
    if not paths:
        return
    ids = [upload_attachment(p) for p in paths]
    send_parts(chat_id, [{"type": "media", "attachment_id": i} for i in ids], parallel_attachments=len(ids) > 1)


def send_link(chat_id: str, url: str, title: str | None = None) -> None:
    part = {"type": "rich_link", "url": url}
    if title:
        part["title"] = title
    send_parts(chat_id, [part])


def typing(chat_id: str, state: str = "typing") -> None:
    try:
        with _client() as c:
            c.post(f"/api/v1/chats/{chat_id}/typing", headers=_headers(), json={"state": state})
    except httpx.HTTPError:
        pass  # best-effort UI signal


def send_voice_note(phone: str, wav: Path, duration_seconds: float) -> None:
    """Stage a WAV as a native voice note, then send it from the same line."""
    if not config.CHERT_PHONE_LINE_ID:
        return
    with _client() as c:
        r = c.post("/api/v1/voice-notes", headers=_headers(), json={
            "audio_base64": base64.b64encode(wav.read_bytes()).decode(),
            "mime_type": "audio/wav",
            "duration_seconds": round(duration_seconds, 2),
            "phone_line_id": config.CHERT_PHONE_LINE_ID,
        })
        r.raise_for_status()
        staged = r.json()
        c.post("/api/v1/send", headers=_headers(), json={
            "phone": phone,
            "phone_line_id": staged["phone_line_id"],
            "voice_note": {"voice_note_id": staged["voice_note_id"]},
            "idempotency_key": str(uuid.uuid4()),
        }).raise_for_status()
