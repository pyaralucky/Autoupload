"""Engagement report for recently published posts."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .instagram import InstagramClient, InstagramError
from .queue import read_log
from .schedule import WEEKDAYS, timezone_of

METRICS = ["reach", "views", "likes", "comments", "saved", "shares"]


def _fetch(client: InstagramClient, media_id: str) -> dict:
    try:
        return client.media_insights(media_id, METRICS)
    except InstagramError:
        pass
    # Some media types reject part of the metric list; ask one at a time.
    stats = {}
    for metric in METRICS:
        try:
            stats.update(client.media_insights(media_id, [metric]))
        except InstagramError:
            continue
    if "likes" not in stats or "comments" not in stats:
        try:
            counts = client.media_counts(media_id)
            stats.setdefault("likes", counts.get("like_count"))
            stats.setdefault("comments", counts.get("comments_count"))
        except InstagramError:
            pass
    return stats


def collect(client: InstagramClient, root: Path, config: dict, now: datetime) -> list[dict]:
    since = now - timedelta(days=config["insights"]["days"])
    rows = []
    for entry in read_log(root / "posted" / "log.jsonl"):
        media_id = entry.get("media_id")
        if not media_id or entry.get("kind") == "story":
            continue  # story metrics disappear after 24 hours
        try:
            at = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            continue
        if at < since:
            continue
        rows.append({**entry, "posted": at, "stats": _fetch(client, media_id)})
    return rows


def _cell(value) -> str:
    return "–" if value is None else f"{value:,}" if isinstance(value, int) else str(value)


def _best(rows: list[dict], key) -> list[tuple[str, float, int]]:
    groups = defaultdict(list)
    for row in rows:
        reach = row["stats"].get("reach")
        if isinstance(reach, (int, float)):
            groups[key(row)].append(reach)
    ranked = [(label, sum(v) / len(v), len(v)) for label, v in groups.items()]
    return sorted(ranked, key=lambda r: -r[1])


def render(rows: list[dict], config: dict, now: datetime) -> str:
    tz = timezone_of(config)
    days = config["insights"]["days"]
    out = [
        "# Instagram insights",
        "",
        f"Posts from the last {days} days, measured {now.astimezone(tz):%d %b %Y %H:%M} "
        f"({config['schedule']['timezone']}).",
        "",
    ]
    if not rows:
        out.append("No posts were published through Autoupload in this period.")
        return "\n".join(out) + "\n"

    for row in rows:
        row["local"] = row["posted"].astimezone(tz)
    ordered = sorted(rows, key=lambda r: r["posted"], reverse=True)

    def total(metric):
        values = [r["stats"].get(metric) for r in rows]
        values = [v for v in values if isinstance(v, (int, float))]
        return sum(values) if values else None

    out += [
        f"**{len(rows)} posts** · reach {_cell(total('reach'))} · "
        f"likes {_cell(total('likes'))} · comments {_cell(total('comments'))} · "
        f"saves {_cell(total('saved'))} · shares {_cell(total('shares'))}",
        "",
        "| Posted | Post | Type | Reach | Views | Likes | Comments | Saves | Shares |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in ordered:
        s = row["stats"]
        name = row.get("item", "?").replace("|", "\\|")
        link = f"[{name}]({row['permalink']})" if row.get("permalink") else name
        out.append(
            f"| {row['local']:%d %b %H:%M} | {link} | {row.get('kind', '')} | "
            + " | ".join(_cell(s.get(m)) for m in METRICS)
            + " |"
        )

    by_hour = _best(rows, lambda r: f"{r['local']:%H}:00")
    by_day = _best(rows, lambda r: WEEKDAYS[r["local"].weekday()].capitalize())
    if by_hour:
        out += ["", "## When posts reach the most people", ""]
        if len(rows) < 10:
            out += [f"Based on only {len(rows)} posts; treat this as a hint, not a finding.", ""]
        out += ["| Hour posted | Average reach | Posts |", "| --- | ---: | ---: |"]
        out += [f"| {label} | {avg:,.0f} | {n} |" for label, avg, n in by_hour]
        out += ["", "| Weekday | Average reach | Posts |", "| --- | ---: | ---: |"]
        out += [f"| {label} | {avg:,.0f} | {n} |" for label, avg, n in by_day]
    return "\n".join(out) + "\n"


def write_report(client: InstagramClient, root: Path, config: dict) -> Path:
    now = datetime.now(timezone.utc)
    rows = collect(client, root, config, now)
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / "insights.md"
    path.write_text(render(rows, config, now), encoding="utf-8")
    snapshot = {
        "measured": now.isoformat(timespec="seconds"),
        "posts": [
            {"item": r.get("item"), "media_id": r.get("media_id"), "at": r["at"], **r["stats"]}
            for r in rows
        ],
    }
    with (reports / "insights-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return path
