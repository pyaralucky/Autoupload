"""Client for the YouTube Data API v3: resumable video upload and playlists.

Authentication is OAuth with a long-lived *refresh token* (created once with
``python -m autoupload youtube-auth``); every run trades it for a short-lived
access token, so nothing has to be renewed by hand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # playlists and the credentials check
)

CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAG_CHARS = 450  # YouTube allows 500 including separators and quotes
PRIVACY = ("private", "unlisted", "public")

# Reasons from the API's error body, grouped by what the caller should do.
_LATER = {
    "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
    "userRateLimitExceeded", "uploadLimitExceeded", "uploadRateLimitExceeded",
}
_AUTH = {
    "authError", "unauthorized", "youtubeSignupRequired", "insufficientPermissions",
    "forbidden", "accountSuspended", "accessNotConfigured",
}


class YouTubeError(Exception):
    """A request failed in a way that may work on a later attempt."""


class AuthError(YouTubeError):
    """The credentials are missing, revoked or lack a permission."""


class RetryLater(YouTubeError):
    """Out of API quota or over the channel's upload limit for now."""


class VideoRejected(YouTubeError):
    """YouTube will never accept this upload as it is."""


@dataclass
class Video:
    """What to upload."""

    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = "private"
    publish_at: str = ""  # RFC 3339 UTC; the video is private until then
    category: str = "22"
    made_for_kids: bool = False
    notify_subscribers: bool = True
    playlist: str = ""


def clean_title(text: str) -> str:
    text = " ".join(text.replace("<", "").replace(">", "").split())
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1].rstrip() + "…"


def clean_description(text: str) -> str:
    text = text.replace("<", "").replace(">", "").strip()
    raw = text.encode("utf-8")
    if len(raw) > MAX_DESCRIPTION_BYTES:
        text = raw[:MAX_DESCRIPTION_BYTES].decode("utf-8", "ignore").rstrip()
    return text


def clean_tags(tags: list[str]) -> list[str]:
    """De-duplicated tags that fit YouTube's combined length limit."""
    out, seen, used = [], set(), 0
    for tag in tags:
        tag = " ".join(tag.replace("<", "").replace(">", "").replace(",", " ").split())
        if not tag or tag.lower() in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if used + cost > MAX_TAG_CHARS:
            break
        seen.add(tag.lower())
        used += cost
        out.append(tag)
    return out


def _problem(resp) -> tuple[str, str]:
    """(reason, message) from an API error response."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, str):  # OAuth endpoint: {"error": "...", "error_description": "..."}
        return error, body.get("error_description") or error
    if isinstance(error, dict):
        first = (error.get("errors") or [{}])[0]
        return first.get("reason") or "", error.get("message") or first.get("message") or ""
    return "", (getattr(resp, "text", "") or "")[:200]


def _raise(resp, what: str) -> None:
    reason, message = _problem(resp)
    detail = f"{what} failed (HTTP {resp.status_code}"
    detail += f", {reason}): {message}" if reason else f"): {message}"
    if reason in _LATER or resp.status_code == 429:
        raise RetryLater(detail)
    if resp.status_code == 401 or reason in _AUTH:
        raise AuthError(detail)
    if 400 <= resp.status_code < 500:
        raise VideoRejected(detail)
    raise YouTubeError(detail)


class YouTubeClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        chunk_size: int = CHUNK,
        retries: int = 5,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._chunk = chunk_size
        self._retries = retries
        self._token = ""
        self._token_until = 0.0

    # -- plumbing -----------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_until:
            return self._token
        try:
            resp = self._session.request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise YouTubeError(f"Google sign-in: network error ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            reason, message = _problem(resp)
            if reason in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise AuthError(
                    f"Google rejected the YouTube credentials ({reason}: {message}). "
                    "The refresh token was revoked or has expired; tokens from an OAuth "
                    "app left in 'Testing' expire after 7 days. Publish the app, run "
                    "`python -m autoupload youtube-auth` again and update YT_REFRESH_TOKEN."
                )
            raise YouTubeError(f"Google sign-in failed (HTTP {resp.status_code}): {message}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_until = self._clock() + max(60, int(body.get("expires_in", 3600)) - 120)
        return self._token

    def _headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}", **(extra or {})}

    def _call(self, method: str, path: str, what: str, **kwargs) -> dict:
        try:
            resp = self._session.request(
                method, f"{API}/{path}", headers=self._headers(), timeout=60, **kwargs
            )
        except requests.RequestException as exc:
            raise YouTubeError(f"{what}: network error ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            _raise(resp, what)
        return resp.json()

    # -- API ----------------------------------------------------------------

    def channel(self) -> dict:
        """The channel uploads go to: ``{"id": ..., "title": ...}``."""
        body = self._call(
            "GET", "channels", "channel lookup", params={"part": "snippet", "mine": "true"}
        )
        items = body.get("items") or []
        if not items:
            raise AuthError(
                "This Google account has no YouTube channel. Create one at "
                "youtube.com, then try again."
            )
        return {"id": items[0]["id"], "title": items[0].get("snippet", {}).get("title", "")}

    def add_to_playlist(self, playlist: str, video_id: str) -> None:
        self._call(
            "POST",
            "playlistItems",
            "adding to the playlist",
            params={"part": "snippet"},
            json={
                "snippet": {
                    "playlistId": playlist,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )

    def upload(self, path: Path, video: Video, content_type: str = "video/*") -> dict:
        """Upload ``path``; returns the created video resource (``id``, ``status``...)."""
        size = path.stat().st_size
        if size == 0:
            raise VideoRejected("the file is empty")
        status: dict = {
            "privacyStatus": video.privacy,
            "selfDeclaredMadeForKids": video.made_for_kids,
        }
        if video.publish_at:
            status["privacyStatus"] = "private"  # required for a scheduled release
            status["publishAt"] = video.publish_at
        body = {
            "snippet": {
                "title": clean_title(video.title),
                "description": clean_description(video.description),
                "tags": clean_tags(video.tags),
                "categoryId": str(video.category),
            },
            "status": status,
        }
        try:
            resp = self._session.request(
                "POST",
                UPLOAD_URL,
                params={
                    "uploadType": "resumable",
                    "part": "snippet,status",
                    "notifySubscribers": "true" if video.notify_subscribers else "false",
                },
                headers=self._headers(
                    {"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": content_type}
                ),
                json=body,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise YouTubeError(f"starting the upload: network error ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            _raise(resp, "starting the upload")
        location = resp.headers.get("Location")
        if not location:
            raise YouTubeError("starting the upload: YouTube returned no upload address")
        return self._send(location, path, size, content_type)

    def _send(self, location: str, path: Path, size: int, content_type: str) -> dict:
        offset, failures = 0, 0
        with path.open("rb") as handle:
            while True:
                handle.seek(offset)
                chunk = handle.read(self._chunk)
                last = offset + len(chunk) - 1
                resp = self._put(
                    location, chunk,
                    {"Content-Type": content_type, "Content-Range": f"bytes {offset}-{last}/{size}"},
                )
                if resp is None or resp.status_code >= 500:
                    failures += 1
                    if failures > self._retries:
                        raise YouTubeError("the upload kept failing; it will be retried")
                    self._sleep(min(60, 2**failures))
                    # Ask how much arrived before sending anything again.
                    resp = self._put(location, b"", {"Content-Range": f"bytes */{size}"})
                    if resp is None or resp.status_code >= 500:
                        continue
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code == 308:
                    received = resp.headers.get("Range")  # "bytes=0-12345", absent if none
                    new_offset = int(received.rsplit("-", 1)[1]) + 1 if received else 0
                    if new_offset > offset:
                        failures = 0
                    offset = new_offset
                    continue
                if resp.status_code == 404:
                    raise YouTubeError("the upload session expired; it will be retried")
                _raise(resp, "the upload")

    def _put(self, location: str, data: bytes, headers: dict):
        try:
            return self._session.request(
                "PUT", location, headers=self._headers(headers), data=data, timeout=600
            )
        except requests.RequestException:
            return None


def watch_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"
