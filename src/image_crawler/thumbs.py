"""Small WebP previews so the viewer never has to download originals just to show a grid."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

THUMB_SIZE = 320
THUMB_QUALITY = 70
_ROTATED_ORIENTATIONS = {5, 6, 7, 8}


@dataclass
class Preview:
    width: int | None
    height: int | None
    thumbnail: bytes | None  # WebP, or None when the format can't be decoded (e.g. SVG)


def make_preview(data: bytes) -> Preview:
    try:
        with Image.open(BytesIO(data)) as img:
            width, height = img.size
            if img.getexif().get(0x0112) in _ROTATED_ORIENTATIONS:
                width, height = height, width
            if img.format == "JPEG":
                img.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
            img.seek(0)  # first frame of animations
            frame = ImageOps.exif_transpose(img)
            has_alpha = frame.mode in ("RGBA", "LA", "PA") or "transparency" in frame.info
            frame = frame.convert("RGBA" if has_alpha else "RGB")
            frame.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            out = BytesIO()
            frame.save(out, "WEBP", quality=THUMB_QUALITY, method=4)
            return Preview(width, height, out.getvalue())
    except Exception as e:  # unsupported format, decompression bomb, truncated file...
        log.info("no thumbnail: %s", e)
        return Preview(None, None, None)
