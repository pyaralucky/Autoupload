"""The on-disk post queue.

Layout::

    queue/
      001-sunrise.jpg            image post
      001-sunrise.txt            its caption (optional)
      002-trip/                  carousel: 2-10 images/videos, in filename order
        a.jpg
        b.jpg
        caption.txt
      003-clip.mp4               Reel
      2026-12-25-xmas.jpg        held until 25 Dec (add -HHMM for a time)
      stories/                   everything in here is posted as a Story
        teaser.jpg

Caption files may contain directive lines, which are removed from the caption::

    @alt: Sunrise over the hills          (repeat once per carousel image)
    @location: 106078429431815            (Facebook Page id of a place)
    @comment: posted as the first comment
    @crosspost: off

Items go out in filename order, except that dated items go first once due.
Afterwards an item moves to ``posted/`` or, if it can never be accepted, to
``failed/``.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

JPEG_SUFFIXES = {".jpg", ".jpeg"}
CONVERTIBLE_SUFFIXES = {".png", ".webp", ".gif", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
IMAGE_SUFFIXES = JPEG_SUFFIXES | CONVERTIBLE_SUFFIXES
VIDEO_SUFFIXES = {".mp4", ".mov"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # GitHub's per-file limit
MIN_ASPECT, MAX_ASPECT = 4 / 5, 1.91
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_CAROUSEL = 10
STORIES_DIR = "stories"
ACCOUNT_PREFIX = "@"

_DIRECTIVE = re.compile(r"^@(alt|location|comment|crosspost|account):\s*(.*)$", re.IGNORECASE)
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[-_T](\d{2})(\d{2}))?(?=[-_ .]|$)")


@dataclass
class Item:
    name: str
    kind: str  # image | carousel | reel | story
    media: list[Path]
    paths: list[Path]  # everything that moves together when archived
    caption: str = ""
    alt: list[str] = field(default_factory=list)
    location: str = ""
    comment: str = ""
    crosspost: bool = True
    accounts: list[str] | None = None  # None: every account
    has_caption_file: bool = False
    not_before: datetime | None = None  # naive, in the schedule's timezone
    problems: list[str] = field(default_factory=list)  # can never be posted as is
    fixes: list[str] = field(default_factory=list)  # `prepare` will resolve these

    @property
    def caption_path(self) -> Path:
        first = self.paths[0]
        return first / "caption.txt" if first.is_dir() else first.with_suffix(".txt")


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


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


def aspect_ok(width: int, height: int) -> bool:
    return MIN_ASPECT - 0.005 <= width / height <= MAX_ASPECT + 0.005


def _check_media(item: Item, path: Path) -> None:
    label = path.name
    size = path.stat().st_size
    if is_video(path):
        if size > MAX_VIDEO_BYTES:
            item.problems.append(
                f"{label}: {size / 1e6:.0f} MB exceeds the 100 MB limit for files in a repository"
            )
        return
    if path.suffix.lower() in CONVERTIBLE_SUFFIXES:
        item.fixes.append(f"{label}: will be converted to JPEG")
        return
    try:
        width, height = jpeg_size(path)
    except (ValueError, OSError) as exc:
        item.problems.append(f"{label}: {exc}")
        return
    if size > MAX_IMAGE_BYTES:
        item.fixes.append(f"{label}: {size / 1e6:.1f} MB exceeds 8 MB; will be compressed")
    if item.kind != "story" and not aspect_ok(width, height):
        item.fixes.append(
            f"{label}: aspect ratio {width / height:.2f} ({width}x{height}) is outside "
            f"4:5 to 1.91:1; will be adjusted"
        )


def count_hashtags(text: str) -> int:
    return sum(1 for word in text.split() if word.startswith("#") and len(word) > 1)


def _read_caption(item: Item, default: str) -> None:
    path = item.caption_path
    if not path.is_file():
        item.caption = default
        return
    item.has_caption_file = True
    body = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _DIRECTIVE.match(line.strip())
        if not match:
            body.append(line)
            continue
        key, value = match[1].lower(), match[2].strip()
        if key == "alt":
            item.alt.append(value)
        elif key == "location":
            item.location = value
        elif key == "comment":
            item.comment = f"{item.comment}\n{value}".strip()
        elif key == "account":
            item.accounts = [v.strip().lstrip("@") for v in value.split(",") if v.strip()]
        elif key == "crosspost":
            item.crosspost = value.lower() not in ("off", "no", "false", "0")
    item.caption = "\n".join(body).strip()


def _parse_date(name: str) -> tuple[datetime | None, str | None]:
    match = _DATE.match(name)
    if not match:
        return None, None
    year, month, day, hour, minute = (int(g) if g else 0 for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute), None
    except ValueError:
        return None, f"filename starts with an invalid date '{match[0]}'"


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def _visible_files(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and not _is_hidden(p)),
        key=lambda p: p.name.lower(),
    )


def _single(entry: Path, kind: str, name: str) -> Item:
    sidecar = entry.with_suffix(".txt")
    return Item(
        name=name,
        kind=kind,
        media=[entry],
        paths=[entry] + ([sidecar] if sidecar.is_file() else []),
    )


def compose_caption(item: Item, config: dict, post_index: int = 0) -> tuple[str, str]:
    """Final (caption, first comment) after footer and hashtag rotation."""
    settings = config["caption"]
    if item.kind == "story":
        return "", ""
    sets = [s for s in settings["hashtag_sets"] if s]
    tags = " ".join(sets[post_index % len(sets)]) if sets else ""
    parts = [item.caption, settings["footer"].strip()]
    comment = item.comment
    if settings["hashtags_in_first_comment"]:
        comment = "\n".join(p for p in (comment, tags) if p)
    else:
        parts.append(tags)
    return "\n\n".join(p for p in parts if p), comment


def _check_caption(item: Item, config: dict) -> None:
    sets = [s for s in config["caption"]["hashtag_sets"] if s] or [[]]
    longest = 0
    most_tags = 0
    for index in range(len(sets)):
        caption, _ = compose_caption(item, config, index)
        longest = max(longest, len(caption))
        most_tags = max(most_tags, count_hashtags(caption))
    if longest > MAX_CAPTION_CHARS:
        item.problems.append(
            f"caption is {longest} characters with footer and hashtags (max {MAX_CAPTION_CHARS})"
        )
    if most_tags > MAX_HASHTAGS:
        item.problems.append(f"caption has {most_tags} hashtags (max {MAX_HASHTAGS})")


def _scan(folder: Path, prefix: str, only: list[str] | None) -> list[Item]:
    """Items directly inside ``folder`` (and its stories/ subfolder)."""
    items: list[Item] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if _is_hidden(entry):
            continue
        suffix = entry.suffix.lower()
        name = f"{prefix}{entry.name}"
        if entry.is_dir() and entry.name.startswith(ACCOUNT_PREFIX) and not prefix:
            # queue/@name/: posts for that one account only
            items += _scan(entry, f"{entry.name}/", [entry.name[len(ACCOUNT_PREFIX):]])
            continue
        if entry.is_dir() and entry.name.lower() == STORIES_DIR:
            found = [
                _single(file, "story", f"{name}/{file.name}")
                for file in _visible_files(entry)
                if file.suffix.lower() in MEDIA_SUFFIXES
            ]
        elif entry.is_dir():
            media = [p for p in _visible_files(entry) if p.suffix.lower() in MEDIA_SUFFIXES]
            if len(media) == 1:
                kind = "reel" if is_video(media[0]) else "image"
            else:
                kind = "carousel"
            item = Item(name=name, kind=kind, media=media, paths=[entry])
            if not media:
                item.problems.append("folder contains no images or videos")
            elif len(media) > MAX_CAROUSEL:
                item.problems.append(
                    f"{len(media)} files; a carousel allows at most {MAX_CAROUSEL}"
                )
            found = [item]
        elif suffix in IMAGE_SUFFIXES:
            found = [_single(entry, "image", name)]
        elif suffix in VIDEO_SUFFIXES:
            found = [_single(entry, "reel", name)]
        else:
            found = []
        for item in found:
            item.accounts = only
        items += found
    return items


def load_queue(queue_dir: Path, config: dict) -> list[Item]:
    """All queued items in filename order, each with its validation results."""
    if not queue_dir.is_dir():
        return []
    items = _scan(queue_dir, "", None)
    known = set(config.get("accounts") or {})

    items.sort(key=lambda i: i.name.rsplit("/", 1)[-1].lower())
    for item in items:
        _read_caption(item, config["caption"]["default"])
        item.not_before, bad_date = _parse_date(item.name.rsplit("/", 1)[-1])
        if bad_date:
            item.problems.append(bad_date)
        for path in item.media[:MAX_CAROUSEL]:
            _check_media(item, path)
        _check_caption(item, config)
        unknown = [a for a in item.accounts or [] if a not in known]
        if unknown:
            item.problems.append(
                f"account '{unknown[0]}' is not defined under [accounts] in autoupload.toml"
            )
    return items


def in_posting_order(items: list[Item], now_local: datetime) -> list[Item]:
    """Items that may go out now: due dated items first, then the rest by name."""
    now = now_local.replace(tzinfo=None)
    dated = sorted(
        (i for i in items if i.not_before and i.not_before <= now),
        key=lambda i: i.not_before,
    )
    return dated + [i for i in items if not i.not_before]


def posted_count(root: Path) -> int:
    log = root / "posted" / "log.jsonl"
    if not log.is_file():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())


def read_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def archive(item: Item, dest_dir: Path, record: dict) -> None:
    """Move an item out of the queue and append ``record`` to dest_dir/log.jsonl."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    clash = any((dest_dir / p.name).exists() for p in item.paths)
    for path in item.paths:
        name = f"{stamp}-{path.name}" if clash else path.name
        shutil.move(str(path), str(dest_dir / name))
    entry = {
        "item": item.name,
        "kind": item.kind,
        "at": now.isoformat(timespec="seconds"),
        **record,
    }
    with (dest_dir / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
