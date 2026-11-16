"""Command line entry point: ``python -m autoupload <command>``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

from .instagram import (
    DEFAULT_API_BASE,
    DEFAULT_API_VERSION,
    InstagramClient,
    InstagramError,
    MediaRejected,
)
from .queue import Item, archive, load_queue


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def public_base_url(env: dict) -> str:
    """Where the repository's files are reachable over public HTTPS."""
    explicit = env.get("PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    repo, sha = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_SHA")
    if repo and sha:
        return f"https://raw.githubusercontent.com/{repo}/{sha}"
    raise ConfigError(
        "Set PUBLIC_BASE_URL to the public HTTPS address that serves this "
        "repository's files (Instagram downloads images from a URL)."
    )


def image_url(base: str, root: Path, image: Path) -> str:
    rel = image.resolve().relative_to(root.resolve()).as_posix()
    return f"{base}/{quote(rel)}"


def check_reachable(url: str, session: requests.Session) -> None:
    try:
        resp = session.get(url, stream=True, timeout=30)
        status = resp.status_code
        resp.close()
    except requests.RequestException as exc:
        raise ConfigError(f"Image URL is not reachable: {url} ({exc})") from exc
    if status != 200:
        raise ConfigError(
            f"Image URL returned HTTP {status}: {url}\n"
            "Instagram must be able to download the image without logging in. "
            "If this repository is private, set PUBLIC_BASE_URL to a public host."
        )


def make_client(env: dict, session: requests.Session) -> InstagramClient:
    missing = [k for k in ("IG_USER_ID", "IG_ACCESS_TOKEN") if not env.get(k)]
    if missing:
        raise ConfigError(f"Missing required setting(s): {', '.join(missing)}")
    return InstagramClient(
        env["IG_USER_ID"],
        env["IG_ACCESS_TOKEN"],
        api_base=env.get("IG_API_BASE") or DEFAULT_API_BASE,
        api_version=env.get("IG_API_VERSION") or DEFAULT_API_VERSION,
        session=session,
    )


def describe(item: Item) -> str:
    kind = f"carousel of {len(item.images)}" if item.is_carousel else "image"
    caption = item.caption.replace("\n", " ")
    if len(caption) > 60:
        caption = caption[:57] + "..."
    return f"{item.name} [{kind}] caption: {caption!r}" if caption else f"{item.name} [{kind}] (no caption)"


def cmd_list(args, env, session) -> int:
    items = load_queue(args.root / "queue", env.get("DEFAULT_CAPTION", ""))
    if not items:
        print("Queue is empty.")
        return 0
    bad = 0
    for number, item in enumerate(items, 1):
        print(f"{number:>3}. {describe(item)}")
        for problem in item.problems:
            print(f"       ! {problem}")
        bad += bool(item.problems)
    print(f"\n{len(items)} queued, {bad} with problems.")
    return 1 if bad else 0


def cmd_check(args, env, session) -> int:
    info = make_client(env, session).account()
    print(f"Credentials OK: @{info.get('username', '?')} (id {info.get('id')})")
    return 0


def cmd_post(args, env, session) -> int:
    root: Path = args.root
    items = load_queue(root / "queue", env.get("DEFAULT_CAPTION", ""))
    if not items:
        print("Queue is empty; nothing to post.")
        return 0

    client = None if args.dry_run else make_client(env, session)
    base = None
    posted = 0
    for item in items:
        if posted >= args.count:
            break
        if item.problems:
            print(f"SKIP {item.name}: " + "; ".join(item.problems))
            if not args.dry_run:
                archive(item, root / "failed", {"problems": item.problems})
            continue
        if args.dry_run:
            print(f"Would post: {describe(item)}")
            posted += 1
            continue

        base = base or public_base_url(env)
        urls = [image_url(base, root, image) for image in item.images]
        for url in urls:
            check_reachable(url, session)
        try:
            if item.is_carousel:
                media_id = client.publish_carousel(urls, item.caption)
            else:
                media_id = client.publish_image(urls[0], item.caption)
        except MediaRejected as exc:
            print(f"REJECTED {item.name}: {exc}")
            archive(item, root / "failed", {"problems": [str(exc)]})
            continue

        try:
            link = client.permalink(media_id)
        except InstagramError:
            link = None  # the post is live; a missing link must not undo that
        archive(item, root / "posted", {"media_id": media_id, "permalink": link})
        print(f"POSTED {item.name}: {link or media_id}")
        posted += 1

    left = len(load_queue(root / "queue"))
    print(f"{posted} {'would be ' if args.dry_run else ''}posted, {left} left in queue.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autoupload", description="Post queued images to Instagram."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory containing queue/ (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    post = sub.add_parser("post", help="publish the next item(s) from the queue")
    post.add_argument("--count", type=int, default=1, help="how many posts to publish")
    post.add_argument(
        "--dry-run", action="store_true", help="show what would happen; change nothing"
    )
    post.set_defaults(func=cmd_post)
    sub.add_parser("list", help="show the queue and validate it").set_defaults(func=cmd_list)
    sub.add_parser("check", help="verify the Instagram credentials").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    load_dotenv(args.root / ".env")
    try:
        return args.func(args, dict(os.environ), requests.Session())
    except (ConfigError, InstagramError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
