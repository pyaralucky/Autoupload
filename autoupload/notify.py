"""Tell the owner when something needs attention.

Alerts become GitHub issues (labelled ``autoupload``) and, if configured,
Telegram messages. An alert with the same title is not repeated while its
issue is open and was updated within ``notify.repeat_hours``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

LABEL = "autoupload"
GITHUB_API = "https://api.github.com"


def _github(env: dict, session, method: str, path: str, **kwargs):
    resp = session.request(
        method,
        f"{GITHUB_API}/repos/{env['GITHUB_REPOSITORY']}/{path}",
        headers={
            "Authorization": f"Bearer {env['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


def _github_issue(env: dict, session, title: str, body: str, repeat_hours: float) -> str:
    issues = _github(
        env, session, "GET", "issues", params={"state": "open", "labels": LABEL, "per_page": 100}
    )
    existing = next((i for i in issues if i.get("title") == title), None)
    if existing is None:
        _github(
            env, session, "POST", "issues", json={"title": title, "body": body, "labels": [LABEL]}
        )
        return "opened issue"
    updated = datetime.fromisoformat(existing["updated_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - updated < timedelta(hours=repeat_hours):
        return "already reported"
    _github(env, session, "POST", f"issues/{existing['number']}/comments", json={"body": body})
    return "commented on issue"


def _telegram(env: dict, session, title: str, body: str) -> None:
    resp = session.request(
        "POST",
        f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data={"chat_id": env["TELEGRAM_CHAT_ID"], "text": f"{title}\n\n{body}"[:4000]},
        timeout=30,
    )
    resp.raise_for_status()


def notify(env: dict, config: dict, session, title: str, body: str) -> None:
    """Best effort: a failed alert is logged, never raised."""
    print(f"NOTICE: {title}")
    sent = None
    if env.get("GITHUB_TOKEN") and env.get("GITHUB_REPOSITORY"):
        try:
            sent = _github_issue(env, session, title, body, config["notify"]["repeat_hours"])
            print(f"  ({sent})")
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"  (could not file a GitHub issue: {exc})")
    if sent != "already reported" and env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        try:
            _telegram(env, session, title, body)
        except requests.RequestException as exc:
            # str(exc) can contain the bot token in the URL; don't print it.
            print(f"  (could not send the Telegram message: {type(exc).__name__})")
