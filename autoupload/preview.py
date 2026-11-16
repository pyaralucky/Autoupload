"""Render the upcoming queue as ``QUEUE.md``, which GitHub shows with images."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .queue import Item, compose_caption, in_posting_order, is_video, load_queue, posted_count
from .schedule import timezone_of, upcoming_slots

COLUMNS = 3


def plan(items: list[Item], config: dict, now: datetime) -> list[tuple[Item, datetime | None]]:
    """Pair each item with the slot it is expected to go out in."""
    tz = timezone_of(config)
    remaining = list(items)
    planned: list[tuple[Item, datetime | None]] = []
    for slot in upcoming_slots(config, now, len(items) * 3 + 30):
        if not remaining:
            break
        ready = in_posting_order(remaining, slot.astimezone(tz))
        ready = [i for i in ready if not i.problems]
        if not ready:
            continue
        planned.append((ready[0], slot))
        remaining.remove(ready[0])
    planned += [(item, None) for item in remaining]
    return planned


def _thumb(item: Item, root: Path, width: int) -> str:
    first = next((p for p in item.media if not is_video(p)), None)
    if first is None:
        return "🎬" if item.media else "∅"
    rel = quote(first.resolve().relative_to(root.resolve()).as_posix())
    return f'<img src="{rel}" width="{width}" alt="{item.name}">'


def _label(item: Item) -> str:
    if item.kind == "carousel":
        return f"carousel × {len(item.media)}"
    return item.kind


def render(root: Path, config: dict, now: datetime) -> str:
    items = load_queue(root / "queue", config)
    zone = config["schedule"]["timezone"]
    out = ["# Upcoming posts", ""]
    if not items:
        return "\n".join(out + ["The queue is empty.", ""])

    planned = plan(items, config, now)
    blocked = sum(1 for i in items if i.problems)
    out += [
        f"{len(items)} queued"
        + (f", {blocked} blocked by problems" if blocked else "")
        + f". Times are estimates in {zone}.",
        "",
        "## Grid",
        "",
        "Newest at the top left, as the profile grid will look once everything is posted.",
        "",
    ]
    feed = [i for i, slot in reversed(planned) if slot and i.kind != "story"]
    if feed:
        out.append("<table>")
        for start in range(0, len(feed), COLUMNS):
            cells = "".join(f"<td>{_thumb(i, root, 220)}</td>" for i in feed[start : start + COLUMNS])
            out.append(f"<tr>{cells}</tr>")
        out += ["</table>", ""]

    out += ["## Schedule", ""]
    index = posted_count(root)
    for item, slot in planned:
        when = f"{slot:%a %d %b %Y, %H:%M}" if slot else "not scheduled"
        out += [f"### {when} — {item.name}", "", f"_{_label(item)}_", "", _thumb(item, root, 320), ""]
        caption, comment = compose_caption(item, config, index)
        if slot:
            index += 1
        if caption:
            out += ["> " + caption.replace("\n", "\n> "), ""]
        elif item.kind != "story":
            out += ["_No caption._", ""]
        if comment:
            out += ["First comment:", "", "> " + comment.replace("\n", "\n> "), ""]
        extras = []
        if item.alt:
            extras.append(f"Alt text: {' / '.join(item.alt)}")
        if item.location:
            extras.append(f"Location id: {item.location}")
        if item.not_before:
            extras.append(f"Held until {item.not_before:%d %b %Y %H:%M}")
        out += [f"- {e}" for e in extras] + ([""] if extras else [])
        for note in item.problems:
            out.append(f"- ⛔ {note}")
        for note in item.fixes:
            out.append(f"- 🔧 {note}")
        if item.problems or item.fixes:
            out.append("")
    return "\n".join(out)


def write_preview(root: Path, config: dict, now: datetime) -> Path:
    path = root / "QUEUE.md"
    path.write_text(render(root, config, now), encoding="utf-8")
    return path
