"""Draw numbered step callouts (box + arrow + label) onto the user's own frames."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (255, 59, 48)
WHITE = (255, 255, 255)


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _pointer(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], w: int, h: int, stroke: int) -> None:
    """Short arrow pointing at the box from outside it, on the side facing the image centre."""
    x0, y0, x1, y1 = box
    # Box corner nearest the image centre, and a tail further out in that direction.
    hx = x0 if (x0 + x1) / 2 > w / 2 else x1
    hy = y0 if (y0 + y1) / 2 > h / 2 else y1
    dx, dy = (-1 if hx == x0 else 1), (-1 if hy == y0 else 1)
    length = max(40, w // 9)
    tx = min(max(hx + dx * length, stroke), w - stroke)
    ty = min(max(hy + dy * length, stroke), h - stroke)
    d.line((tx, ty, hx, hy), fill=ACCENT, width=stroke)
    # Arrowhead
    ux, uy = hx - tx, hy - ty
    norm = max((ux * ux + uy * uy) ** 0.5, 1)
    ux, uy = ux / norm, uy / norm
    size = stroke * 4
    left = (hx - ux * size - uy * size * 0.6, hy - uy * size + ux * size * 0.6)
    right = (hx - ux * size + uy * size * 0.6, hy - uy * size - ux * size * 0.6)
    d.polygon([(hx, hy), left, right], fill=ACCENT)


def draw_step(frame: Path, dst: Path, step_no: int, label: str, box: list[int] | None) -> Path:
    """box is [x0, y0, x1, y1] in pixel coordinates of the frame."""
    im = Image.open(frame).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    stroke = max(4, w // 160)
    font = _font(max(18, w // 28))

    if box and len(box) == 4:
        x0, x1 = sorted(min(max(int(v), 0), w - 1) for v in (box[0], box[2]))
        y0, y1 = sorted(min(max(int(v), 0), h - 1) for v in (box[1], box[3]))
        pad = stroke * 2  # breathing room so the outline doesn't sit on the object's edge
        x0, y0, x1, y1 = max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, w - 1), min(y1 + pad, h - 1)
        d.rounded_rectangle((x0, y0, x1, y1), radius=stroke * 3, outline=ACCENT, width=stroke)
        _pointer(d, (x0, y0, x1, y1), w, h, stroke)

    # Caption banner at the bottom.
    text = f"{step_no}. {label}"
    pad = stroke * 3
    tw = d.textlength(text, font=font)
    while tw > w - 4 * pad and len(text) > 8:
        text = text[:-4] + "…"
        tw = d.textlength(text, font=font)
    bh = font.size + 2 * pad
    d.rectangle((0, h - bh, w, h), fill=(0, 0, 0))
    badge = font.size + pad
    d.ellipse((pad, h - bh + (bh - badge) // 2, pad + badge, h - bh + (bh + badge) // 2), fill=ACCENT)
    d.text((pad + badge / 2, h - bh / 2), str(step_no), fill=WHITE, font=font, anchor="mm")
    d.text((pad * 2 + badge, h - bh / 2), text.split(". ", 1)[1], fill=WHITE, font=font, anchor="lm")

    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "JPEG", quality=88)
    return dst
