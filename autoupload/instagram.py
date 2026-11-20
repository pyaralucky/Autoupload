"""Clients for the Instagram content publishing API (and the shared plumbing
used by the Facebook and Threads cross-posters).

Publishing is a two-step flow: create a media *container* that points at a
publicly reachable file URL, wait until it has been ingested, then publish
the container. Carousels are a parent container over child containers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

DEFAULT_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v24.0"

IMAGE_TIMEOUT = 120.0
VIDEO_TIMEOUT = 900.0


class InstagramError(Exception):
    """The API refused a request (auth, permissions, rate limit, bad input)."""


class MediaRejected(InstagramError):
    """The request was accepted but this media could not be processed."""


@dataclass
class Post:
    """What to publish, independent of the destination network."""

    kind: str  # image | carousel | reel | story
    media: list[tuple[str, bool]]  # (public url, is_video)
    caption: str = ""
    alt: list[str] = field(default_factory=list)
    location: str = ""
    # Only used by destinations that take the file itself (YouTube).
    files: list[Path] = field(default_factory=list)
    title: str = ""
    tags: list[str] = field(default_factory=list)


class GraphAPI:
    """Minimal Graph-style HTTP client with uniform error reporting."""

    def __init__(
        self,
        root: str,
        access_token: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 5.0,
    ) -> None:
        self._root = root.rstrip("/")
        self._session = session or requests.Session()
        # Header rather than query string so the token never lands in URLs/logs.
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._sleep = sleep
        self._poll_interval = poll_interval

    def accepts(self, post: Post) -> bool:
        return True

    def call(self, method: str, path: str, **params: str) -> dict:
        url = f"{self._root}/{path}"
        params = {k: v for k, v in params.items() if v not in ("", None)}
        kwargs = {"params": params} if method == "GET" else {"data": params}
        try:
            resp = self._session.request(
                method, url, headers=self._headers, timeout=60, **kwargs
            )
        except requests.RequestException as exc:
            raise InstagramError(f"{method} {path}: network error: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"data": body}
        if resp.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            if not isinstance(err, dict):
                err = {"message": str(err)}
            detail = err.get("error_user_msg") or err.get("message") or resp.text[:300]
            code = err.get("code", "?")
            sub = err.get("error_subcode")
            tag = f"code {code}" + (f"/{sub}" if sub else "")
            raise InstagramError(
                f"{method} {path} failed (HTTP {resp.status_code}, {tag}): {detail}"
            )
        return body

    def wait_until_ready(
        self, container_id: str, *, fields: str, status_key: str, timeout: float
    ) -> None:
        waited = 0.0
        while True:
            info = self.call("GET", container_id, fields=fields)
            status = info.get(status_key)
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                detail = info.get("status") if status_key != "status" else None
                detail = detail or info.get("error_message") or "no detail"
                raise MediaRejected(f"container {container_id} is {status}: {detail}")
            if waited >= timeout:
                raise InstagramError(
                    f"container {container_id} still {status} after {waited:.0f}s"
                )
            self._sleep(self._poll_interval)
            waited += self._poll_interval


class InstagramClient(GraphAPI):
    def __init__(
        self,
        user_id: str,
        access_token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        api_version: str = DEFAULT_API_VERSION,
        **kwargs,
    ) -> None:
        super().__init__(f"{api_base.rstrip('/')}/{api_version}", access_token, **kwargs)
        self.user_id = user_id

    # -- publishing ---------------------------------------------------------

    def publish(self, post: Post) -> str:
        """Publish a post of any kind. Returns the new media id."""
        if post.kind == "carousel":
            children = []
            for index, (url, video) in enumerate(post.media):
                alt = post.alt[index] if index < len(post.alt) else ""
                if video:
                    params = {"media_type": "VIDEO", "video_url": url}
                else:
                    params = {"image_url": url, "alt_text": alt}
                children.append(self._container(video, is_carousel_item="true", **params))
            container = self._container(
                False,
                media_type="CAROUSEL",
                children=",".join(children),
                caption=post.caption,
                location_id=post.location,
            )
        else:
            url, video = post.media[0]
            if post.kind == "story":
                params = {"media_type": "STORIES"}
                params["video_url" if video else "image_url"] = url
            elif post.kind == "reel":
                params = {
                    "media_type": "REELS",
                    "video_url": url,
                    "share_to_feed": "true",
                    "caption": post.caption,
                    "location_id": post.location,
                }
            else:
                params = {
                    "image_url": url,
                    "caption": post.caption,
                    "alt_text": post.alt[0] if post.alt else "",
                    "location_id": post.location,
                }
            container = self._container(video, **params)
        return self.call(
            "POST", f"{self.user_id}/media_publish", creation_id=container
        )["id"]

    def publish_image(self, image_url: str, caption: str = "") -> str:
        return self.publish(Post("image", [(image_url, False)], caption))

    def comment(self, media_id: str, message: str) -> str:
        return self.call("POST", f"{media_id}/comments", message=message)["id"]

    # -- reading ------------------------------------------------------------

    def permalink(self, media_id: str) -> str | None:
        return self.call("GET", media_id, fields="permalink").get("permalink")

    def account(self) -> dict:
        """Basic account info; doubles as a credentials check."""
        return self.call("GET", self.user_id, fields="id,username")

    def media_counts(self, media_id: str) -> dict:
        return self.call("GET", media_id, fields="like_count,comments_count")

    def media_insights(self, media_id: str, metrics: list[str]) -> dict:
        body = self.call("GET", f"{media_id}/insights", metric=",".join(metrics))
        out = {}
        for row in body.get("data", []):
            values = row.get("values") or [{}]
            out[row.get("name")] = values[0].get("value")
        return out

    # -- internals ----------------------------------------------------------

    def _container(self, video: bool, **params: str) -> str:
        container = self.call("POST", f"{self.user_id}/media", **params)["id"]
        self.wait_until_ready(
            container,
            fields="status_code,status",
            status_key="status_code",
            timeout=VIDEO_TIMEOUT if video else IMAGE_TIMEOUT,
        )
        return container
