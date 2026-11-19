"""Telegram to YouTube: every video sent to the bot from an allowed chat is
uploaded to the YouTube channel, and the bot replies with the link.

State lives in ``posted/youtube.json`` (what is waiting, what was done) and
every upload is appended to ``posted/youtube.jsonl``.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import telegram
from .config import ConfigError
from .notify import notify
from .telegram import FileUnavailable, TelegramError
from .youtube import PRIVACY, AuthError, RetryLater, Video, VideoRejected, YouTubeError, watch_url

STATE_FILE = "posted/youtube.json"
LOG_FILE = "posted/youtube.jsonl"
QUOTA_PAUSE = 3600  # seconds to stop uploading after YouTube reports no quota
RETRY_PAUSE = 300  # seconds before retrying a failed video, times the attempt number
REMEMBER = 500  # how many finished files to remember, to never upload one twice

EXTENSIONS = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "video/webm": ".webm", "video/x-msvideo": ".avi", "video/mpeg": ".mpg",
    "video/3gpp": ".3gp", "video/x-m4v": ".m4v",
}
HELP = (
    "Send or forward a video here and it is uploaded to YouTube.\n\n"
    "Caption: the first line is the title, the rest is the description. "
    "Optional lines:\n"
    "@privacy: public | unlisted | private\n"
    "@schedule: 2026-12-25 18:00\n"
    "@tags: travel, vlog\n"
    "@playlist: PLxxxxxxxx\n"
    "@category: 22"
)


# -- state ------------------------------------------------------------------


def load_state(root: Path) -> dict:
    state = {"offset": 0, "pending": [], "done": [], "paused_until": 0}
    path = root / STATE_FILE
    if path.is_file():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise ConfigError(f"{STATE_FILE} is not valid JSON: {exc}") from exc
    return state


def save_state(root: Path, state: dict) -> None:
    path = root / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    state["done"] = state["done"][-REMEMBER:]
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _log(root: Path, record: dict) -> None:
    path = root / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# -- reading Telegram -------------------------------------------------------


def allowed_chats(env: dict) -> set[int]:
    chats = set()
    for part in re.split(r"[,\s]+", (env.get("TELEGRAM_ALLOWED_CHATS") or "").strip()):
        if not part:
            continue
        try:
            chats.add(int(part))
        except ValueError:
            raise ConfigError(
                f"TELEGRAM_ALLOWED_CHATS: '{part}' is not a chat id. Use numbers "
                "separated by commas; send /id to the bot to see a chat's id."
            ) from None
    return chats


def incoming(message: dict) -> dict | None:
    """The video in a Telegram message as a queue entry, or None."""
    if "animation" in message:  # GIFs also arrive as a video document
        return None
    media = message.get("video")
    if media is None:
        document = message.get("document") or {}
        if str(document.get("mime_type", "")).startswith("video/"):
            media = document
    if not media or not media.get("file_id"):
        return None
    return {
        "uid": media.get("file_unique_id") or media["file_id"],
        "file_id": media["file_id"],
        "chat": message["chat"]["id"],
        "message": message["message_id"],
        "size": int(media.get("file_size") or 0),
        "name": media.get("file_name") or "",
        "mime": media.get("mime_type") or "",
        "caption": message.get("caption") or "",
        "date": int(message.get("date") or 0),
        "attempts": 0,
    }


def _command(message: dict) -> str:
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split()[0].split("@")[0].lower()


def collect(updates: list[dict], state: dict, allowed: set[int], bot, reply: bool = True) -> int:
    """Queue the videos in ``updates``. Returns how many were added."""
    known = set(state["done"]) | {item["uid"] for item in state["pending"]}
    added = 0
    for update in updates:
        state["offset"] = max(state["offset"], int(update["update_id"]) + 1)
        message = update.get("message") or update.get("channel_post")
        if not message or "chat" not in message:
            continue
        chat = message["chat"]
        command = _command(message)
        if chat["id"] not in allowed:
            # Stay quiet in groups unless asked, so the bot cannot be made to spam.
            if reply and (chat.get("type") == "private" or command):
                _say(
                    bot, chat["id"],
                    f"This chat is not authorised to upload. Its id is {chat['id']}.\n"
                    "The owner can allow it by adding that number to TELEGRAM_ALLOWED_CHATS.",
                    message.get("message_id"),
                )
            continue
        if command in ("/start", "/help", "/id"):
            if reply:
                _say(bot, chat["id"], f"{HELP}\n\nThis chat's id is {chat['id']}.")
            continue
        item = incoming(message)
        if item is None:
            continue
        if item["uid"] in known:
            if reply and item["uid"] in state["done"]:
                _say(
                    bot, chat["id"],
                    "This video was already uploaded, so it is not uploaded again.",
                    item["message"],
                )
            continue
        known.add(item["uid"])
        state["pending"].append(item)
        added += 1
    return added


def _say(bot, chat_id: int, text: str, reply_to: int | None = None) -> None:
    """Replies are a courtesy: a failure to send one never stops an upload."""
    try:
        bot.send(chat_id, text, reply_to)
    except TelegramError as exc:
        print(f"  (could not reply in Telegram: {exc})")


# -- caption to video metadata ----------------------------------------------


def timezone_of(config: dict):
    name = config["schedule"]["timezone"]
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"autoupload.toml: unknown timezone '{name}'") from None


def _schedule(value: str, tz, now: datetime) -> str:
    """'2026-12-25 18:00' in the configured timezone -> RFC 3339 UTC, '' if past."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            when = datetime.strptime(value, fmt).replace(tzinfo=tz)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"@schedule: '{value}' is not a date like 2026-12-25 18:00")
    if when <= now + timedelta(minutes=5):
        return ""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(item: dict, config: dict, now: datetime) -> Video:
    """Build the YouTube metadata for a queued video from its caption.

    Raises ValueError when an ``@option`` line cannot be understood.
    """
    yt = config["youtube"]
    tz = timezone_of(config)
    options: dict[str, str] = {}
    lines = []
    for line in item["caption"].splitlines():
        match = re.match(r"\s*@(\w+)\s*:\s*(.*)$", line)
        if match:
            options[match.group(1).lower()] = match.group(2).strip()
        else:
            lines.append(line.rstrip())
    unknown = set(options) - {"privacy", "schedule", "tags", "playlist", "category"}
    if unknown:
        raise ValueError(f"unknown option @{sorted(unknown)[0]}")
    text = "\n".join(lines).strip()
    title, _, description = text.partition("\n")
    if not title.strip():
        stem = Path(item["name"]).stem.replace("_", " ").strip()
        when = datetime.fromtimestamp(item["date"], tz) if item["date"] else now.astimezone(tz)
        title = stem or yt["default_title"].format(
            date=when.strftime("%Y-%m-%d"), time=when.strftime("%H:%M")
        )
    description = description.strip()
    if yt["description_footer"]:
        description = f"{description}\n\n{yt['description_footer']}".strip()

    privacy = (options.get("privacy") or yt["privacy"]).lower()
    if privacy not in PRIVACY:
        raise ValueError(f"@privacy must be one of: {', '.join(PRIVACY)}")
    publish_at = ""
    if options.get("schedule"):
        publish_at = _schedule(options["schedule"], tz, now)
        if not publish_at:
            privacy = "public"  # the release time has already passed
    tags = [t.strip() for t in options.get("tags", "").split(",")]
    tags += re.findall(r"#(\w+)", text)
    tags += list(yt["tags"])
    return Video(
        title=title.strip(),
        description=description,
        tags=tags,
        privacy=privacy,
        publish_at=publish_at,
        category=options.get("category") or str(yt["category"]),
        made_for_kids=bool(yt["made_for_kids"]),
        notify_subscribers=bool(yt["notify_subscribers"]),
        playlist=options.get("playlist") or yt["playlist"],
    )


# -- download and upload ----------------------------------------------------


def _megabytes(size: int) -> str:
    return f"{size / 1024 / 1024:.0f} MB"


def fetch(item: dict, folder: Path, bot, env: dict) -> Path:
    """Download the video for ``item`` into ``folder``."""
    suffix = Path(item["name"]).suffix.lower() or EXTENSIONS.get(item["mime"], ".mp4")
    dest = folder / f"video{suffix}"
    size = item["size"]
    if size > telegram.MAX_SIZE:
        raise FileUnavailable(f"the video is {_megabytes(size)}; the limit is 2000 MB")
    if 0 < size <= telegram.BOT_API_LIMIT:
        try:
            return bot.download(item["file_id"], dest)
        except FileUnavailable:
            pass  # larger than reported; fall through to the large-file route
    if not telegram.can_download_large(env):
        raise FileUnavailable(
            f"the video is {_megabytes(size) if size else 'over 20 MB'} and Telegram bots "
            "can only download 20 MB. To lift this to 2 GB, add the TELEGRAM_API_ID and "
            "TELEGRAM_API_HASH secrets (see the README), then send the video again"
        )
    return telegram.download_large(env, item["chat"], item["message"], dest)


def _upload(item: dict, video: Video, bot, yt, env: dict) -> dict:
    folder = Path(tempfile.mkdtemp(prefix="autoupload-"))
    try:
        path = fetch(item, folder, bot, env)
        result = yt.upload(path, video, item["mime"] or "video/*")
        size = path.stat().st_size
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    record = {
        "video_id": result["id"],
        "url": watch_url(result["id"]),
        "title": video.title,
        "privacy": (result.get("status") or {}).get("privacyStatus") or video.privacy,
        "bytes": size,
        "chat": item["chat"],
        "message": item["message"],
    }
    if video.publish_at:
        record["publish_at"] = video.publish_at
    if video.playlist:
        try:
            yt.add_to_playlist(video.playlist, result["id"])
            record["playlist"] = video.playlist
        except YouTubeError as exc:
            record["warning"] = f"not added to the playlist: {exc}"
    return record


def _announce(record: dict, wanted: str) -> str:
    text = f"Uploaded to YouTube: {record['url']}\n{record['title']}\n"
    if record.get("publish_at"):
        text += f"Private until {record['publish_at']}, then public."
    else:
        text += f"Visibility: {record['privacy']}."
        if record["privacy"] != wanted:
            text += f" (You asked for {wanted}; YouTube changed it.)"
    if record.get("warning"):
        text += f"\nNote: {record['warning']}"
    return text


def run(
    root: Path,
    config: dict,
    env: dict,
    session,
    bot,
    yt,
    *,
    wait: int = 0,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """One pass: read new messages, then upload what is waiting.

    Returns counts: ``{"queued", "uploaded", "failed", "waiting"}``.
    """
    now = now or datetime.now(timezone.utc)
    settings = config["youtube"]
    reply = bool(settings["reply"]) and not dry_run
    allowed = allowed_chats(env)
    state = load_state(root)

    # Not acknowledged to Telegram until the next pass, after the state holding
    # them has been saved: a crashed run re-reads the same messages.
    updates = bot.get_updates(state["offset"] or None, wait)
    queued = collect(updates, state, allowed, bot, reply)
    if not allowed:
        print(
            "TELEGRAM_ALLOWED_CHATS is empty, so nothing is uploaded. Send /id to "
            "the bot, then put the chat id it replies with in that variable."
        )
    if dry_run:
        for item in state["pending"]:
            try:
                video = describe(item, config, now)
                print(f"Would upload: {video.title!r} [{video.privacy}] {_megabytes(item['size'])}")
            except ValueError as exc:
                print(f"Would reject: {exc}")
        return {"queued": queued, "uploaded": 0, "failed": 0, "waiting": len(state["pending"])}
    save_state(root, state)

    uploaded, failed = 0, []
    limit = int(settings["max_per_run"])
    stamp = now.timestamp()
    if state.get("paused_until", 0) > stamp:
        print("Waiting for YouTube's quota to reset; nothing uploaded this pass.")
        limit = 0
    for item in list(state["pending"]):
        if uploaded >= limit:
            break
        if item.get("next_try", 0) > stamp:
            continue

        def finish(problem: str = "") -> None:
            state["pending"].remove(item)
            if not problem:  # a failed video may be sent again once fixed
                state["done"].append(item["uid"])
            save_state(root, state)
            if problem:
                print(f"FAILED message {item['message']}: {problem}")
                failed.append(problem)
                if reply:
                    _say(bot, item["chat"], f"Not uploaded: {problem}.", item["message"])

        try:
            video = describe(item, config, now)
        except ValueError as exc:
            finish(f"{exc}. Fix the caption and send the video again")
            continue
        try:
            record = _upload(item, video, bot, yt, env)
        except RetryLater as exc:
            print(f"WAIT: {exc}")
            if reply and not item.get("told"):
                _say(
                    bot, item["chat"],
                    "YouTube's upload quota is used up for now. This video stays "
                    "queued and is uploaded automatically once the quota resets.",
                    item["message"],
                )
            item["told"] = True
            # The quota is per account: later videos would fail the same way.
            state["paused_until"] = int(stamp) + QUOTA_PAUSE
            save_state(root, state)
            break
        except (VideoRejected, FileUnavailable) as exc:
            finish(str(exc))
            continue
        except AuthError:
            save_state(root, state)
            raise  # nothing can be uploaded until the credentials are fixed
        except (YouTubeError, TelegramError) as exc:
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= int(settings["max_attempts"]):
                finish(f"gave up after {item['attempts']} attempts: {exc}")
            else:
                item["next_try"] = int(stamp) + RETRY_PAUSE * item["attempts"]
                print(f"RETRY message {item['message']} (attempt {item['attempts']}): {exc}")
                save_state(root, state)
            continue

        record["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _log(root, record)
        finish()
        uploaded += 1
        print(f"UPLOADED {record['url']} [{record['privacy']}] {record['title']!r}")
        if reply:
            _say(bot, item["chat"], _announce(record, video.privacy), item["message"])

    if failed:
        notify(
            env, config, session, "Autoupload: videos could not be uploaded to YouTube",
            "These Telegram videos were dropped:\n\n- " + "\n- ".join(failed),
        )
    waiting = len(state["pending"])
    print(f"{queued} new, {uploaded} uploaded, {len(failed)} failed, {waiting} waiting.")
    return {"queued": queued, "uploaded": uploaded, "failed": len(failed), "waiting": waiting}
