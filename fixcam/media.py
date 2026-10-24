"""ffmpeg helpers: sniff media type, sample key frames, pull audio tracks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

FRAME_MAX_SIDE = 1024


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def probe(path: Path) -> dict:
    out = _run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(out)


def classify(path: Path) -> str:
    """Return 'image', 'video', 'audio' or 'other' by inspecting the bytes, not the MIME hint."""
    try:
        Image.open(path).verify()
        return "image"
    except Exception:
        pass
    try:
        info = probe(path)
    except subprocess.CalledProcessError:
        return "other"
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    duration = float(info.get("format", {}).get("duration") or 0)
    if "video" in kinds and duration > 0.5:
        return "video"
    if "audio" in kinds:
        return "audio"
    if "video" in kinds:  # single-frame "video" such as HEIC sequences
        return "image"
    return "other"


def _normalize_image(src: Path, dst: Path) -> Path:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((FRAME_MAX_SIDE, FRAME_MAX_SIDE))
        im.save(dst, "JPEG", quality=88)
    return dst


def image_to_frame(src: Path, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{name}.jpg"
    try:
        return _normalize_image(src, dst)
    except Exception:
        # HEIC and other formats Pillow cannot open: let ffmpeg decode.
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-frames:v", "1", str(dst)])
        return _normalize_image(dst, dst)


def sample_frames(video: Path, out_dir: Path, max_frames: int) -> list[tuple[Path, float]]:
    """Evenly sample up to max_frames frames; returns (path, timestamp_seconds)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = float(probe(video)["format"].get("duration") or 0)
    count = max(1, min(max_frames, int(duration) + 1))
    frames = []
    for i in range(count):
        ts = duration * (i + 0.5) / count
        dst = out_dir / f"frame_{i:02d}.jpg"
        _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", str(video),
              "-frames:v", "1", "-vf", f"scale='min({FRAME_MAX_SIDE},iw)':-2", str(dst)])
        if dst.exists():
            frames.append((_normalize_image(dst, dst), ts))
    return frames


def extract_audio_wav(src: Path, dst: Path) -> Path | None:
    """Mono 16 kHz WAV from any audio/video container (CAF/Opus voice memos included)."""
    try:
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst)])
    except subprocess.CalledProcessError:
        return None
    return dst if dst.exists() and dst.stat().st_size > 1000 else None


def wav_duration(path: Path) -> float:
    return float(probe(path)["format"].get("duration") or 0)
