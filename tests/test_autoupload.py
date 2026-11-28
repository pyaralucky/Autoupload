import json
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from PIL import Image

from autoupload import cli, crosspost, insights, notify, prepare, preview, schedule, tokens
from autoupload.config import ConfigError, load_config
from autoupload.instagram import InstagramClient, InstagramError, MediaRejected, Post
from autoupload.queue import compose_caption, in_posting_order, jpeg_size, load_queue

UTC = timezone.utc


def write_jpeg(path: Path, width: int = 1080, height: int = 1080) -> Path:
    """Smallest byte sequence our header parser accepts as a JPEG."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x00" * 10
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8" + app0 + sof + b"\xff\xd9")
    return path


def write_image(path: Path, size=(1080, 1080), mode="RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, (200, 30, 30, 128)[: len(mode)]).save(path)
    return path


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeWeb:
    """Stands in for requests.Session across every service the tool talks to."""

    def __init__(self, container_status="FINISHED", in_progress_polls=0, public=True):
        self.calls = []  # (method, path-or-url, payload)
        self.container_status = container_status
        self.in_progress_polls = in_progress_polls
        self.public = public
        self.issues = []
        self.fail = set()  # path fragments that should return an API error
        self._next = 100

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method in (None, m)]

    def payload(self, path):
        return next(d for _, p, d in self.calls if p == path)

    def _id(self, prefix):
        self._next += 1
        return f"{prefix}{self._next}"

    def get(self, url, **kwargs):  # reachability probe of a media URL
        self.calls.append(("PROBE", url, {}))
        return FakeResponse(200 if self.public else 404)

    def request(self, method, url, headers=None, timeout=None, params=None, data=None, json=None):
        payload = params or data or json or {}
        for host in ("graph.facebook.com/v24.0/", "graph.threads.net/v1.0/", "graph.instagram.com/"):
            if host in url:
                path = url.split(host, 1)[1]
                break
        else:
            path = url
        self.calls.append((method, path, payload))
        if any(f in path for f in self.fail):
            return FakeResponse(400, {"error": {"message": "nope", "code": 100}})

        if "api.github.com" in url:
            return self._github(method, url, payload)
        if "api.telegram.org" in url:
            return FakeResponse(body={"ok": True})
        if path.endswith("oauth/access_token") or path == "refresh_access_token":
            return FakeResponse(body={"access_token": "newtok", "expires_in": 5184000})

        if "graph.threads.net" in url:
            if path == "t1/threads":
                return FakeResponse(body={"id": self._id("tc")})
            if path == "t1/threads_publish":
                return FakeResponse(body={"id": "thread1"})
            if payload.get("fields") == "permalink":
                return FakeResponse(body={"permalink": "https://threads.net/t/1"})
            return FakeResponse(body={"status": "FINISHED"})

        if path == "page1/photos":
            return FakeResponse(body={"id": self._id("ph"), "post_id": "page1_55"})
        if path == "page1/feed":
            return FakeResponse(body={"id": "page1_77"})
        if path == "page1/videos":
            return FakeResponse(body={"id": "vid9"})

        if path in ("42/media", "43/media"):
            return FakeResponse(body={"id": self._id("c")})
        if path == "42/media_publish":
            return FakeResponse(body={"id": "m1"})
        if path == "43/media_publish":
            return FakeResponse(body={"id": "m2"})
        if path.endswith("/comments"):
            return FakeResponse(body={"id": "cm1"})
        if path.endswith("/insights"):
            metrics = payload["metric"].split(",")
            if "views" in metrics and len(metrics) > 1:
                return FakeResponse(400, {"error": {"message": "bad metric", "code": 100}})
            if metrics == ["views"]:
                return FakeResponse(400, {"error": {"message": "bad metric", "code": 100}})
            return FakeResponse(
                body={"data": [{"name": m, "values": [{"value": 10}]} for m in metrics]}
            )
        fields = payload.get("fields", "")
        if fields == "permalink":
            return FakeResponse(body={"permalink": "https://instagram.com/p/x"})
        if fields.startswith("status_code"):
            if self.in_progress_polls:
                self.in_progress_polls -= 1
                return FakeResponse(body={"status_code": "IN_PROGRESS"})
            return FakeResponse(body={"status_code": self.container_status})
        if fields == "id,username":
            return FakeResponse(body={"id": "42", "username": "me"})
        return FakeResponse(400, {"error": {"message": "unknown call", "code": 1}})

    def _github(self, method, url, payload):
        if url.endswith("/actions/secrets/public-key"):
            from nacl import encoding, public

            self.private_key = public.PrivateKey.generate()
            key = self.private_key.public_key.encode(encoding.Base64Encoder()).decode()
            return FakeResponse(body={"key": key, "key_id": "k1"})
        if "/actions/secrets/" in url:
            self.secret = payload
            return FakeResponse(201)
        if method == "GET" and url.endswith("/issues"):
            return FakeResponse(body=self.issues)
        if method == "POST" and url.endswith("/issues"):
            self.issues.append(
                {
                    "number": len(self.issues) + 1,
                    "title": payload["title"],
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            return FakeResponse(201, {})
        return FakeResponse(201, {})


def client(web):
    return InstagramClient("42", "tok", session=web, sleep=lambda s: None)


class TempRoot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.queue = self.root / "queue"
        self.queue.mkdir()

    def toml(self, text):
        (self.root / "autoupload.toml").write_text(text)

    @property
    def config(self):
        return load_config(self.root)


class InstagramClientTests(unittest.TestCase):
    def test_single_image_flow(self):
        web = FakeWeb(in_progress_polls=2)
        post = Post("image", [("https://h/a.jpg", False)], "hi", ["a red square"], "777")
        self.assertEqual(client(web).publish(post), "m1")
        self.assertEqual(
            web.calls[0][2],
            {"image_url": "https://h/a.jpg", "caption": "hi", "alt_text": "a red square",
             "location_id": "777"},
        )
        self.assertEqual(web.paths("GET").count("c101"), 3)
        self.assertEqual(web.calls[-1][1:], ("42/media_publish", {"creation_id": "c101"}))

    def test_carousel_with_video_child(self):
        web = FakeWeb()
        post = Post("carousel", [("https://h/a.jpg", False), ("https://h/b.mp4", True)], "cap", ["alt a"])
        client(web).publish(post)
        creates = [p for m, path, p in web.calls if path == "42/media"]
        self.assertEqual(
            creates[0], {"image_url": "https://h/a.jpg", "alt_text": "alt a", "is_carousel_item": "true"}
        )
        self.assertEqual(
            creates[1], {"media_type": "VIDEO", "video_url": "https://h/b.mp4", "is_carousel_item": "true"}
        )
        self.assertEqual(
            creates[2], {"media_type": "CAROUSEL", "children": "c101,c102", "caption": "cap"}
        )

    def test_reel_and_story(self):
        web = FakeWeb()
        client(web).publish(Post("reel", [("https://h/v.mp4", True)], "cap"))
        self.assertEqual(
            web.calls[0][2],
            {"media_type": "REELS", "video_url": "https://h/v.mp4", "share_to_feed": "true",
             "caption": "cap"},
        )
        web = FakeWeb()
        client(web).publish(Post("story", [("https://h/s.jpg", False)], "ignored"))
        self.assertEqual(web.calls[0][2], {"media_type": "STORIES", "image_url": "https://h/s.jpg"})

    def test_container_error_is_media_rejected(self):
        with self.assertRaises(MediaRejected):
            client(FakeWeb(container_status="ERROR")).publish_image("https://h/a.jpg")

    def test_api_error_surfaces_message(self):
        web = FakeWeb()
        web.fail.add("42")
        with self.assertRaisesRegex(InstagramError, "nope"):
            client(web).account()

    def test_poll_timeout(self):
        with self.assertRaisesRegex(InstagramError, "still IN_PROGRESS"):
            client(FakeWeb(in_progress_polls=10**6)).publish_image("https://h/a.jpg")


class CrosspostTests(unittest.TestCase):
    def test_facebook_single_and_multi_photo(self):
        web = FakeWeb()
        page = crosspost.FacebookPage("page1", "ptok", session=web)
        link = page.publish(Post("image", [("https://h/a.jpg", False)], "cap"))
        self.assertEqual(link, "https://www.facebook.com/page1_55")
        link = page.publish(Post("carousel", [("https://h/a.jpg", False), ("https://h/b.jpg", False)], "cap"))
        self.assertEqual(link, "https://www.facebook.com/page1_77")
        feed = web.payload("page1/feed")
        self.assertEqual(json.loads(feed["attached_media[1]"])["media_fbid"], "ph103")

    def test_threads_truncates_and_publishes(self):
        web = FakeWeb()
        threads = crosspost.Threads("t1", "ttok", session=web, sleep=lambda s: None)
        link = threads.publish(Post("image", [("https://h/a.jpg", False)], "x" * 600))
        self.assertEqual(link, "https://threads.net/t/1")
        text = web.payload("t1/threads")["text"]
        self.assertEqual(len(text), 500)
        self.assertTrue(text.endswith("…"))


class ConfigAndScheduleTests(TempRoot):
    def test_unknown_setting_is_rejected(self):
        self.toml("[schedule]\nslotz = ['09:00']\n")
        with self.assertRaisesRegex(ConfigError, "schedule.slotz"):
            self.config

    def test_slots_per_weekday_and_timezone(self):
        self.toml(
            "[schedule]\ntimezone='Asia/Kolkata'\nslots=['09:00','18:30']\n"
            "[schedule.days]\nsun=[]\nsat=['11:00']\n"
        )
        tz = ZoneInfo("Asia/Kolkata")
        # Monday 16 Nov 2026 10:00 local
        now = datetime(2026, 11, 16, 10, 0, tzinfo=tz)
        self.assertEqual(schedule.latest_slot(self.config, now), datetime(2026, 11, 16, 9, 0, tzinfo=tz))
        # Sunday has no slots, so the latest is Saturday's.
        sunday = datetime(2026, 11, 15, 23, 0, tzinfo=tz)
        self.assertEqual(schedule.latest_slot(self.config, sunday), datetime(2026, 11, 14, 11, 0, tzinfo=tz))
        nxt = schedule.upcoming_slots(self.config, sunday, 2)
        self.assertEqual([s.hour for s in nxt], [9, 18])

    def test_due_slot_fires_once(self):
        self.toml("[schedule]\nslots=['09:00']\n")
        start = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
        self.assertIsNone(schedule.due_slot(self.config, self.root, start))  # first run
        self.assertIsNone(schedule.due_slot(self.config, self.root, start + timedelta(minutes=30)))
        slot = schedule.due_slot(self.config, self.root, start + timedelta(hours=1, minutes=5))
        self.assertEqual(slot, datetime(2026, 11, 16, 9, 0, tzinfo=UTC))
        schedule.mark_done(self.root, slot)
        self.assertIsNone(schedule.due_slot(self.config, self.root, start + timedelta(hours=2)))

    def test_missed_slots_are_caught_up_one_per_run(self):
        self.toml("[schedule]\nslots=['09:00','18:00']\n")
        start = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
        schedule.due_slot(self.config, self.root, start)  # first run starts the clock
        late = datetime(2026, 11, 16, 23, 0, tzinfo=UTC)  # both slots passed unseen
        first = schedule.due_slot(self.config, self.root, late)
        self.assertEqual(first.hour, 9)
        schedule.mark_done(self.root, first)
        second = schedule.due_slot(self.config, self.root, late + timedelta(hours=1))
        self.assertEqual(second.hour, 18)
        schedule.mark_done(self.root, second)
        self.assertIsNone(schedule.due_slot(self.config, self.root, late + timedelta(hours=2)))
        # After a long pause only the last day's slots are owed, not a flood.
        much_later = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
        self.assertEqual(schedule.due_slot(self.config, self.root, much_later).day, 20)

    def test_bad_time(self):
        self.toml("[schedule]\nslots=['25:00']\n")
        with self.assertRaisesRegex(ConfigError, "HH:MM"):
            schedule.slots_on(self.config, datetime(2026, 11, 16).date())


class QueueTests(TempRoot):
    def test_jpeg_size(self):
        self.assertEqual(jpeg_size(write_jpeg(self.queue / "a.jpg", 1080, 1350)), (1080, 1350))
        self.assertEqual(jpeg_size(write_image(self.queue / "real.jpg", (640, 480))), (640, 480))

    def test_kinds_order_and_captions(self):
        write_jpeg(self.queue / "02-b.jpg")
        write_jpeg(self.queue / "01-a.JPG")
        (self.queue / "01-a.txt").write_text("hello #world\n")
        write_jpeg(self.queue / "03-trip" / "1.jpg")
        (self.queue / "03-trip" / "2.mp4").write_bytes(b"v")
        (self.queue / "03-trip" / "caption.txt").write_text("trip")
        (self.queue / "04-clip.mp4").write_bytes(b"v")
        write_jpeg(self.queue / "stories" / "00-s.jpg", 1080, 1920)
        (self.queue / ".gitkeep").touch()
        self.toml("[caption]\ndefault='fallback'\n")
        items = load_queue(self.queue, self.config)
        self.assertEqual(
            [(i.name, i.kind) for i in items],
            [("stories/00-s.jpg", "story"), ("01-a.JPG", "image"), ("02-b.jpg", "image"),
             ("03-trip", "carousel"), ("04-clip.mp4", "reel")],
        )
        self.assertEqual([i.caption for i in items[1:4]], ["hello #world", "fallback", "trip"])
        self.assertTrue(all(not i.problems and not i.fixes for i in items))

    def test_directives(self):
        write_jpeg(self.queue / "a.jpg")
        (self.queue / "a.txt").write_text(
            "Line one\n@alt: A red square\n@location: 123\n@comment: #first\n"
            "@crosspost: off\nemail me @alt: not a directive mid-line\n"
        )
        item = load_queue(self.queue, self.config)[0]
        self.assertEqual(item.caption, "Line one\nemail me @alt: not a directive mid-line")
        self.assertEqual((item.alt, item.location, item.comment, item.crosspost),
                         (["A red square"], "123", "#first", False))

    def test_problems_versus_fixes(self):
        write_jpeg(self.queue / "tall.jpg", 1080, 1920)
        write_image(self.queue / "pic.png")
        (self.queue / "broken.jpg").write_bytes(b"nope")
        write_jpeg(self.queue / "tags.jpg")
        (self.queue / "tags.txt").write_text(" ".join(f"#t{i}" for i in range(31)))
        write_jpeg(self.queue / "2026-13-45-bad.jpg")
        items = {i.name: i for i in load_queue(self.queue, self.config)}
        self.assertIn("aspect ratio", items["tall.jpg"].fixes[0])
        self.assertIn("converted", items["pic.png"].fixes[0])
        self.assertFalse(items["tall.jpg"].problems or items["pic.png"].problems)
        self.assertIn("not a JPEG", items["broken.jpg"].problems[0])
        self.assertIn("31 hashtags", items["tags.jpg"].problems[0])
        self.assertIn("invalid date", items["2026-13-45-bad.jpg"].problems[0])

    def test_dated_items(self):
        write_jpeg(self.queue / "001-plain.jpg")
        write_jpeg(self.queue / "2026-12-25-xmas.jpg")
        write_jpeg(self.queue / "2026-12-24-1800-eve.jpg")
        items = load_queue(self.queue, self.config)
        before = in_posting_order(items, datetime(2026, 12, 24, 12, 0, tzinfo=UTC))
        self.assertEqual([i.name for i in before], ["001-plain.jpg"])
        after = in_posting_order(items, datetime(2026, 12, 25, 9, 0, tzinfo=UTC))
        self.assertEqual(
            [i.name for i in after],
            ["2026-12-24-1800-eve.jpg", "2026-12-25-xmas.jpg", "001-plain.jpg"],
        )

    def test_caption_templates(self):
        write_jpeg(self.queue / "a.jpg")
        (self.queue / "a.txt").write_text("Body\n@comment: hi")
        self.toml("[caption]\nfooter='Follow us'\nhashtag_sets=[['#a','#b'],['#c']]\n")
        item = load_queue(self.queue, self.config)[0]
        self.assertEqual(compose_caption(item, self.config, 0), ("Body\n\nFollow us\n\n#a #b", "hi"))
        self.assertEqual(compose_caption(item, self.config, 1)[0], "Body\n\nFollow us\n\n#c")
        self.toml("[caption]\nhashtag_sets=[['#a']]\nhashtags_in_first_comment=true\n")
        self.assertEqual(compose_caption(item, self.config, 0), ("Body", "hi\n#a"))


class PrepareTests(TempRoot):
    def test_converts_png_with_transparency(self):
        write_image(self.queue / "pic.png", (800, 800), "RGBA")
        (self.queue / "pic.txt").write_text("cap")
        self.assertEqual(prepare.prepare_images(self.queue, self.config, log=lambda m: None), 1)
        self.assertFalse((self.queue / "pic.png").exists())
        item = load_queue(self.queue, self.config)[0]
        self.assertEqual((item.name, item.caption, item.fixes), ("pic.jpg", "cap", []))

    def test_pads_or_crops_aspect_and_limits_width(self):
        write_image(self.queue / "tall.jpg", (1000, 2000))
        write_image(self.queue / "wide.jpg", (3000, 1000))
        prepare.prepare_images(self.queue, self.config, log=lambda m: None)
        self.assertEqual(jpeg_size(self.queue / "tall.jpg"), (1440, 1800))  # 1600x2000 scaled
        self.assertEqual(jpeg_size(self.queue / "wide.jpg"), (1440, 754))
        write_image(self.queue / "crop.jpg", (1000, 2000))
        self.toml("[images]\nfit='crop'\n")
        prepare.prepare_images(self.queue, self.config, log=lambda m: None)
        self.assertEqual(jpeg_size(self.queue / "crop.jpg"), (1000, 1250))
        self.assertTrue(all(not i.fixes for i in load_queue(self.queue, self.config)))

    def test_leaves_good_images_and_stories_alone(self):
        good = write_image(self.queue / "ok.jpg", (1080, 1350))
        story = write_image(self.queue / "stories" / "s.jpg", (1080, 1920))
        before = (good.read_bytes(), story.read_bytes())
        self.assertEqual(prepare.prepare_images(self.queue, self.config, log=lambda m: None), 0)
        self.assertEqual((good.read_bytes(), story.read_bytes()), before)

    def test_ai_captions_only_for_items_without_a_caption_file(self):
        write_image(self.queue / "a.jpg")
        write_image(self.queue / "b.jpg")
        (self.queue / "b.txt").write_text("mine")
        (self.queue / "c.mp4").write_bytes(b"v")
        seen = []

        def draft(images, config):
            seen.append([p.name for p in images])
            return "Drafted caption"

        n = prepare.prepare_captions(self.queue, self.config, log=lambda m: None, draft=draft)
        self.assertEqual((n, seen), (1, [["a.jpg"]]))
        self.assertEqual((self.queue / "a.txt").read_text(), "Drafted caption\n")
        self.assertEqual((self.queue / "b.txt").read_text(), "mine")

    def test_ai_request_shape(self):
        from autoupload import ai

        write_image(self.queue / "a.jpg")
        captured = {}

        class Messages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    stop_reason="end_turn",
                    content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=" Hi ")],
                )

        fake = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
        self.toml("[ai]\nvoice='warm and brief'\n")
        self.assertEqual(ai.draft_caption([self.queue / "a.jpg"], self.config, client=fake), "Hi")
        self.assertEqual(captured["model"], "claude-opus-5-5")
        self.assertEqual(captured["fallbacks"], "default")
        self.assertIn("warm and brief", captured["system"])
        self.assertEqual(captured["messages"][0]["content"][0]["type"], "image")


class CardTests(TempRoot):
    def test_three_layouts_are_valid_queue_images(self):
        from autoupload import card

        brand = self.config["brand"]
        src = write_image(self.root / "src.jpg", (1600, 900))
        card.render(self.queue / "a.jpg", brand, label="Skincare tip", title="A headline " * 4,
                    body="Body text. " * 20)
        card.render(self.queue / "b.jpg", brand, label="Myth vs fact", myth="A myth " * 6,
                    fact="A fact " * 12)
        card.render(self.queue / "c.jpg", brand, label="Treatment explained",
                    title="Peels: what to expect", image=src)
        items = load_queue(self.queue, self.config)
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual(jpeg_size(item.media[0]), (1080, 1350))
            self.assertFalse(item.problems or item.fixes)


class PostCommandTests(TempRoot):
    ENV = {
        "IG_USER_ID": "42",
        "IG_ACCESS_TOKEN": "tok",
        "GITHUB_REPOSITORY": "me/repo",
        "GITHUB_SHA": "abc123",
    }

    def setUp(self):
        super().setUp()
        write_jpeg(self.queue / "01 first.jpg")
        (self.queue / "01 first.txt").write_text("cap\n@comment: first!")
        write_jpeg(self.queue / "02-second.jpg")
        write_jpeg(self.queue / "03-third.jpg")
        self.toml("[notify]\nlow_queue=0\n")

    def run_post(self, web, count=1, dry_run=False, env=None, scheduled=False):
        args = SimpleNamespace(root=self.root, count=count, dry_run=dry_run, scheduled=scheduled)
        return cli.cmd_post(args, self.ENV if env is None else env, web)

    def log(self, folder="posted"):
        return [json.loads(l) for l in (self.root / folder / "log.jsonl").read_text().splitlines()]

    def test_posts_one_archives_it_and_comments(self):
        web = FakeWeb()
        self.assertEqual(self.run_post(web), 0)
        self.assertEqual(
            web.calls[0][1],
            "https://raw.githubusercontent.com/me/repo/abc123/queue/01%20first.jpg",
        )
        self.assertEqual(web.payload("m1/comments"), {"message": "first!"})
        self.assertTrue((self.root / "posted" / "01 first.jpg").is_file())
        self.assertTrue((self.root / "posted" / "01 first.txt").is_file())
        self.assertTrue((self.queue / "02-second.jpg").is_file())
        entry = self.log()[0]
        self.assertEqual((entry["media_id"], entry["permalink"], entry["kind"]),
                         ("m1", "https://instagram.com/p/x", "image"))
        self.assertIn("02-second.jpg", (self.root / "QUEUE.md").read_text())

    def test_ref_override_is_used_for_urls(self):
        web = FakeWeb()
        self.run_post(web, env={**self.ENV, "AUTOUPLOAD_REF": "newsha"})
        self.assertIn("/me/repo/newsha/queue/", web.calls[0][1])

    def test_dry_run_changes_nothing(self):
        web = FakeWeb()
        self.assertEqual(self.run_post(web, count=5, dry_run=True, env={}), 0)
        self.assertEqual(web.calls, [])
        self.assertEqual(len(list(self.queue.iterdir())), 4)
        self.assertFalse((self.root / "QUEUE.md").exists())

    def test_invalid_item_goes_to_failed_with_alert(self):
        (self.queue / "00-broken.jpg").write_bytes(b"nope")
        web = FakeWeb()
        env = {**self.ENV, "GITHUB_TOKEN": "gh"}
        self.assertEqual(self.run_post(web, env=env), 0)
        self.assertTrue((self.root / "failed" / "00-broken.jpg").is_file())
        self.assertTrue((self.root / "posted" / "01 first.jpg").is_file())
        self.assertEqual([i["title"] for i in web.issues], ["Autoupload: items could not be posted"])

    def test_unfixed_item_waits_in_queue(self):
        write_jpeg(self.queue / "00-tall.jpg", 1080, 1920)
        self.run_post(FakeWeb())
        self.assertTrue((self.queue / "00-tall.jpg").is_file())
        self.assertTrue((self.root / "posted" / "01 first.jpg").is_file())

    def test_rejected_media_goes_to_failed(self):
        self.run_post(FakeWeb(container_status="ERROR"), count=1)
        self.assertTrue((self.root / "failed" / "01 first.jpg").is_file())
        self.assertFalse((self.root / "posted").exists())

    def test_unreachable_media_keeps_item_queued(self):
        with self.assertRaisesRegex(ConfigError, "HTTP 404"):
            self.run_post(FakeWeb(public=False))
        self.assertTrue((self.queue / "01 first.jpg").is_file())

    def test_missing_credentials(self):
        with self.assertRaisesRegex(ConfigError, "IG_ACCESS_TOKEN"):
            self.run_post(FakeWeb(), env={"IG_USER_ID": "42"})

    def test_crosspost_and_nonfatal_failure(self):
        self.toml("[crosspost]\nfacebook=true\nthreads=true\n[notify]\nlow_queue=0\n")
        env = {**self.ENV, "FB_PAGE_ID": "page1", "FB_PAGE_ACCESS_TOKEN": "p",
               "THREADS_USER_ID": "t1", "THREADS_ACCESS_TOKEN": "t", "GITHUB_TOKEN": "gh"}
        web = FakeWeb()
        web.fail.add("t1/threads")
        self.assertEqual(self.run_post(web, env=env), 0)
        entry = self.log()[0]
        self.assertEqual(entry["facebook"], "https://www.facebook.com/page1_55")
        self.assertIn("threads:", entry["warnings"][0])
        self.assertEqual([i["title"] for i in web.issues], ["Autoupload: posted with warnings"])

    def test_low_queue_alert_is_not_repeated(self):
        self.toml("[notify]\nlow_queue=3\n")
        env = {**self.ENV, "GITHUB_TOKEN": "gh", "TELEGRAM_BOT_TOKEN": "b", "TELEGRAM_CHAT_ID": "c"}
        web = FakeWeb()
        self.run_post(web, env=env)
        self.run_post(web, env=env)
        self.assertEqual([i["title"] for i in web.issues], ["Autoupload: the queue is running low"])
        self.assertEqual(sum("telegram" in p for p in web.paths()), 1)

    def test_scheduled_posts_only_when_a_slot_is_due(self):
        self.toml("[schedule]\nslots=['00:00','12:00']\n[notify]\nlow_queue=0\n")
        web = FakeWeb()
        self.run_post(web, scheduled=True)  # first run starts the clock
        self.assertEqual(web.calls, [])
        state = self.root / "posted" / "state.json"
        old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        state.write_text(json.dumps({"last_slot": old}))
        self.run_post(web, scheduled=True)
        self.assertEqual(len(self.log()), 1)
        self.run_post(web, scheduled=True)  # the day's other missed slot
        self.assertEqual(len(self.log()), 2)
        self.run_post(web, scheduled=True)  # nothing owed any more
        self.assertEqual(len(self.log()), 2)

    def test_claim_slot_stops_the_scheduler_posting_again(self):
        now = datetime.now(UTC)
        soon = (now + timedelta(minutes=10)).strftime("%H:%M")
        self.toml(f"[schedule]\nslots=['{soon}']\n[notify]\nlow_queue=0\n")
        state = self.root / "posted" / "state.json"
        state.parent.mkdir()
        state.write_text(json.dumps({"last_slot": (now - timedelta(days=3)).isoformat()}))
        web = FakeWeb()
        args = SimpleNamespace(root=self.root, count=1, dry_run=False, scheduled=False, claim_slot=True)
        cli.cmd_post(args, self.ENV, web)
        self.assertEqual(len(self.log()), 1)
        if (now + timedelta(minutes=10)).date() == now.date():  # not straddling midnight
            claimed = datetime.fromisoformat(json.loads(state.read_text())["last_slot"])
            self.assertGreater(claimed, now)
            later = now + timedelta(minutes=20)
            self.assertIsNone(schedule.due_slot(self.config, self.root, later))

    def test_two_accounts_shared_and_own_posts(self):
        self.toml(
            "[accounts.clinic]\n[accounts.doctor]\nuser_id='43'\n[notify]\nlow_queue=0\n"
        )
        write_jpeg(self.queue / "@doctor" / "00-own.jpg")
        (self.queue / "@doctor" / "00-own.txt").write_text("mine")
        write_jpeg(self.queue / "@nobody" / "zz.jpg")
        items = {i.name: i for i in load_queue(self.queue, self.config)}
        self.assertEqual(items["@doctor/00-own.jpg"].accounts, ["doctor"])
        self.assertIn("not defined", items["@nobody/zz.jpg"].problems[0])
        (self.queue / "@nobody" / "zz.jpg").unlink()

        web = FakeWeb()
        self.run_post(web, count=2)
        publishes = [p for p in web.paths("POST") if p.endswith("media_publish")]
        # the doctor-only post goes to 43; the shared post goes to both
        self.assertEqual(publishes, ["43/media_publish", "42/media_publish", "43/media_publish"])
        own, shared = self.log()
        self.assertEqual(list(own["accounts"]), ["doctor"])
        self.assertEqual(shared["media_id"], "m1")
        self.assertEqual(shared["accounts"]["doctor"]["media_id"], "m2")
        self.assertEqual(sum(p.endswith("/comments") for p in web.paths("POST")), 2)

    def test_second_account_failure_does_not_requeue(self):
        self.toml("[accounts.clinic]\n[accounts.doctor]\nuser_id='43'\n[notify]\nlow_queue=0\n")
        web = FakeWeb()
        web.fail.add("43/media")
        self.run_post(web)
        entry = self.log()[0]
        self.assertEqual(entry["media_id"], "m1")
        self.assertIn("account doctor", entry["warnings"][0])
        self.assertFalse((self.queue / "01 first.jpg").exists())

    def test_story_and_hashtag_rotation(self):
        write_jpeg(self.queue / "stories" / "00.jpg", 1080, 1920)
        self.toml("[caption]\nhashtag_sets=[['#a'],['#b']]\n[notify]\nlow_queue=0\n")
        web = FakeWeb()
        self.run_post(web, count=3)
        creates = [p for m, path, p in web.calls if path == "42/media"]
        self.assertEqual(creates[0]["media_type"], "STORIES")
        self.assertNotIn("caption", creates[0])
        self.assertEqual(creates[1]["caption"], "cap\n\n#b")  # index 1: the story was post 0
        self.assertEqual(creates[2]["caption"], "#a")


class NotifyTokenInsightsPreviewTests(TempRoot):
    def test_refresh_and_store_secret(self):
        from nacl import public

        web = FakeWeb()
        env = {"IG_ACCESS_TOKEN": "old", "FB_APP_ID": "a", "FB_APP_SECRET": "s",
               "GH_SECRETS_TOKEN": "pat", "GITHUB_REPOSITORY": "me/repo"}
        args = SimpleNamespace(root=self.root, check_only=False)
        self.assertEqual(cli.cmd_refresh_token(args, env, web), 0)
        self.assertEqual(web.payload("oauth/access_token")["fb_exchange_token"], "old")
        import base64

        sealed = base64.b64decode(web.secret["encrypted_value"])
        self.assertEqual(public.SealedBox(web.private_key).decrypt(sealed), b"newtok")

    def test_instagram_login_refresh_and_missing_app_secret(self):
        web = FakeWeb()
        env = {"IG_ACCESS_TOKEN": "old", "IG_API_BASE": "https://graph.instagram.com"}
        self.assertEqual(tokens.refresh_access_token(env, web)[0], "newtok")
        self.assertEqual(web.calls[0][2]["grant_type"], "ig_refresh_token")
        with self.assertRaisesRegex(ConfigError, "FB_APP_SECRET"):
            tokens.refresh_access_token({"IG_ACCESS_TOKEN": "old"}, web)

    def test_notify_comments_after_repeat_window(self):
        web = FakeWeb()
        env = {"GITHUB_TOKEN": "gh", "GITHUB_REPOSITORY": "me/repo"}
        notify.notify(env, self.config, web, "T", "b")
        web.issues[0]["updated_at"] = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
        notify.notify(env, self.config, web, "T", "b")
        self.assertEqual(len(web.issues), 1)
        self.assertTrue(web.paths("POST")[-1].endswith("/issues/1/comments"))

    def test_insights_report_falls_back_per_metric(self):
        posted = self.root / "posted"
        posted.mkdir()
        now = datetime.now(UTC)
        rows = [
            {"item": "a.jpg", "kind": "image", "at": (now - timedelta(days=1)).isoformat(),
             "media_id": "m1", "permalink": "https://instagram.com/p/x"},
            {"item": "old.jpg", "kind": "image", "at": (now - timedelta(days=90)).isoformat(),
             "media_id": "m0"},
            {"item": "s.jpg", "kind": "story", "at": now.isoformat(), "media_id": "m2"},
        ]
        (posted / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        path = insights.write_report(client(FakeWeb()), self.root, self.config)
        text = path.read_text()
        self.assertIn("[a.jpg](https://instagram.com/p/x) | image | 10 | – | 10 | 10 | 10 | 10 |", text)
        self.assertNotIn("old.jpg", text)
        self.assertNotIn("s.jpg", text)
        self.assertIn("Based on only 1 posts", text)

    def test_preview_plans_slots_and_holds_dated_items(self):
        write_jpeg(self.queue / "001-a.jpg")
        (self.queue / "001-a.txt").write_text("Hello\n@alt: square")
        write_jpeg(self.queue / "2026-11-18-held.jpg")
        (self.queue / "bad.jpg").write_bytes(b"x")
        self.toml("[schedule]\nslots=['09:00']\n")
        now = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        planned = preview.plan(load_queue(self.queue, self.config), self.config, now)
        self.assertEqual(
            [(i.name, s and s.day) for i, s in planned],
            [("001-a.jpg", 17), ("2026-11-18-held.jpg", 18), ("bad.jpg", None)],
        )
        text = preview.render(self.root, self.config, now)
        self.assertIn('<img src="queue/001-a.jpg"', text)
        self.assertIn("> Hello", text)
        self.assertIn("Alt text: square", text)
        self.assertIn("⛔", text)


if __name__ == "__main__":
    unittest.main()
