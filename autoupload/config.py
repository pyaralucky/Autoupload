"""Settings from ``autoupload.toml``. Secrets never live here; they come from
environment variables."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "schedule": {
        "timezone": "UTC",
        "slots": ["13:17"],  # posting times, every day
        "days": {},  # per-weekday override, e.g. {"sat": ["10:00", "18:00"], "sun": []}
        "jitter_minutes": 0,
    },
    "caption": {
        "default": "",
        "footer": "",
        "hashtag_sets": [],  # rotated, one set per post
        "hashtags_in_first_comment": False,
    },
    "images": {
        "fit": "pad",  # "pad" adds borders, "crop" trims
        "pad_color": "#ffffff",
        "max_width": 1440,
        "jpeg_quality": 90,
    },
    "ai": {
        "enabled": False,
        "model": "claude-opus-5-5",
        "voice": "",
        "language": "English",
    },
    "crosspost": {"facebook": False, "threads": False},
    # Instagram accounts, e.g. {"clinic": {"user_id": "1784..."}}. Empty means
    # one account taken from the IG_USER_ID secret.
    "accounts": {},
    "brand": {
        "name": "",
        "handle": "",
        "background": "#f7f1ea",
        "text": "#1f3a3a",
        "muted": "#8a9a98",
        "accent": "#b9785f",
    },
    "notify": {"low_queue": 3, "repeat_hours": 24},
    "insights": {"days": 30},
}


class ConfigError(Exception):
    pass


def _merge(base: dict, extra: dict, where: str = "") -> dict:
    for key, value in extra.items():
        if key not in base:
            raise ConfigError(f"autoupload.toml: unknown setting '{where}{key}'")
        if isinstance(base[key], dict) and base[key]:
            if not isinstance(value, dict):
                raise ConfigError(f"autoupload.toml: '{where}{key}' must be a table")
            _merge(base[key], value, f"{where}{key}.")
        else:
            base[key] = value
    return base


def load_config(root: Path) -> dict:
    config = copy.deepcopy(DEFAULTS)
    path = root / "autoupload.toml"
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"autoupload.toml: {exc}") from exc
        _merge(config, data)
    if config["images"]["fit"] not in ("pad", "crop"):
        raise ConfigError("autoupload.toml: images.fit must be 'pad' or 'crop'")
    return config
