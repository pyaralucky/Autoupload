import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests

from autoupload import cli, telegram, tg2yt, youtube, youtube_auth
from autoupload.config import DEFAULTS, ConfigError, load_config
from autoupload.telegram import FileUnavailable, TelegramBot, TelegramError
from autoupload.youtube import AuthError, RetryLater, Video, VideoRejected, YouTubeClient

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TOKEN = "123:SECRET"
CHAT = 555
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_CHATS": str(CHAT)}


class Response:
    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_content(self, size):
        yield self.content


def api_error(status, reason, message="nope"):
    return Response(status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}})


class FakeWeb:
    """Stands in for requests.Session for Google, YouTube and Telegram."""

    def __init__(self, updates=(), files=None):
        self.calls = []  # (method, url, kwargs)
        self.updates = list(updates)
        self.files = files or {}  # file_id -> bytes
        self.sent = []  # Telegram messages: (chat_id, text, reply_to)
        self.received = bytearray()  # bytes YouTube has accepted
        self.total = None
        self.video = None  # metadata of the upload in progress
        self.uploads = []  # finished: (metadata, bytes)
        self.playlist_items = []
        self.fail = {}  # scripted responses by key, consumed in order
        self.drop_next_chunk = False

    def _scripted(self, key):
        queue = self.fail.get(key)
        return queue.pop(0) if queue else None

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url == youtube.TOKEN_URL:
            return self._scripted("token") or Response(200, {"access_token": "AT", "expires_in": 3600})
        if "api.telegram.org" in url:
            return self._telegram(url, kw)
        assert kw["headers"]["Authorization"] == "Bearer AT", "request without access token"
        if url == youtube.UPLOAD_URL:
            scripted = self._scripted("start")
            if scripted:
                return scripted
            self.video = kw["json"]
            self.total = int(kw["headers"]["X-Upload-Content-Length"])
            self.received = bytearray()
            return Response(200, {}, {"Location": "https://upload.example/session1"})
        if url == "https://upload.example/session1":
            return self._chunk(kw)
        if url == f"{youtube.API}/channels":
            return Response(200, {"items": [{"id": "UC1", "snippet": {"title": "My Channel"}}]})
        if url == f"{youtube.API}/playlistItems":
            scripted = self._scripted("playlist")
            if scripted:
                return scripted
            self.playlist_items.append(kw["json"]["snippet"])
            return Response(200, {})
        raise AssertionError(f"unexpected request {method} {url}")

    def _chunk(self, kw):
        content_range = kw["headers"]["Content-Range"]
        if content_range.startswith("bytes */"):  # status query
            pass
        else:
            if self.drop_next_chunk:  # connection lost after half the chunk arrived
                self.drop_next_chunk = False
                self.received += kw["data"][: len(kw["data"]) // 2]
                raise requests.ConnectionError("reset")
            span, total = content_range[len("bytes "):].split("/")
            start, end = map(int, span.split("-"))
            assert start == len(self.received), f"chunk at {start}, expected {len(self.received)}"
            assert end - start + 1 == len(kw["data"]) and int(total) == self.total
            self.received += kw["data"]
        if len(self.received) == self.total:
            video_id = f"vid{len(self.uploads) + 1}"
            self.uploads.append((self.video, bytes(self.received)))
            privacy = self.video["status"]["privacyStatus"]
            return Response(200, {"id": video_id, "status": {"privacyStatus": privacy}})
        headers = {"Range": f"bytes=0-{len(self.received) - 1}"} if self.received else {}
        return Response(308, None, headers)

    def _telegram(self, url, kw):
        assert url.startswith(f"https://api.telegram.org/bot{TOKEN}/") or url.startswith(
            f"https://api.telegram.org/file/bot{TOKEN}/"
        )
        if "/file/" in url:
            return Response(200, None, content=self.files[url.rsplit("/", 1)[1]])
        method, params = url.rsplit("/", 1)[1], kw["json"]
        scripted = self._scripted(method)
        if scripted:
            return scripted
        if method == "getUpdates":
            offset = params.get("offset", 0)
            return Response(200, {"ok": True, "result": [u for u in self.updates if u["update_id"] >= offset]})
        if method == "getFile":
            return Response(200, {"ok": True, "result": {"file_path": f"videos/{params['file_id']}"}})
        if method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"], params.get("reply_to_message_id")))
            return Response(200, {"ok": True, "result": {}})
        if method == "getMe":
            return Response(200, {"ok": True, "result": {"username": "upload_bot"}})
        raise AssertionError(f"unexpected Telegram method {method}")


def video_update(update_id, caption="", size=None, chat=CHAT, kind="video", file_id=None, **extra):
    file_id = file_id or f"f{update_id}"
    media = {"file_id": file_id, "file_unique_id": f"u-{file_id}", "mime_type": "video/mp4", **extra}
    if size is not None:
        media["file_size"] = size
    message = {
        "message_id": update_id * 10,
        "date": int(NOW.timestamp()),
        "chat": {"id": chat, "type": "private"},
        kind: media,
    }
    if caption:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def text_update(update_id, text, chat=CHAT, chat_type="private"):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id * 10, "chat": {"id": chat, "type": chat_type}, "text": text},
    }


def config(**youtube_settings):
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["youtube"].update(youtube_settings)
    return cfg


def client(web, **kw):
    return YouTubeClient("cid", "csecret", "rtoken", session=web, sleep=lambda s: None, **kw)


class YouTubeClientTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "v.mp4"
        self.data = bytes(range(256)) * 40  # 10,240 bytes
        self.path.write_bytes(self.data)

    def test_upload_in_chunks(self):
        web = FakeWeb()
        result = client(web, chunk_size=4096).upload(
            self.path, Video("Title", "Desc", ["a", "A", "b c"], "unlisted"), "video/mp4"
        )
        self.assertEqual(result["id"], "vid1")
        meta, body = web.uploads[0]
        self.assertEqual(body, self.data)
        self.assertEqual(meta["snippet"]["title"], "Title")
        self.assertEqual(meta["snippet"]["tags"], ["a", "b c"])
        self.assertEqual(meta["status"]["privacyStatus"], "unlisted")
        puts = [c for c in web.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 3)
        self.assertEqual(sum(1 for c in web.calls if c[1] == youtube.TOKEN_URL), 1)
        start = next(c for c in web.calls if c[1] == youtube.UPLOAD_URL)
        self.assertEqual(start[2]["params"]["uploadType"], "resumable")
        self.assertEqual(start[2]["headers"]["X-Upload-Content-Type"], "video/mp4")

    def test_upload_resumes_after_dropped_connection(self):
        web = FakeWeb()
        web.drop_next_chunk = True
        client(web, chunk_size=4096).upload(self.path, Video("T"))
        self.assertEqual(web.uploads[0][1], self.data)

    def test_upload_gives_up_after_repeated_server_errors(self):
        web = FakeWeb()
        yt = client(web, chunk_size=4096, retries=2)
        with mock.patch.object(web, "_chunk", return_value=Response(503, None)):
            with self.assertRaises(youtube.YouTubeError) as caught:
                yt.upload(self.path, Video("T"))
        self.assertNotIsInstance(caught.exception, (VideoRejected, RetryLater, AuthError))

    def test_scheduled_release_is_private_with_publish_time(self):
        web = FakeWeb()
        client(web).upload(self.path, Video("T", privacy="public", publish_at="2026-12-25T12:30:00Z"))
        status = web.uploads[0][0]["status"]
        self.assertEqual(status, {
            "privacyStatus": "private", "selfDeclaredMadeForKids": False,
            "publishAt": "2026-12-25T12:30:00Z",
        })

    def test_errors_are_classified(self):
        cases = [
            (api_error(403, "quotaExceeded"), RetryLater),
            (api_error(400, "uploadLimitExceeded"), RetryLater),
            (api_error(400, "invalidTitle"), VideoRejected),
            (api_error(401, "authError"), AuthError),
            (api_error(403, "youtubeSignupRequired"), AuthError),
        ]
        for response, expected in cases:
            web = FakeWeb()
            web.fail["start"] = [response]
            with self.assertRaises(expected):
                client(web).upload(self.path, Video("T"))

    def test_revoked_refresh_token_explains_itself(self):
        web = FakeWeb()
        web.fail["token"] = [Response(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})]
        with self.assertRaises(AuthError) as caught:
            client(web).channel()
        self.assertIn("youtube-auth", str(caught.exception))
        self.assertNotIn("rtoken", str(caught.exception))

    def test_channel_and_cleaning(self):
        self.assertEqual(client(FakeWeb()).channel(), {"id": "UC1", "title": "My Channel"})
        self.assertEqual(len(youtube.clean_title("x" * 150)), 100)
        self.assertEqual(youtube.clean_title(" a <b>\n c "), "a b c")
        self.assertLessEqual(len(youtube.clean_description("é" * 4000).encode()), 5000)
        tags = youtube.clean_tags(["tag%03d" % i for i in range(200)])
        self.assertLessEqual(sum(len(t) + 1 for t in tags), 450)
        self.assertGreater(len(tags), 50)

    def test_empty_file_is_rejected(self):
        self.path.write_bytes(b"")
        with self.assertRaises(VideoRejected):
            client(FakeWeb()).upload(self.path, Video("T"))


class DescribeTests(unittest.TestCase):
    def item(self, caption="", name="", date=0):
        return {"caption": caption, "name": name, "date": date}

    def test_caption_becomes_title_description_and_tags(self):
        video = tg2yt.describe(
            self.item("My trip\nDay one in Goa #travel #goa\n@tags: vlog, beach life"),
            config(tags=["daily"], description_footer="Subscribe!"), NOW,
        )
        self.assertEqual(video.title, "My trip")
        self.assertEqual(video.description, "Day one in Goa #travel #goa\n\nSubscribe!")
        self.assertEqual(video.tags, ["vlog", "beach life", "travel", "goa", "daily"])
        self.assertEqual(video.privacy, "private")

    def test_options(self):
        video = tg2yt.describe(
            self.item("T\n@privacy: Public\n@playlist: PL9\n@category: 10"), config(), NOW
        )
        self.assertEqual((video.privacy, video.playlist, video.category), ("public", "PL9", "10"))

    def test_schedule_uses_the_configured_timezone(self):
        cfg = config()
        cfg["schedule"]["timezone"] = "Asia/Kolkata"
        video = tg2yt.describe(self.item("T\n@schedule: 2026-12-25 18:00"), cfg, NOW)
        self.assertEqual(video.publish_at, "2026-12-25T12:30:00Z")
        past = tg2yt.describe(self.item("T\n@schedule: 2026-01-01"), cfg, NOW)
        self.assertEqual((past.publish_at, past.privacy), ("", "public"))

    def test_title_fallbacks(self):
        self.assertEqual(tg2yt.describe(self.item(name="my_clip.mov"), config(), NOW).title, "my clip")
        stamp = int(datetime(2026, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp())
        self.assertEqual(tg2yt.describe(self.item(date=stamp), config(), NOW).title, "Video 2026-03-04")
        only_options = tg2yt.describe(self.item("@privacy: unlisted"), config(), NOW)
        self.assertEqual(only_options.title, "Video 2026-11-19")

    def test_bad_options_are_reported(self):
        for caption in ("T\n@privacy: secret", "T\n@schedule: tomorrow", "T\n@visiblity: public"):
            with self.assertRaises(ValueError):
                tg2yt.describe(self.item(caption), config(), NOW)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        patcher = mock.patch.object(tg2yt, "notify")
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

    def run_once(self, web, env=ENV, cfg=None, now=NOW, **kw):
        bot = TelegramBot(TOKEN, session=web)
        return tg2yt.run(self.root, cfg or config(), env, web, bot, client(web), now=now, **kw)

    def state(self):
        return json.loads((self.root / tg2yt.STATE_FILE).read_text())

    def log(self):
        path = self.root / tg2yt.LOG_FILE
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_video_is_uploaded_and_link_sent_back(self):
        web = FakeWeb([video_update(7, "Hello world\nA description", size=5)], {"f7": b"VIDEO"})
        result = self.run_once(web)
        self.assertEqual(result, {"queued": 1, "uploaded": 1, "failed": 0, "waiting": 0})
        meta, body = web.uploads[0]
        self.assertEqual(body, b"VIDEO")
        self.assertEqual(meta["snippet"]["title"], "Hello world")
        self.assertEqual(meta["snippet"]["description"], "A description")
        self.assertEqual(meta["status"]["privacyStatus"], "private")
        chat, text, reply_to = web.sent[0]
        self.assertEqual((chat, reply_to), (CHAT, 70))
        self.assertIn("https://youtu.be/vid1", text)
        self.assertIn("private", text)
        state = self.state()
        self.assertEqual((state["offset"], state["pending"], state["done"]), (8, [], ["u-f7"]))
        self.assertEqual(self.log()[0]["video_id"], "vid1")
        self.notify.assert_not_called()

    def test_same_updates_seen_again_are_not_uploaded_twice(self):
        web = FakeWeb([video_update(7, size=5)], {"f7": b"VIDEO"})
        self.run_once(web)
        (self.root / tg2yt.STATE_FILE).write_text(json.dumps({**self.state(), "offset": 0}))
        result = self.run_once(web)
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(len(web.uploads), 1)

    def test_second_run_acknowledges_processed_updates(self):
        web = FakeWeb([video_update(7, size=5)], {"f7": b"VIDEO"})
        self.run_once(web)
        self.run_once(web)
        offsets = [c[2]["json"].get("offset") for c in web.calls if c[1].endswith("/getUpdates")]
        self.assertEqual(offsets, [None, 8])

    def test_other_chats_are_refused(self):
        web = FakeWeb(
            [video_update(1, size=5, chat=999), text_update(2, "hi", chat=-42, chat_type="group")],
            {"f1": b"V"},
        )
        result = self.run_once(web)
        self.assertEqual((result["queued"], web.uploads), (0, []))
        self.assertEqual(len(web.sent), 1)  # the private chat is told; the group is not
        self.assertIn("999", web.sent[0][1])

    def test_nothing_is_uploaded_without_an_allow_list(self):
        web = FakeWeb([video_update(1, size=5), text_update(2, "/id")], {"f1": b"V"})
        result = self.run_once(web, env={"TELEGRAM_BOT_TOKEN": TOKEN})
        self.assertEqual((result["queued"], web.uploads), (0, []))
        self.assertTrue(all(str(CHAT) in text for _, text, _ in web.sent))

    def test_commands_gifs_and_documents(self):
        gif = video_update(3, size=5, kind="document")
        gif["message"]["animation"] = {"file_id": "g"}
        web = FakeWeb(
            [
                text_update(1, "/id@upload_bot"),
                text_update(2, "just chatting"),
                gif,
                video_update(4, "Doc video", size=5, kind="document", file_name="clip.mov"),
                {"update_id": 5, "channel_post": video_update(5, "From channel", size=5)["message"]},
            ],
            {"f4": b"DOC", "f5": b"CHAN"},
        )
        result = self.run_once(web)
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual([m["snippet"]["title"] for m, _ in web.uploads], ["Doc video", "From channel"])
        self.assertIn(f"id is {CHAT}", web.sent[0][1])

    def test_large_video_without_api_credentials_is_explained(self):
        web = FakeWeb([video_update(1, "Big", size=50 * 1024 * 1024)])
        result = self.run_once(web)
        self.assertEqual((result["failed"], result["waiting"]), (1, 0))
        self.assertIn("TELEGRAM_API_ID", web.sent[0][1])
        self.assertIn("50 MB", web.sent[0][1])
        self.notify.assert_called_once()

    def test_large_video_uses_the_native_download(self):
        web = FakeWeb([video_update(1, "Big", size=50 * 1024 * 1024)])
        env = {**ENV, "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h"}

        def fake_download(env_, chat, message, dest):
            self.assertEqual((chat, message), (CHAT, 10))
            dest.write_bytes(b"BIGVIDEO")
            return dest

        with mock.patch.object(telegram, "download_large", fake_download):
            result = self.run_once(web, env=env)
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(web.uploads[0][1], b"BIGVIDEO")

    def test_quota_pauses_and_resumes_later(self):
        web = FakeWeb([video_update(1, "A", size=1), video_update(2, "B", size=1)], {"f1": b"a", "f2": b"b"})
        web.fail["start"] = [api_error(403, "quotaExceeded")]
        first = self.run_once(web)
        self.assertEqual((first["uploaded"], first["waiting"]), (0, 2))
        self.assertEqual(len(web.sent), 1)
        self.assertIn("quota", web.sent[0][1])
        soon = self.run_once(web, now=NOW.replace(minute=30))
        self.assertEqual(soon["uploaded"], 0)
        self.assertEqual(len([c for c in web.calls if c[1] == youtube.UPLOAD_URL]), 1)
        later = self.run_once(web, now=NOW.replace(hour=14))
        self.assertEqual((later["uploaded"], later["waiting"]), (2, 0))
        self.assertEqual(len([t for _, t, _ in web.sent if "quota" in t]), 1)
        self.notify.assert_not_called()

    def test_transient_failure_is_retried_then_dropped(self):
        web = FakeWeb([video_update(1, "A", size=1)], {"f1": b"a"})
        web.fail["getFile"] = [Response(500, {"ok": False, "error_code": 500, "description": "oops"})] * 2
        cfg = config(max_attempts=2)
        first = self.run_once(web, cfg=cfg)
        self.assertEqual((first["failed"], first["waiting"]), (0, 1))
        self.assertEqual(self.run_once(web, cfg=cfg)["waiting"], 1)  # too soon to retry
        second = self.run_once(web, cfg=cfg, now=NOW.replace(hour=13))
        self.assertEqual((second["failed"], second["waiting"]), (1, 0))
        self.assertIn("gave up", web.sent[0][1])
        self.assertNotIn(TOKEN, web.sent[0][1])

    def test_rejected_video_does_not_block_the_next(self):
        web = FakeWeb(
            [video_update(1, "Bad\n@privacy: nope", size=1), video_update(2, "Good", size=1)],
            {"f1": b"a", "f2": b"b"},
        )
        result = self.run_once(web)
        self.assertEqual((result["uploaded"], result["failed"]), (1, 1))
        self.assertIn("@privacy", web.sent[0][1])
        # Sent again with the caption fixed, the same file is accepted...
        web.updates.append(video_update(3, "Bad no more", size=1, file_id="f1"))
        self.assertEqual(self.run_once(web)["uploaded"], 1)
        # ...but a video that already went up is not uploaded a second time.
        web.updates.append(video_update(4, "Good", size=1, file_id="f2"))
        again = self.run_once(web)
        self.assertEqual((again["queued"], len(web.uploads)), (0, 3))
        self.assertIn("already uploaded", web.sent[-1][1])

    def test_auth_error_stops_the_run_and_keeps_the_video(self):
        web = FakeWeb([video_update(1, "A", size=1)], {"f1": b"a"})
        web.fail["token"] = [Response(400, {"error": "invalid_grant"})]
        with self.assertRaises(AuthError):
            self.run_once(web)
        self.assertEqual(len(self.state()["pending"]), 1)

    def test_max_per_run_and_playlist(self):
        updates = [video_update(i, f"V{i}", size=1) for i in (1, 2, 3)]
        web = FakeWeb(updates, {f"f{i}": b"x" for i in (1, 2, 3)})
        web.fail["playlist"] = [api_error(404, "playlistNotFound")]
        result = self.run_once(web, cfg=config(max_per_run=2, playlist="PL1"))
        self.assertEqual((result["uploaded"], result["waiting"]), (2, 1))
        self.assertIn("not added to the playlist", web.sent[0][1])
        self.assertEqual(web.playlist_items[0]["resourceId"]["videoId"], "vid2")

    def test_dry_run_changes_nothing(self):
        web = FakeWeb([video_update(1, "A", size=1)], {"f1": b"a"})
        bot = TelegramBot(TOKEN, session=web)
        result = tg2yt.run(self.root, config(), ENV, web, bot, None, now=NOW, dry_run=True)
        self.assertEqual(result["waiting"], 1)
        self.assertFalse((self.root / tg2yt.STATE_FILE).exists())
        self.assertEqual((web.sent, web.uploads), ([], []))

    def test_bad_allow_list(self):
        with self.assertRaises(ConfigError):
            tg2yt.allowed_chats({"TELEGRAM_ALLOWED_CHATS": "@mychannel"})
        self.assertEqual(tg2yt.allowed_chats({"TELEGRAM_ALLOWED_CHATS": "1, -1002 3"}), {1, -1002, 3})


class TelegramTests(unittest.TestCase):
    def test_errors_never_contain_the_token(self):
        class Broken:
            def request(self, method, url, **kw):
                raise requests.ConnectionError(f"failed: {url}")

        with self.assertRaises(TelegramError) as caught:
            TelegramBot(TOKEN, session=Broken()).me()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_conflict_and_too_big(self):
        web = FakeWeb()
        web.fail["getUpdates"] = [Response(409, {"ok": False, "error_code": 409, "description": "Conflict"})]
        with self.assertRaises(TelegramError) as caught:
            TelegramBot(TOKEN, session=web).get_updates()
        self.assertIn("webhook", str(caught.exception))
        web.fail["getFile"] = [Response(400, {"ok": False, "error_code": 400, "description": "Bad Request: file is too big"})]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileUnavailable):
            TelegramBot(TOKEN, session=web).download("f", Path(tmp) / "v.mp4")


class CliAndAuthTests(unittest.TestCase):
    def test_config_validates_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "autoupload.toml").write_text('[youtube]\nprivacy = "hidden"\n')
            with self.assertRaises(ConfigError):
                load_config(Path(tmp))

    def test_missing_credentials(self):
        with self.assertRaises(ConfigError) as caught:
            cli.make_youtube({"YT_CLIENT_ID": "x"}, None)
        self.assertIn("YT_REFRESH_TOKEN", str(caught.exception))

    def test_sign_in_round_trip(self):
        """The real local redirect server, with Google replaced by a thread."""
        seen = {}

        class Google:
            def request(self, method, url, **kw):
                seen.update(kw["data"])
                return Response(200, {"refresh_token": "RT", "access_token": "AT"})

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            redirect, state = query["redirect_uri"][0], query["state"][0]
            self.assertEqual(query["access_type"], ["offline"])

            def visit():
                plain = requests.Session()
                plain.trust_env = False  # no proxy for 127.0.0.1
                plain.get(redirect, params={"state": "wrong", "code": "evil"}, timeout=10)
                plain.get(redirect, params={"state": state, "code": "CODE"}, timeout=10)

            threading.Thread(target=visit, daemon=True).start()

        env = {"YT_CLIENT_ID": "cid", "YT_CLIENT_SECRET": "cs"}
        with mock.patch.object(youtube_auth.webbrowser, "open", browser), mock.patch("builtins.print"):
            token = youtube_auth.sign_in(env, Google())
        self.assertEqual(token, "RT")
        self.assertEqual(seen["code"], "CODE")
        self.assertTrue(seen["redirect_uri"].startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
