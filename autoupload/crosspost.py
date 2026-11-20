"""Optional cross-posting of a published item to a Facebook Page, Threads and
YouTube (videos only, as Shorts).

Cross-posting is best effort: the Instagram post is already live, so a
failure here is reported but never fails the run.
"""

from __future__ import annotations

import json

from . import youtube
from .instagram import DEFAULT_API_VERSION, IMAGE_TIMEOUT, VIDEO_TIMEOUT, GraphAPI, Post

THREADS_TEXT_LIMIT = 500


class FacebookPage(GraphAPI):
    def __init__(self, page_id: str, access_token: str, *, api_version=DEFAULT_API_VERSION, **kw):
        super().__init__(f"https://graph.facebook.com/{api_version}", access_token, **kw)
        self.page_id = page_id

    def publish(self, post: Post) -> str:
        """Returns a link to the new Page post."""
        images = [url for url, video in post.media if not video]
        videos = [url for url, video in post.media if video]
        if post.kind == "reel" or (videos and not images):
            body = self.call(
                "POST", f"{self.page_id}/videos", file_url=videos[0], description=post.caption
            )
            return f"https://www.facebook.com/{body['id']}"
        if len(images) == 1:
            body = self.call(
                "POST", f"{self.page_id}/photos", url=images[0], message=post.caption
            )
            return f"https://www.facebook.com/{body.get('post_id') or body['id']}"
        # Several photos: upload each unpublished, then attach them to one post.
        attached = {}
        for index, url in enumerate(images):
            photo = self.call("POST", f"{self.page_id}/photos", url=url, published="false")
            attached[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo["id"]})
        body = self.call("POST", f"{self.page_id}/feed", message=post.caption, **attached)
        return f"https://www.facebook.com/{body['id']}"


class Threads(GraphAPI):
    def __init__(self, user_id: str, access_token: str, **kw):
        super().__init__("https://graph.threads.net/v1.0", access_token, **kw)
        self.user_id = user_id

    def publish(self, post: Post) -> str:
        """Returns a link to the new thread (or its id if no link is available)."""
        text = post.caption
        if len(text) > THREADS_TEXT_LIMIT:
            text = text[: THREADS_TEXT_LIMIT - 1].rstrip() + "…"
        if len(post.media) > 1:
            children = [
                self._container(url, video, is_carousel_item="true")
                for url, video in post.media
            ]
            container = self._create(
                False, media_type="CAROUSEL", children=",".join(children), text=text
            )
        else:
            url, video = post.media[0]
            container = self._container(url, video, text=text)
        thread = self.call(
            "POST", f"{self.user_id}/threads_publish", creation_id=container
        )["id"]
        try:
            return self.call("GET", thread, fields="permalink").get("permalink") or thread
        except Exception:
            return thread

    def _container(self, url: str, video: bool, **params: str) -> str:
        if video:
            return self._create(True, media_type="VIDEO", video_url=url, **params)
        return self._create(False, media_type="IMAGE", image_url=url, **params)

    def _create(self, video: bool, **params: str) -> str:
        container = self.call("POST", f"{self.user_id}/threads", **params)["id"]
        self.wait_until_ready(
            container,
            fields="status,error_message",
            status_key="status",
            timeout=VIDEO_TIMEOUT if video else IMAGE_TIMEOUT,
        )
        return container


def targets(config: dict, env: dict, session) -> dict:
    """Enabled cross-post destinations that also have credentials."""
    out = {}
    wanted = config["crosspost"]
    if wanted["facebook"] and env.get("FB_PAGE_ID") and env.get("FB_PAGE_ACCESS_TOKEN"):
        out["facebook"] = FacebookPage(
            env["FB_PAGE_ID"],
            env["FB_PAGE_ACCESS_TOKEN"],
            api_version=env.get("IG_API_VERSION") or DEFAULT_API_VERSION,
            session=session,
        )
    if wanted["threads"] and env.get("THREADS_USER_ID") and env.get("THREADS_ACCESS_TOKEN"):
        out["threads"] = Threads(
            env["THREADS_USER_ID"], env["THREADS_ACCESS_TOKEN"], session=session
        )
    if wanted["youtube"] and all(env.get(k) for k in youtube.CREDENTIALS):
        out["youtube"] = youtube.YouTube(
            *(env[k] for k in youtube.CREDENTIALS), config["youtube"], session=session
        )
    return out
