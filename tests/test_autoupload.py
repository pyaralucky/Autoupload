import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autoupload import cli
from autoupload.instagram import InstagramClient, InstagramError, MediaRejected
from autoupload.queue import jpeg_size, load_queue


def write_jpeg(path: Path, width: int = 1080, height: int = 1080) -> Path:
    """Smallest byte sequence our header parser accepts as a JPEG."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x00" * 10
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8" + app0 + sof + b"\xff\xd9")
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


class FakeGraph:
    """Stands in for requests.Session and mimics the publishing endpoints."""

    def __init__(self, container_status="FINISHED", in_progress_polls=0, public=True):
        self.calls = []
        self.container_status = container_status
        self.in_progress_polls = in_progress_polls
        self.public = public
        self._next = 100

    def get(self, url, **kwargs):  # reachability probe of the image URL
        self.calls.append(("PROBE", url, {}))
        return FakeResponse(200 if self.public else 404)

    def request(self, method, url, headers=None, timeout=None, params=None, data=None):
        payload = params or data or {}
        path = url.split("/v24.0/", 1)[1]
        self.calls.append((method, path, payload))
        assert headers["Authorization"] == "Bearer tok"
        if path == "42/media":
            self._next += 1
            return FakeResponse(body={"id": f"c{self._next}"})
        if path == "42/media_publish":
            return FakeResponse(body={"id": "m1"})
        if payload.get("fields") == "permalink":
            return FakeResponse(body={"permalink": "https://instagram.com/p/x"})
        if payload.get("fields", "").startswith("status_code"):
            if self.in_progress_polls:
                self.in_progress_polls -= 1
                return FakeResponse(body={"status_code": "IN_PROGRESS"})
            return FakeResponse(body={"status_code": self.container_status})
        return FakeResponse(400, {"error": {"message": "bad token", "code": 190}})


def client(graph):
    return InstagramClient("42", "tok", session=graph, sleep=lambda s: None)


class InstagramClientTests(unittest.TestCase):
    def test_single_image_flow(self):
        graph = FakeGraph(in_progress_polls=2)
        self.assertEqual(client(graph).publish_image("https://h/a.jpg", "hi"), "m1")
        steps = [(m, p) for m, p, _ in graph.calls]
        self.assertEqual(steps[0], ("POST", "42/media"))
        self.assertEqual(graph.calls[0][2], {"image_url": "https://h/a.jpg", "caption": "hi"})
        self.assertEqual(steps.count(("GET", "c101")), 3)
        self.assertEqual(steps[-1], ("POST", "42/media_publish"))
        self.assertEqual(graph.calls[-1][2], {"creation_id": "c101"})

    def test_carousel_flow(self):
        graph = FakeGraph()
        client(graph).publish_carousel(["https://h/a.jpg", "https://h/b.jpg"], "cap")
        creates = [p for m, path, p in graph.calls if path == "42/media"]
        self.assertEqual(creates[0], {"image_url": "https://h/a.jpg", "is_carousel_item": "true"})
        self.assertEqual(
            creates[2], {"media_type": "CAROUSEL", "children": "c101,c102", "caption": "cap"}
        )
        self.assertEqual(graph.calls[-1][2], {"creation_id": "c103"})

    def test_container_error_is_media_rejected(self):
        with self.assertRaises(MediaRejected):
            client(FakeGraph(container_status="ERROR")).publish_image("https://h/a.jpg")

    def test_api_error_surfaces_message(self):
        with self.assertRaisesRegex(InstagramError, "bad token"):
            client(FakeGraph()).account()

    def test_poll_timeout(self):
        graph = FakeGraph(in_progress_polls=10**6)
        with self.assertRaisesRegex(InstagramError, "still IN_PROGRESS"):
            client(graph).publish_image("https://h/a.jpg")


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.queue = self.root / "queue"
        self.queue.mkdir()

    def test_jpeg_size(self):
        self.assertEqual(jpeg_size(write_jpeg(self.queue / "a.jpg", 1080, 1350)), (1080, 1350))

    def test_order_captions_and_carousels(self):
        write_jpeg(self.queue / "02-b.jpg")
        write_jpeg(self.queue / "01-a.JPG")
        (self.queue / "01-a.txt").write_text("hello #world\n")
        write_jpeg(self.queue / "03-trip" / "1.jpg")
        write_jpeg(self.queue / "03-trip" / "2.jpg")
        (self.queue / "03-trip" / "caption.txt").write_text("trip")
        (self.queue / ".gitkeep").touch()
        items = load_queue(self.queue, default_caption="default")
        self.assertEqual([i.name for i in items], ["01-a.JPG", "02-b.jpg", "03-trip"])
        self.assertEqual([i.caption for i in items], ["hello #world", "default", "trip"])
        self.assertTrue(items[2].is_carousel)
        self.assertTrue(all(not i.problems for i in items))

    def test_validation_problems(self):
        write_jpeg(self.queue / "tall.jpg", 1080, 1920)
        (self.queue / "pic.png").write_bytes(b"\x89PNG")
        (self.queue / "broken.jpg").write_bytes(b"nope")
        write_jpeg(self.queue / "tags.jpg")
        (self.queue / "tags.txt").write_text(" ".join(f"#t{i}" for i in range(31)))
        problems = {i.name: " ".join(i.problems) for i in load_queue(self.queue)}
        self.assertIn("aspect ratio", problems["tall.jpg"])
        self.assertIn("only JPEG", problems["pic.png"])
        self.assertIn("not a JPEG", problems["broken.jpg"])
        self.assertIn("31 hashtags", problems["tags.jpg"])


class PostCommandTests(unittest.TestCase):
    ENV = {
        "IG_USER_ID": "42",
        "IG_ACCESS_TOKEN": "tok",
        "GITHUB_REPOSITORY": "me/repo",
        "GITHUB_SHA": "abc123",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_jpeg(self.root / "queue" / "01 first.jpg")
        (self.root / "queue" / "01 first.txt").write_text("cap")
        write_jpeg(self.root / "queue" / "02-second.jpg")

    def run_post(self, graph, count=1, dry_run=False, env=None):
        args = SimpleNamespace(root=self.root, count=count, dry_run=dry_run)
        return cli.cmd_post(args, env or self.ENV, graph)

    def test_posts_one_and_archives_it(self):
        graph = FakeGraph()
        self.assertEqual(self.run_post(graph), 0)
        self.assertEqual(
            graph.calls[0][1],
            "https://raw.githubusercontent.com/me/repo/abc123/queue/01%20first.jpg",
        )
        self.assertTrue((self.root / "posted" / "01 first.jpg").is_file())
        self.assertTrue((self.root / "posted" / "01 first.txt").is_file())
        self.assertTrue((self.root / "queue" / "02-second.jpg").is_file())
        log = json.loads((self.root / "posted" / "log.jsonl").read_text())
        self.assertEqual(log["media_id"], "m1")
        self.assertEqual(log["permalink"], "https://instagram.com/p/x")

    def test_dry_run_changes_nothing(self):
        graph = FakeGraph()
        self.assertEqual(self.run_post(graph, count=5, dry_run=True, env={}), 0)
        self.assertEqual(graph.calls, [])
        self.assertEqual(len(list((self.root / "queue").iterdir())), 3)

    def test_invalid_item_goes_to_failed_and_next_is_posted(self):
        write_jpeg(self.root / "queue" / "00-tall.jpg", 1080, 1920)
        self.assertEqual(self.run_post(FakeGraph()), 0)
        self.assertTrue((self.root / "failed" / "00-tall.jpg").is_file())
        self.assertTrue((self.root / "posted" / "01 first.jpg").is_file())

    def test_rejected_media_goes_to_failed(self):
        self.run_post(FakeGraph(container_status="ERROR"), count=1)
        self.assertTrue((self.root / "failed" / "01 first.jpg").is_file())
        self.assertFalse((self.root / "posted").exists())

    def test_unreachable_image_keeps_item_queued(self):
        with self.assertRaisesRegex(cli.ConfigError, "HTTP 404"):
            self.run_post(FakeGraph(public=False))
        self.assertTrue((self.root / "queue" / "01 first.jpg").is_file())

    def test_missing_credentials(self):
        with self.assertRaisesRegex(cli.ConfigError, "IG_ACCESS_TOKEN"):
            self.run_post(FakeGraph(), env={"IG_USER_ID": "42"})


if __name__ == "__main__":
    unittest.main()
