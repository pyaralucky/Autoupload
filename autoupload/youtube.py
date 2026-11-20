"""Upload Reels to YouTube, where a short vertical video becomes a Short.

Unlike Instagram, YouTube does not fetch a URL: the file itself is sent, using
the resumable upload protocol so that a dropped connection continues from
where it stopped. Access comes from an OAuth refresh token (see
``python -m autoupload youtube-auth``), never from the account password.
"""

from __future__ import annotations

import http.server
import re
import secrets
import struct
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .instagram import InstagramError, Post

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # lets `check` name the channel
)
CREDENTIALS = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS_CHARS = 500
SHORT_MAX_SECONDS = 180
PRIVACY = ("public", "unlisted", "private")
UPLOAD_ATTEMPTS = 5


class YouTubeError(InstagramError):
    """YouTube or Google sign-in refused a request."""


# -- titles and descriptions --------------------------------------------------


def _clean(text: str) -> str:
    # YouTube rejects angle brackets in titles and descriptions.
    return text.replace("<", "‹").replace(">", "›")


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_title(title: str, caption: str, fallback: str) -> str:
    """An explicit title, else the caption's first line without hashtags, else
    the file name."""
    lines = [" ".join(w for w in line.split() if not w.startswith("#")) for line in caption.splitlines()]
    for candidate in (title.strip(), *lines):
        if candidate:
            return _shorten(_clean(candidate), MAX_TITLE_CHARS)
    stem = Path(fallback).stem
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}(?:[-_T]\d{4})?", "", stem)  # hold date
    stem = re.sub(r"^\d+(?=[-_ .])", "", stem)  # ordering number
    stem = re.sub(r"[-_.]+", " ", stem).strip()
    return _shorten(_clean(stem or "Untitled"), MAX_TITLE_CHARS)


def make_tags(tags: list[str]) -> list[str]:
    """De-duplicated tags that fit YouTube's overall length limit."""
    out: list[str] = []
    used = 0
    for tag in tags:
        tag = _clean(str(tag)).strip().lstrip("#").strip()
        if not tag or tag.lower() in (t.lower() for t in out):
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if used + cost > MAX_TAGS_CHARS:
            break
        out.append(tag)
        used += cost
    return out


# -- reading a video's length and shape ---------------------------------------


def _boxes(fh, end: int):
    """Yield (type, payload start, payload end) for each MP4 box up to ``end``."""
    pos = fh.tell()
    while pos + 8 <= end:
        fh.seek(pos)
        size, kind = struct.unpack(">I4s", fh.read(8))
        header = 8
        if size == 1:
            (size,) = struct.unpack(">Q", fh.read(8))
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, min(pos + size, end)
        pos += size


def probe_video(path: Path) -> tuple[float, int, int] | None:
    """(seconds, width, height) as displayed, or None if it cannot be read.

    Reads only the MP4/MOV index, so it is fast and needs no ffmpeg.
    """
    try:
        with path.open("rb") as fh:
            total = path.stat().st_size
            moov = next(((s, e) for k, s, e in _boxes(fh, total) if k == b"moov"), None)
            if moov is None:
                return None
            duration = 0.0
            width = height = 0
            fh.seek(moov[0])
            for kind, start, end in list(_boxes(fh, moov[1])):
                if kind == b"mvhd":
                    fh.seek(start)
                    version = fh.read(4)[0]
                    if version == 1:
                        fh.seek(16, 1)
                        scale, length = struct.unpack(">IQ", fh.read(12))
                    else:
                        fh.seek(8, 1)
                        scale, length = struct.unpack(">II", fh.read(8))
                    duration = length / scale if scale else 0.0
                elif kind == b"trak" and not width:
                    fh.seek(start)
                    tkhd = next(((s, e) for k, s, e in _boxes(fh, end) if k == b"tkhd"), None)
                    if tkhd is None:
                        continue
                    fh.seek(tkhd[0])
                    version = fh.read(4)[0]
                    fh.seek(32 if version == 1 else 20, 1)
                    fh.seek(16, 1)  # reserved, layer, group, volume
                    matrix = struct.unpack(">9i", fh.read(36))
                    w, h = (v / 65536 for v in struct.unpack(">II", fh.read(8)))
                    if w and h:  # audio tracks have no size
                        # A phone clip is stored sideways with a rotation matrix.
                        rotated = matrix[0] == 0 and matrix[4] == 0 and matrix[1] != 0
                        width, height = (round(h), round(w)) if rotated else (round(w), round(h))
            if not (duration and width and height):
                return None
            return duration, width, height
    except (OSError, struct.error, IndexError):
        return None


def short_note(path: Path) -> str | None:
    """Why this video would be a regular YouTube video rather than a Short."""
    info = probe_video(path)
    if info is None:
        return None
    seconds, width, height = info
    reasons = []
    if seconds > SHORT_MAX_SECONDS:
        reasons.append(f"it is {seconds / 60:.1f} min long (Shorts are up to 3 min)")
    if width > height:
        reasons.append(f"it is landscape, {width}x{height} (Shorts are vertical or square)")
    if not reasons:
        return None
    return "YouTube will treat this as a regular video, not a Short: " + " and ".join(reasons)


# -- Google sign-in -----------------------------------------------------------


def _token_request(session, **form: str) -> dict:
    try:
        resp = session.request("POST", TOKEN_URL, data=form, timeout=60)
    except requests.RequestException as exc:
        # Report the type only: the request carries the client secret.
        raise YouTubeError(f"Google sign-in: network error ({type(exc).__name__})") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or "access_token" not in body:
        code = body.get("error") if isinstance(body.get("error"), str) else "?"
        detail = body.get("error_description") or "no detail"
        hint = ""
        if code == "invalid_grant":
            hint = (
                " The refresh token has expired or was revoked: run "
                "`python -m autoupload youtube-auth` and update the YT_REFRESH_TOKEN "
                "secret. Tokens last only 7 days while the Google app's publishing "
                "status is 'Testing'; set it to 'In production'."
            )
        elif code == "invalid_client":
            hint = " Check YT_CLIENT_ID and YT_CLIENT_SECRET."
        raise YouTubeError(
            f"Google sign-in failed (HTTP {resp.status_code}, {code}): {detail}.{hint}"
        )
    return body


def consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # ...every time, not only on the first approval
            "state": state,
        }
    )


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str, session) -> str:
    """Trade the one-time code from the consent page for a refresh token."""
    body = _token_request(
        session,
        grant_type="authorization_code",
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    if not body.get("refresh_token"):
        raise YouTubeError(
            "Google did not return a refresh token. Remove this app under "
            "https://myaccount.google.com/permissions and try again."
        )
    return body["refresh_token"]


def authorize(
    client_id: str,
    client_secret: str,
    session,
    *,
    port: int = 0,
    announce: Callable[[str], None] = print,
    timeout: float = 600,
) -> str:
    """Run the browser consent flow on this computer. Returns a refresh token."""
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_error(400, "Unexpected request")
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            message = "Done. You can close this tab and return to the terminal."
            if not result["code"]:
                message = "Access was not granted. You can close this tab."
            page = message.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):  # the request line contains the code
            pass

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        announce(consent_url(client_id, redirect_uri, state))
        server.timeout = 1
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise YouTubeError("Timed out waiting for the Google consent page.")
    if not result["code"]:
        raise YouTubeError(f"Google sign-in was not completed ({result['error'] or 'no code'}).")
    return exchange_code(client_id, client_secret, result["code"], redirect_uri, session)


# -- the client ---------------------------------------------------------------


def _api_error(resp, what: str) -> YouTubeError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {}
    first = (err.get("errors") or [{}])[0]
    reason = first.get("reason") if isinstance(first, dict) else None
    detail = re.sub(r"<[^>]+>", "", err.get("message") or resp.text[:300] or "no detail")
    hint = ""
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        hint = " The Google project's daily YouTube API quota is used up; it resets at midnight Pacific time."
    elif reason == "uploadLimitExceeded":
        hint = " The channel has reached YouTube's limit on uploads per day."
    elif reason == "youtubeSignupRequired":
        hint = " The Google account that granted access has no YouTube channel."
    elif reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
        hint = " Run `python -m autoupload youtube-auth` again and tick every permission."
    tag = f"HTTP {resp.status_code}" + (f", {reason}" if reason else "")
    return YouTubeError(f"YouTube {what} failed ({tag}): {detail.rstrip('. ')}.{hint}")


class YouTube:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        settings: dict,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._settings = settings
        self._session = session or requests.Session()
        self._sleep = sleep
        self._access_token: str | None = None

    def accepts(self, post: Post) -> bool:
        return post.kind == "reel" and bool(post.files)

    def _auth(self) -> dict:
        if self._access_token is None:
            self._access_token = _token_request(
                self._session,
                grant_type="refresh_token",
                refresh_token=self._refresh_token,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )["access_token"]
        return {"Authorization": f"Bearer {self._access_token}"}

    def channel(self) -> dict | None:
        """The channel uploads go to; doubles as a credentials check.

        None means the token works but was granted without permission to read
        the channel's name.
        """
        headers = self._auth()
        try:
            resp = self._session.request(
                "GET",
                f"{API_URL}/channels",
                headers=headers,
                params={"part": "snippet", "mine": "true"},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise YouTubeError(f"YouTube: network error: {exc}") from exc
        if resp.status_code == 403 and "insufficient" in resp.text.lower():
            return None
        if resp.status_code >= 400:
            raise _api_error(resp, "channel lookup")
        items = resp.json().get("items") or []
        if not items:
            raise YouTubeError("The Google account that granted access has no YouTube channel.")
        return {"id": items[0].get("id"), "title": (items[0].get("snippet") or {}).get("title")}

    def metadata(self, post: Post) -> dict:
        cfg = self._settings
        status = {
            "privacyStatus": cfg["privacy"],
            "selfDeclaredMadeForKids": bool(cfg["made_for_kids"]),
        }
        if cfg["altered_content"]:
            status["containsSyntheticMedia"] = True
        return {
            "snippet": {
                "title": make_title(post.title, post.caption, post.files[0].name),
                "description": _shorten(_clean(post.caption), MAX_DESCRIPTION_CHARS),
                "tags": make_tags([*post.tags, *cfg["tags"]]),
                "categoryId": str(cfg["category"]),
            },
            "status": status,
        }

    def publish(self, post: Post) -> str:
        """Upload the post's video. Returns a link to it."""
        path = post.files[0]
        size = path.stat().st_size
        mime = "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"
        try:
            resp = self._session.request(
                "POST",
                UPLOAD_URL,
                headers={
                    **self._auth(),
                    "X-Upload-Content-Type": mime,
                    "X-Upload-Content-Length": str(size),
                },
                params={
                    "uploadType": "resumable",
                    "part": "snippet,status",
                    "notifySubscribers": "true" if self._settings["notify_subscribers"] else "false",
                },
                json=self.metadata(post),
                timeout=60,
            )
        except requests.RequestException as exc:
            raise YouTubeError(f"YouTube upload: network error: {exc}") from exc
        upload_url = resp.headers.get("Location")
        if resp.status_code >= 400 or not upload_url:
            raise _api_error(resp, "upload")

        video = self._send(upload_url, path, size, mime)
        video_id = video.get("id")
        if not video_id:
            raise YouTubeError("YouTube accepted the upload but returned no video id")
        info = probe_video(path)
        is_short = info is not None and info[0] <= SHORT_MAX_SECONDS and info[1] <= info[2]
        return f"https://www.youtube.com/{'shorts/' if is_short else 'watch?v='}{video_id}"

    def _send(self, upload_url: str, path: Path, size: int, mime: str) -> dict:
        offset = 0
        problem = "no response"
        for attempt in range(UPLOAD_ATTEMPTS):
            if attempt:
                self._sleep(min(2**attempt, 30))
                # Ask how much arrived, then carry on from there.
                try:
                    resp = self._session.request(
                        "PUT",
                        upload_url,
                        headers={**self._auth(), "Content-Range": f"bytes */{size}"},
                        timeout=60,
                    )
                except requests.RequestException as exc:
                    problem = f"network error: {type(exc).__name__}"
                    continue
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code != 308:
                    if resp.status_code >= 500:
                        problem = f"HTTP {resp.status_code}"
                        continue
                    raise _api_error(resp, "upload")
                match = re.search(r"-(\d+)$", resp.headers.get("Range") or "")
                offset = int(match[1]) + 1 if match else 0
            headers = {**self._auth(), "Content-Type": mime}
            if offset:
                headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    resp = self._session.request(
                        "PUT", upload_url, headers=headers, data=fh, timeout=(30, 900)
                    )
            except requests.RequestException as exc:
                problem = f"network error: {type(exc).__name__}"
                continue
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code >= 500 or resp.status_code == 308:
                problem = f"HTTP {resp.status_code}"
                continue
            raise _api_error(resp, "upload")
        raise YouTubeError(f"YouTube upload did not finish after {UPLOAD_ATTEMPTS} tries ({problem})")
