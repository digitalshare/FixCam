"""Speech-to-text for voice notes / video audio, text-to-speech for voice replies."""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from . import config


def _client() -> OpenAI | None:
    if not config.OPENAI_API_KEY:
        return None
    return OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)


def transcribe(wav: Path) -> str:
    client = _client()
    if client is None:
        return ""
    try:
        with wav.open("rb") as f:
            return client.audio.transcriptions.create(model=config.STT_MODEL, file=f).text.strip()
    except Exception as e:
        print(f"[speech] transcription failed: {e}")
        return ""


def synthesize(text: str, dst: Path) -> Path | None:
    client = _client()
    if client is None or not text:
        return None
    try:
        with client.audio.speech.with_streaming_response.create(
            model=config.TTS_MODEL, voice=config.TTS_VOICE, input=text, response_format="wav"
        ) as resp:
            resp.stream_to_file(dst)
        return dst
    except Exception as e:
        print(f"[speech] TTS failed: {e}")
        return None
