"""Make queued items acceptable to Instagram before they are posted.

* converts PNG/WebP/HEIC/GIF/BMP/TIFF to JPEG
* pads (or crops) images outside the 4:5 to 1.91:1 aspect range
* shrinks and recompresses images over the size limits
* optionally drafts a caption with Claude for items that have none

Files are changed in place; the originals remain in git history.
"""

from __future__ import annotations

import io
from pathlib import Path

from .queue import (
    CONVERTIBLE_SUFFIXES,
    MAX_ASPECT,
    MAX_IMAGE_BYTES,
    MIN_ASPECT,
    Item,
    aspect_ok,
    is_video,
    jpeg_size,
    load_queue,
)


def _open(path: Path):
    from PIL import Image, ImageOps

    if path.suffix.lower() in (".heic", ".heif"):
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError as exc:
            raise ValueError("HEIC support needs the pillow-heif package") from exc
    image = Image.open(path)
    image.load()
    image = ImageOps.exif_transpose(image)
    return image


def _flatten(image, color: str):
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, color)
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _fit(image, mode: str, color: str):
    from PIL import Image

    width, height = image.size
    if aspect_ok(width, height):
        return image
    target = MIN_ASPECT if width / height < MIN_ASPECT else MAX_ASPECT
    if mode == "crop":
        if width / height < target:  # too tall
            new_h = round(width / target)
            top = (height - new_h) // 2
            return image.crop((0, top, width, top + new_h))
        new_w = round(height * target)
        left = (width - new_w) // 2
        return image.crop((left, 0, left + new_w, height))
    if width / height < target:  # too tall: widen the canvas
        canvas = Image.new("RGB", (round(height * target), height), color)
    else:  # too wide: heighten the canvas
        canvas = Image.new("RGB", (width, round(width / target)), color)
    canvas.paste(image, ((canvas.width - width) // 2, (canvas.height - height) // 2))
    return canvas


def needs_fix(path: Path, story: bool) -> bool:
    if path.suffix.lower() in CONVERTIBLE_SUFFIXES:
        return True
    if path.stat().st_size > MAX_IMAGE_BYTES:
        return True
    if story:
        return False
    try:
        return not aspect_ok(*jpeg_size(path))
    except (ValueError, OSError):
        return False  # unreadable: reported as a problem by the queue check


def fix_image(path: Path, config: dict, story: bool = False) -> Path:
    """Rewrite ``path`` as a compliant JPEG and return the (possibly new) path."""
    from PIL import Image

    settings = config["images"]
    target = path.with_suffix(".jpg") if path.suffix.lower() in CONVERTIBLE_SUFFIXES else path
    if target != path and target.exists():
        raise ValueError(f"cannot convert: {target.name} already exists")

    try:
        image = _flatten(_open(path), settings["pad_color"])
    except Exception as exc:  # PIL raises many unrelated types for bad files
        raise ValueError(f"cannot read image: {exc}") from exc
    if not story:
        image = _fit(image, settings["fit"], settings["pad_color"])
    if image.width > settings["max_width"]:
        ratio = settings["max_width"] / image.width
        image = image.resize((settings["max_width"], round(image.height * ratio)), Image.LANCZOS)

    quality = int(settings["jpeg_quality"])
    while True:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= MAX_IMAGE_BYTES:
            break
        if quality > 60:
            quality -= 10
        else:
            image = image.resize(
                (round(image.width * 0.85), round(image.height * 0.85)), Image.LANCZOS
            )
    target.write_bytes(buffer.getvalue())
    if target != path:
        path.unlink()
    return target


def prepare_images(queue_dir: Path, config: dict, log=print) -> int:
    changed = 0
    for item in load_queue(queue_dir, config):
        for path in item.media:
            story = item.kind == "story"
            if is_video(path) or not needs_fix(path, story):
                continue
            try:
                new = fix_image(path, config, story)
            except ValueError as exc:
                log(f"  ! {item.name}: {path.name}: {exc}")
                continue
            log(f"  fixed {item.name}: {path.name}" + (f" -> {new.name}" if new != path else ""))
            changed += 1
    return changed


def wants_ai_caption(item: Item) -> bool:
    return (
        item.kind in ("image", "carousel")
        and not item.has_caption_file
        and not item.problems
        and not item.fixes
        and any(not is_video(p) for p in item.media)
    )


def prepare_captions(queue_dir: Path, config: dict, log=print, draft=None) -> int:
    from .ai import AIError, draft_caption

    draft = draft or draft_caption
    written = 0
    for item in load_queue(queue_dir, config):
        if not wants_ai_caption(item):
            continue
        images = [p for p in item.media if not is_video(p)]
        try:
            caption = draft(images, config)
        except AIError as exc:
            log(f"  ! {item.name}: no AI caption: {exc}")
            if "ANTHROPIC_API_KEY" in str(exc):
                break
            continue
        item.caption_path.write_text(caption + "\n", encoding="utf-8")
        log(f"  drafted caption for {item.name}")
        written += 1
    return written
