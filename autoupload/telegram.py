"""Telegram side of the YouTube uploader: read what was sent to the bot,
download the video, reply with the result.

The Bot API only serves files up to 20 MB. Larger videos (up to 2 GB) are
fetched over Telegram's native protocol with the same bot, which additionally
needs ``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH`` and the ``telethon`` package.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import requests

API_BASE = "https://api.telegram.org"
BOT_API_LIMIT = 20 * 1024 * 1024
MAX_SIZE = 2000 * 1024 * 1024
CONNECT_TIMEOUT = 90  # seconds to log in before giving up on a large download


class TelegramError(Exception):
    """A Telegram request failed in a way that may work on a later attempt."""


class FileUnavailable(TelegramError):
    """This file can never be downloaded with the current setup."""


class TelegramBot:
    def __init__(self, token: str, *, session=None, api_base: str = "") -> None:
        self._token = token
        self._session = session or requests.Session()
        self._base = (api_base or API_BASE).rstrip("/")

    def _call(self, method: str, http_timeout: float = 30, **params) -> object:
        # The URL carries the bot token, so errors never include it or str(exc).
        try:
            resp = self._session.request(
                "POST", f"{self._base}/bot{self._token}/{method}", json=params, timeout=http_timeout
            )
            body = resp.json()
        except requests.RequestException as exc:
            raise TelegramError(f"Telegram {method}: network error ({type(exc).__name__})") from None
        except ValueError:
            body = {}
        if not isinstance(body, dict) or not body.get("ok"):
            detail = body.get("description") if isinstance(body, dict) else None
            code = body.get("error_code") if isinstance(body, dict) else None
            message = f"Telegram {method} failed (HTTP {code or resp.status_code}): {detail or 'no detail'}"
            if code == 409:
                message += (
                    " - another program is reading this bot's messages, or a webhook is "
                    "set. Use a bot dedicated to uploads, or remove the webhook."
                )
            if method == "getFile" and "too big" in (detail or "").lower():
                raise FileUnavailable(message)
            raise TelegramError(message)
        return body["result"]

    def me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: int | None = None, wait: int = 0) -> list[dict]:
        params = {"timeout": wait, "allowed_updates": ["message", "channel_post"], "limit": 100}
        if offset:
            params["offset"] = offset
        return self._call("getUpdates", http_timeout=wait + 30, **params)

    def send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if reply_to:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = True
        self._call("sendMessage", **params)

    def download(self, file_id: str, dest: Path) -> Path:
        """Bot API download; only works for files up to 20 MB."""
        info = self._call("getFile", file_id=file_id)
        remote = info.get("file_path")
        if not remote:
            raise FileUnavailable("Telegram did not return a download path for this file")
        try:
            resp = self._session.request(
                "GET", f"{self._base}/file/bot{self._token}/{remote}", stream=True, timeout=120
            )
            if resp.status_code != 200:
                raise TelegramError(f"Telegram file download failed (HTTP {resp.status_code})")
            with dest.open("wb") as out:
                for block in resp.iter_content(1024 * 1024):
                    out.write(block)
        except requests.RequestException as exc:
            raise TelegramError(f"Telegram file download: network error ({type(exc).__name__})") from None
        return dest


def can_download_large(env: dict) -> bool:
    return bool(env.get("TELEGRAM_API_ID") and env.get("TELEGRAM_API_HASH"))


def download_large(env: dict, chat_id: int, message_id: int, dest: Path) -> Path:
    """Fetch the video attached to a message over MTProto (files up to 2 GB)."""
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise TelegramError(
            "Videos over 20 MB need the 'telethon' package: pip install telethon"
        ) from None
    try:
        api_id = int(env["TELEGRAM_API_ID"])
    except (KeyError, ValueError):
        raise FileUnavailable("TELEGRAM_API_ID must be the numeric id from my.telegram.org") from None

    async def fetch() -> str | None:
        # An in-memory session: the login key is never written to disk.
        client = TelegramClient(
            StringSession(), api_id, env["TELEGRAM_API_HASH"], connection_retries=2, timeout=20
        )
        try:
            # Without a limit an unreachable Telegram would hang the whole run.
            await asyncio.wait_for(client.start(bot_token=env["TELEGRAM_BOT_TOKEN"]), CONNECT_TIMEOUT)
            message = await client.get_messages(chat_id, ids=message_id)
            if message is None or not message.media:
                raise FileUnavailable("the message was deleted before it could be downloaded")
            return await client.download_media(message, file=str(dest))
        finally:
            await client.disconnect()

    try:
        saved = asyncio.run(fetch())
    except TelegramError:
        raise
    except Exception as exc:  # Telethon raises many RPC error types
        raise TelegramError(f"Telegram download failed ({type(exc).__name__})") from None
    if not saved:
        raise TelegramError("Telegram download produced no file")
    return Path(saved)
