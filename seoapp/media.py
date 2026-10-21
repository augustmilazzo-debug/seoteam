"""Client photo library: storage, processing and selection."""

import io
import re
import secrets

from PIL import Image, ImageOps

from . import config
from .db import MediaItem

MEDIA_DIR = config.DATA_DIR / "media"
MAX_SIDE = 2000  # long edge for stored photos
ANALYZE_SIDE = 1400  # long edge sent to Claude for review
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "image"


def path_for(m: MediaItem):
    return MEDIA_DIR / str(m.client_id) / f"{m.token}-{m.filename}"


def url_for(m: MediaItem, absolute: bool = False) -> str:
    rel = f"/media/{m.token}/{m.filename}"
    return (config.APP_BASE_URL + rel) if absolute and config.APP_BASE_URL else rel


def _store(m: MediaItem, data: bytes) -> None:
    p = path_for(m)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def process_upload(raw: bytes, original_name: str) -> tuple[bytes, int, int]:
    """Rotate per EXIF, drop all metadata (GPS, camera, patient info), resize and compress to JPEG."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.convert("RGBA").split()[-1])
        img = bg
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=84, optimize=True, progressive=True)
    return out.getvalue(), img.width, img.height


def add_upload(db, client_id: int, raw: bytes, original_name: str) -> MediaItem:
    data, w, h = process_upload(raw, original_name)
    stem = _slug(original_name.rsplit(".", 1)[0])
    m = MediaItem(client_id=client_id, source="upload", kind="photo", token=secrets.token_urlsafe(18),
                  filename=f"{stem}.jpg", mime="image/jpeg", width=w, height=h, title=stem.replace("-", " "),
                  analysis="pending")
    db.add(m)
    db.flush()
    _store(m, data)
    return m


def add_generated(db, client_id: int, png: bytes, kind: str, name: str, alt: str, spec: dict,
                  blog_post_id: int | None) -> MediaItem:
    img = Image.open(io.BytesIO(png))
    m = MediaItem(client_id=client_id, source="claude", kind=kind, token=secrets.token_urlsafe(18),
                  filename=f"{_slug(name)}.png", mime="image/png", width=img.width, height=img.height,
                  title=name[:300], alt_text=alt[:300], analysis="done", approved=True, spec=spec,
                  blog_post_id=blog_post_id)
    db.add(m)
    db.flush()
    _store(m, png)
    return m


def read(m: MediaItem) -> bytes:
    return path_for(m).read_bytes()


def for_analysis(m: MediaItem) -> bytes:
    img = Image.open(io.BytesIO(read(m)))
    img.thumbnail((ANALYZE_SIDE, ANALYZE_SIDE))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=80)
    return out.getvalue()


def delete(db, m: MediaItem) -> None:
    try:
        path_for(m).unlink()
    except FileNotFoundError:
        pass
    db.delete(m)


def usable(m: MediaItem) -> bool:
    """May be placed in content automatically: approved, consent where people appear, never before/after."""
    return (m.source == "upload" and m.approved and not m.before_after
            and (m.consent or not m.has_people))


def library_view(items: list[MediaItem]) -> list[dict]:
    return [{"id": m.id, "title": m.title, "description": m.description[:300], "tags": m.tags,
             "orientation": "landscape" if m.width >= m.height else "portrait"} for m in items if usable(m)]
