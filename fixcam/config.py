"""Environment-driven settings. Every integration is optional so the demo degrades gracefully."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("FIXCAM_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SAMPLES_DIR = ROOT / "samples"

# Claude (vision + reasoning)
CLAUDE_MODEL = os.getenv("FIXCAM_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("FIXCAM_CLAUDE_EFFORT", "medium")

# Chert (iMessage)
CHERT_BASE_URL = os.getenv("CHERT_BASE_URL", "https://console.trychert.com")
CHERT_SIGNING_SECRET = os.getenv("CHERT_SIGNING_SECRET", "")
CHERT_TENANT_SLUG = os.getenv("CHERT_TENANT_SLUG", "")
CHERT_WEBHOOK_SECRET = os.getenv("CHERT_WEBHOOK_SECRET", "")
CHERT_PHONE_LINE_ID = os.getenv("CHERT_PHONE_LINE_ID", "")  # needed only for voice-note replies

# Keenable (web search)
KEENABLE_API_KEY = os.getenv("KEENABLE_API_KEY", "")
KEENABLE_BASE_URL = os.getenv("KEENABLE_BASE_URL", "https://api.keenable.ai")

# Speech (OpenAI-compatible STT/TTS; any compatible base URL works)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or None
STT_MODEL = os.getenv("FIXCAM_STT_MODEL", "whisper-1")
TTS_MODEL = os.getenv("FIXCAM_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("FIXCAM_TTS_VOICE", "alloy")

# Public base URL of this server (for image links in eval/demo replies)
PUBLIC_BASE_URL = os.getenv("FIXCAM_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

MAX_FRAMES = int(os.getenv("FIXCAM_MAX_FRAMES", "8"))
