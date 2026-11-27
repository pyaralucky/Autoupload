"""Posting slots.

The workflow wakes up every hour; a post goes out when a slot from
``[schedule]`` has come due since the last one that was handled. The last
handled slot is remembered in ``posted/state.json``.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import ConfigError

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CATCH_UP_HOURS = 24
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def timezone_of(config: dict) -> ZoneInfo:
    name = config["schedule"]["timezone"]
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"autoupload.toml: unknown timezone '{name}'") from exc


def _parse(times, where: str) -> list[time]:
    out = []
    for text in times:
        match = _TIME.match(str(text))
        if not match:
            raise ConfigError(f"autoupload.toml: {where}: '{text}' is not HH:MM")
        out.append(time(int(match[1]), int(match[2])))
    return sorted(out)


def slots_on(config: dict, day: date) -> list[datetime]:
    schedule = config["schedule"]
    days = schedule["days"]
    unknown = set(days) - set(WEEKDAYS)
    if unknown:
        raise ConfigError(
            f"autoupload.toml: schedule.days: unknown day(s) {sorted(unknown)}; "
            f"use {', '.join(WEEKDAYS)}"
        )
    key = WEEKDAYS[day.weekday()]
    times = (
        _parse(days[key], f"schedule.days.{key}")
        if key in days
        else _parse(schedule["slots"], "schedule.slots")
    )
    tz = timezone_of(config)
    return [datetime.combine(day, t, tzinfo=tz) for t in times]


def latest_slot(config: dict, now: datetime) -> datetime | None:
    """Most recent slot at or before ``now`` (looking back up to two weeks)."""
    local = now.astimezone(timezone_of(config))
    for back in range(15):
        due = [s for s in slots_on(config, local.date() - timedelta(days=back)) if s <= local]
        if due:
            return due[-1]
    return None


def upcoming_slots(config: dict, now: datetime, count: int) -> list[datetime]:
    """The next ``count`` slots strictly after ``now``."""
    local = now.astimezone(timezone_of(config))
    found: list[datetime] = []
    for ahead in range(370):
        for slot in slots_on(config, local.date() + timedelta(days=ahead)):
            if slot > local:
                found.append(slot)
                if len(found) == count:
                    return found
    return found


def _state_path(root: Path) -> Path:
    return root / "posted" / "state.json"


def load_state(root: Path) -> dict:
    path = _state_path(root)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save_state(root: Path, state: dict) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def due_slot(config: dict, root: Path, now: datetime) -> datetime | None:
    """The slot to post for now, or None. Starts the clock on first use."""
    state = load_state(root)
    last = state.get("last_slot")
    if last is None:
        # First run: nothing is owed for slots that passed before installation.
        state["last_slot"] = now.isoformat(timespec="seconds")
        save_state(root, state)
        return None
    # GitHub often runs scheduled workflows hours late, so several slots can
    # pass between runs. Take the oldest one still owed (within the last day)
    # so that each run posts once and none is silently dropped.
    last_done = datetime.fromisoformat(last)
    local = now.astimezone(timezone_of(config))
    floor = max(last_done, now - timedelta(hours=CATCH_UP_HOURS))
    for back in (1, 0):
        for slot in slots_on(config, local.date() - timedelta(days=back)):
            if floor < slot <= local:
                return slot
    return None


def mark_done(root: Path, slot: datetime) -> None:
    state = load_state(root)
    state["last_slot"] = slot.isoformat(timespec="seconds")
    save_state(root, state)
