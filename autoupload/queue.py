"""The on-disk post queue.

Layout::

    queue/
      001-sunrise.jpg          single-image post
      001-sunrise.txt          optional caption for it
      002-trip/                carousel post (2-10 JPEGs, sorted by name)
        a.jpg
        b.jpg
        caption.txt            optional caption

Items are posted in filename order. After a run an item is moved to
``posted/`` (success) or ``failed/`` (Instagram can never accept it).
"""

from __future__ import annotations

import json
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

JPEG_SUFFIXES = {".jpg", ".jpeg"}
OTHER_IMAGE_SUFFIXES = {".png", ".webp", ".gif", ".heic", ".bmp", ".tif", ".tiff"}
MAX_BYTES = 8 * 1024 * 1024
MIN_ASPECT, MAX_ASPECT = 4 / 5, 1.91
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_CAROUSEL = 10


@dataclass
class Item:
    name: str
    images: list[Path]
    caption: str
    paths: list[Path]  # everything that moves together when archived
    problems: list[str] = field(default_factory=list)

    @property
    def is_carousel(self) -> bool:
        return len(self.images) > 1


def jpeg_size(path: Path) -> tuple[int, int]:
    """Return (width, height) of a JPEG by reading its frame header."""
    with path.open("rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise ValueError("not a JPEG file")
        while True:
            byte = fh.read(1)
            if not byte:
                raise ValueError("no JPEG frame header found")
            if byte != b"\xff":
                continue
            marker = fh.read(1)
            while marker == b"\xff":
                marker = fh.read(1)
            if not marker:
                raise ValueError("truncated JPEG")
            m = marker[0]
            if m in (0x00, 0x01) or 0xD0 <= m <= 0xD9:
                continue  # stuffing / standalone markers carry no length
            head = fh.read(2)
            if len(head) < 2:
                raise ValueError("truncated JPEG")
            (length,) = struct.unpack(">H", head)
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                data = fh.read(5)
                if len(data) < 5:
                    raise ValueError("truncated JPEG")
                height, width = struct.unpack(">xHH", data)
                if not width or not height:
                    raise ValueError("JPEG has zero dimensions")
                return width, height
            fh.seek(length - 2, 1)


def _check_image(path: Path) -> list[str]:
    label = path.name
    size = path.stat().st_size
    if size > MAX_BYTES:
        return [f"{label}: {size / 1e6:.1f} MB exceeds Instagram's 8 MB limit"]
    try:
        width, height = jpeg_size(path)
    except (ValueError, OSError) as exc:
        return [f"{label}: {exc}"]
    aspect = width / height
    if not (MIN_ASPECT - 0.005 <= aspect <= MAX_ASPECT + 0.005):
        return [
            f"{label}: aspect ratio {aspect:.2f} ({width}x{height}) is outside "
            f"Instagram's allowed range 4:5 (0.80) to 1.91:1"
        ]
    return []


def _check_caption(caption: str) -> list[str]:
    problems = []
    if len(caption) > MAX_CAPTION_CHARS:
        problems.append(
            f"caption is {len(caption)} characters (max {MAX_CAPTION_CHARS})"
        )
    tags = sum(1 for word in caption.split() if word.startswith("#") and len(word) > 1)
    if tags > MAX_HASHTAGS:
        problems.append(f"caption has {tags} hashtags (max {MAX_HASHTAGS})")
    return problems


def _read_caption(path: Path, default: str) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return default


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def load_queue(queue_dir: Path, default_caption: str = "") -> list[Item]:
    """All queued items in posting order, each with any validation problems."""
    if not queue_dir.is_dir():
        return []
    items: list[Item] = []
    for entry in sorted(queue_dir.iterdir(), key=lambda p: p.name.lower()):
        if _is_hidden(entry):
            continue
        suffix = entry.suffix.lower()
        if entry.is_dir():
            files = sorted(
                (p for p in entry.iterdir() if p.is_file() and not _is_hidden(p)),
                key=lambda p: p.name.lower(),
            )
            images = [p for p in files if p.suffix.lower() in JPEG_SUFFIXES]
            item = Item(
                name=entry.name,
                images=images,
                caption=_read_caption(entry / "caption.txt", default_caption),
                paths=[entry],
            )
            wrong = [p.name for p in files if p.suffix.lower() in OTHER_IMAGE_SUFFIXES]
            if wrong:
                item.problems.append(
                    f"only JPEG is supported by the Instagram API: {', '.join(wrong)}"
                )
            if not images:
                item.problems.append("folder contains no JPEG images")
            elif len(images) > MAX_CAROUSEL:
                item.problems.append(
                    f"{len(images)} images; a carousel allows at most {MAX_CAROUSEL}"
                )
        elif suffix in JPEG_SUFFIXES:
            sidecar = entry.with_suffix(".txt")
            item = Item(
                name=entry.name,
                images=[entry],
                caption=_read_caption(sidecar, default_caption),
                paths=[entry] + ([sidecar] if sidecar.is_file() else []),
            )
        elif suffix in OTHER_IMAGE_SUFFIXES:
            item = Item(
                name=entry.name,
                images=[],
                caption="",
                paths=[entry],
                problems=[
                    "only JPEG is supported by the Instagram API; convert this file"
                ],
            )
        else:
            continue  # captions, notes, README, ...
        for image in item.images[:MAX_CAROUSEL]:
            item.problems.extend(_check_image(image))
        item.problems.extend(_check_caption(item.caption))
        items.append(item)
    return items


def archive(item: Item, dest_dir: Path, record: dict) -> None:
    """Move an item out of the queue and append ``record`` to dest_dir/log.jsonl."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    clash = any((dest_dir / p.name).exists() for p in item.paths)
    for path in item.paths:
        name = f"{stamp}-{path.name}" if clash else path.name
        shutil.move(str(path), str(dest_dir / name))
    entry = {"item": item.name, "at": now.isoformat(timespec="seconds"), **record}
    with (dest_dir / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
