"""Thin client for the Instagram content publishing API.

Publishing is a two-step flow: create a media *container* that points at a
publicly reachable image URL, wait until Instagram has ingested it, then
publish the container. Carousels are a parent container over child containers.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

import requests

DEFAULT_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v24.0"


class InstagramError(Exception):
    """The API refused a request (auth, permissions, rate limit, bad input)."""


class MediaRejected(InstagramError):
    """Instagram accepted the request but could not process this media."""


class InstagramClient:
    def __init__(
        self,
        user_id: str,
        access_token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        api_version: str = DEFAULT_API_VERSION,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 5.0,
        poll_timeout: float = 120.0,
    ) -> None:
        self.user_id = user_id
        self._root = f"{api_base.rstrip('/')}/{api_version}"
        self._session = session or requests.Session()
        # Header rather than query string so the token never lands in URLs/logs.
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    # -- public API ---------------------------------------------------------

    def publish_image(self, image_url: str, caption: str = "") -> str:
        """Publish a single image. Returns the new media id."""
        container = self._create_container(image_url=image_url, caption=caption)
        self._wait_until_ready(container)
        return self._publish(container)

    def publish_carousel(self, image_urls: Sequence[str], caption: str = "") -> str:
        """Publish 2-10 images as one carousel post. Returns the new media id."""
        children = []
        for url in image_urls:
            child = self._create_container(image_url=url, is_carousel_item="true")
            self._wait_until_ready(child)
            children.append(child)
        parent = self._create_container(
            media_type="CAROUSEL", children=",".join(children), caption=caption
        )
        self._wait_until_ready(parent)
        return self._publish(parent)

    def permalink(self, media_id: str) -> str | None:
        return self._call("GET", media_id, fields="permalink").get("permalink")

    def account(self) -> dict:
        """Basic account info; doubles as a credentials check."""
        return self._call("GET", self.user_id, fields="id,username")

    # -- internals ----------------------------------------------------------

    def _create_container(self, **params: str) -> str:
        params = {k: v for k, v in params.items() if v != ""}
        return self._call("POST", f"{self.user_id}/media", **params)["id"]

    def _publish(self, container_id: str) -> str:
        return self._call(
            "POST", f"{self.user_id}/media_publish", creation_id=container_id
        )["id"]

    def _wait_until_ready(self, container_id: str) -> None:
        waited = 0.0
        while True:
            info = self._call("GET", container_id, fields="status_code,status")
            status = info.get("status_code")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise MediaRejected(
                    f"container {container_id} is {status}: {info.get('status', 'no detail')}"
                )
            if waited >= self._poll_timeout:
                raise InstagramError(
                    f"container {container_id} still {status} after {waited:.0f}s"
                )
            self._sleep(self._poll_interval)
            waited += self._poll_interval

    def _call(self, method: str, path: str, **params: str) -> dict:
        url = f"{self._root}/{path}"
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
        if resp.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            detail = err.get("error_user_msg") or err.get("message") or resp.text[:300]
            code = err.get("code", "?")
            sub = err.get("error_subcode")
            tag = f"code {code}" + (f"/{sub}" if sub else "")
            raise InstagramError(
                f"{method} {path} failed (HTTP {resp.status_code}, {tag}): {detail}"
            )
        return body
