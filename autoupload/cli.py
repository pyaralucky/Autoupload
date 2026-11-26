"""Command line entry point: ``python -m autoupload <command>``."""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from . import crosspost, schedule
from .config import ConfigError, load_config
from .instagram import (
    DEFAULT_API_BASE,
    DEFAULT_API_VERSION,
    InstagramClient,
    InstagramError,
    MediaRejected,
    Post,
)
from .notify import notify
from .queue import (
    Item,
    archive,
    compose_caption,
    in_posting_order,
    is_video,
    load_queue,
    posted_count,
)

_sleep = time.sleep


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


def settings(root: Path, env: dict) -> dict:
    config = load_config(root)
    if env.get("DEFAULT_CAPTION"):
        config["caption"]["default"] = env["DEFAULT_CAPTION"]
    return config


def public_base_url(env: dict) -> str:
    """Where the repository's files are reachable over public HTTPS."""
    explicit = (env.get("PUBLIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    repo = env.get("GITHUB_REPOSITORY")
    ref = env.get("AUTOUPLOAD_REF") or env.get("GITHUB_SHA")
    if repo and ref:
        return f"https://raw.githubusercontent.com/{repo}/{ref}"
    raise ConfigError(
        "Set PUBLIC_BASE_URL to the public HTTPS address that serves this "
        "repository's files (Instagram downloads media from a URL)."
    )


def media_url(base: str, root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return f"{base}/{quote(rel)}"


def check_reachable(url: str, session) -> None:
    try:
        resp = session.get(url, stream=True, timeout=30)
        status = resp.status_code
        resp.close()
    except requests.RequestException as exc:
        raise ConfigError(f"Media URL is not reachable: {url} ({exc})") from exc
    if status != 200:
        raise ConfigError(
            f"Media URL returned HTTP {status}: {url}\n"
            "Instagram must be able to download the file without logging in. "
            "If this repository is private, set PUBLIC_BASE_URL to a public host."
        )


def make_client(env: dict, session) -> InstagramClient:
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
    kind = f"carousel of {len(item.media)}" if item.kind == "carousel" else item.kind
    caption = item.caption.replace("\n", " ")
    if len(caption) > 60:
        caption = caption[:57] + "..."
    tail = f"caption: {caption!r}" if caption else "(no caption)"
    return f"{item.name} [{kind}] {tail}"


def _diagnose(env: dict, session) -> None:
    """Work out which ID and API host this token actually belongs to."""
    from .instagram import GraphAPI

    token = env["IG_ACCESS_TOKEN"]
    version = env.get("IG_API_VERSION") or DEFAULT_API_VERSION
    print("\nLooking for the right settings for this token...")

    try:
        me = GraphAPI(f"https://graph.instagram.com/{version}", token, session=session).call(
            "GET", "me", fields="user_id,username"
        )
        print(
            "This is an Instagram Login token. Set:\n"
            "  variable IG_API_BASE = https://graph.instagram.com\n"
            f"  secret   IG_USER_ID  = {me.get('user_id') or me.get('id')}"
            f"   (@{me.get('username', '?')})"
        )
        return
    except InstagramError:
        pass

    facebook = GraphAPI(f"{DEFAULT_API_BASE}/{version}", token, session=session)
    try:
        pages = facebook.call(
            "GET", "me/accounts", fields="name,instagram_business_account{id,username}"
        ).get("data", [])
    except InstagramError as exc:
        print(f"The token was not accepted by Facebook or Instagram: {exc}")
        return
    linked = [p for p in pages if p.get("instagram_business_account")]
    if linked:
        print("This is a Facebook Login token. Leave IG_API_BASE unset and set:")
        for page in linked:
            account = page["instagram_business_account"]
            print(
                f"  secret IG_USER_ID = {account['id']}   (@{account.get('username', '?')}, "
                f"linked to the Facebook Page '{page.get('name', '?')}')"
            )
    elif pages:
        names = ", ".join(f"'{p.get('name', '?')}'" for p in pages)
        print(
            f"This is a Facebook Login token and it can see the Page(s) {names}, but none "
            "has an Instagram professional account linked. Link the Instagram account to "
            "the Page (Page settings > Linked accounts > Instagram), or generate the token "
            "through Instagram Login instead."
        )
    else:
        print(
            "This is a Facebook Login token but it cannot see any Facebook Page. Regenerate "
            "it with the permissions pages_show_list, pages_read_engagement, instagram_basic "
            "and instagram_content_publish, selecting your Page when asked; or generate the "
            "token through Instagram Login instead."
        )


# -- commands ---------------------------------------------------------------


def cmd_list(args, env, session) -> int:
    config = settings(args.root, env)
    items = load_queue(args.root / "queue", config)
    if not items:
        print("Queue is empty.")
        return 0
    bad = 0
    for number, item in enumerate(items, 1):
        held = f" (held until {item.not_before:%Y-%m-%d %H:%M})" if item.not_before else ""
        print(f"{number:>3}. {describe(item)}{held}")
        for problem in item.problems:
            print(f"       ! {problem}")
        for fix in item.fixes:
            print(f"       ~ {fix}")
        bad += bool(item.problems)
    print(f"\n{len(items)} queued, {bad} with problems. '~' lines are fixed by `prepare`.")
    return 1 if bad else 0


def cmd_check(args, env, session) -> int:
    config = settings(args.root, env)
    client = make_client(env, session)
    try:
        info = client.account()
    except InstagramError as exc:
        print(f"Instagram credentials do NOT work: {exc}")
        _diagnose(env, session)
        return 2
    print(f"Instagram credentials OK: @{info.get('username', '?')} (id {info.get('id')})")
    active = crosspost.targets(config, env, session)
    for name, wanted in config["crosspost"].items():
        if wanted:
            state = "credentials present" if name in active else "enabled but credentials missing"
            print(f"Cross-post to {name}: {state}")
    return 0


def cmd_prepare(args, env, session) -> int:
    from . import prepare, preview

    root = args.root
    config = settings(root, env)
    queue_dir = root / "queue"
    print("Checking images...")
    fixed = prepare.prepare_images(queue_dir, config)
    drafted = 0
    if config["ai"]["enabled"] and not args.no_ai:
        if env.get("ANTHROPIC_API_KEY"):
            print("Drafting missing captions...")
            drafted = prepare.prepare_captions(queue_dir, config)
        else:
            print("AI captions are enabled but ANTHROPIC_API_KEY is not set; skipping.")
    preview.write_preview(root, config, datetime.now(timezone.utc))
    print(f"{fixed} image(s) fixed, {drafted} caption(s) drafted, QUEUE.md updated.")
    return 0


def cmd_preview(args, env, session) -> int:
    from . import preview

    path = preview.write_preview(args.root, settings(args.root, env), datetime.now(timezone.utc))
    print(f"Wrote {path}")
    return 0


def cmd_insights(args, env, session) -> int:
    from . import insights

    path = insights.write_report(make_client(env, session), args.root, settings(args.root, env))
    print(f"Wrote {path}")
    return 0


def cmd_refresh_token(args, env, session) -> int:
    from . import tokens

    if not (env.get("GH_SECRETS_TOKEN") and env.get("GITHUB_REPOSITORY")) and not args.check_only:
        raise ConfigError(
            "refresh-token stores the new token as a repository secret and needs "
            "GH_SECRETS_TOKEN and GITHUB_REPOSITORY. Use --check-only to test the "
            "refresh without saving."
        )
    token, expires = tokens.refresh_access_token(env, session)
    life = f"valid for about {expires // 86400} days" if expires else "expiry not reported"
    if args.check_only:
        print(f"Token refresh works ({life}); nothing was saved.")
        return 0
    tokens.set_github_secret(env, session, "IG_ACCESS_TOKEN", token)
    print(f"IG_ACCESS_TOKEN secret updated ({life}).")
    return 0


def _publish_item(item, client, extra, config, env, session, root, base, index) -> dict:
    """Publish one item everywhere. Returns the log record."""
    caption, comment = compose_caption(item, config, index)
    media = [(media_url(base, root, p), is_video(p)) for p in item.media]
    for url, _ in media:
        check_reachable(url, session)
    post = Post(item.kind, media, caption, item.alt, item.location)
    media_id = client.publish(post)

    warnings = []
    try:
        link = client.permalink(media_id)
    except InstagramError:
        link = None  # the post is live; a missing link must not undo that
    record = {"media_id": media_id, "permalink": link}
    if comment:
        try:
            client.comment(media_id, comment)
        except InstagramError as exc:
            warnings.append(f"first comment: {exc}")
    if item.crosspost and item.kind != "story":
        for name, target in extra.items():
            try:
                record[name] = target.publish(post)
                print(f"  also on {name}: {record[name]}")
            except InstagramError as exc:
                warnings.append(f"{name}: {exc}")
    if warnings:
        record["warnings"] = warnings
    return record


def cmd_post(args, env, session) -> int:
    root: Path = args.root
    config = settings(root, env)
    now = datetime.now(timezone.utc)
    scheduled = getattr(args, "scheduled", False) and not args.dry_run

    slot = None
    if scheduled:
        slot = schedule.due_slot(config, root, now)
        if slot is None:
            print("No posting slot is due.")
            return 0
        print(f"Slot due: {slot:%a %d %b %H:%M %Z}")
        jitter = float(config["schedule"]["jitter_minutes"])
        if jitter > 0:
            delay = random.uniform(0, jitter * 60)
            print(f"Waiting {delay / 60:.1f} min (jitter).")
            _sleep(delay)

    items = load_queue(root / "queue", config)
    ready = in_posting_order(items, now.astimezone(schedule.timezone_of(config)))
    if not ready:
        print("Nothing to post: the queue is empty or everything is held for a later date.")

    client = None if args.dry_run else make_client(env, session)
    extra = {} if args.dry_run else crosspost.targets(config, env, session)
    base = None
    posted = 0
    done: set[str] = set()
    failed: list[str] = []
    warned: list[str] = []
    for item in ready:
        if posted >= args.count:
            break
        if item.problems:
            print(f"SKIP {item.name}: " + "; ".join(item.problems))
            if not args.dry_run:
                archive(item, root / "failed", {"problems": item.problems})
                failed.append(f"**{item.name}**: " + "; ".join(item.problems))
                done.add(item.name)
            continue
        if item.fixes and not args.dry_run:
            print(f"WAIT {item.name}: needs `prepare` first: " + "; ".join(item.fixes))
            continue
        if args.dry_run:
            print(f"Would post: {describe(item)}")
            for fix in item.fixes:
                print(f"  ~ {fix}")
            posted += 1
            continue

        base = base or public_base_url(env)
        try:
            record = _publish_item(
                item, client, extra, config, env, session, root, base, posted_count(root)
            )
        except MediaRejected as exc:
            print(f"REJECTED {item.name}: {exc}")
            archive(item, root / "failed", {"problems": [str(exc)]})
            failed.append(f"**{item.name}**: {exc}")
            done.add(item.name)
            continue
        archive(item, root / "posted", record)
        done.add(item.name)
        print(f"POSTED {item.name}: {record['permalink'] or record['media_id']}")
        warned += [f"**{item.name}**: {w}" for w in record.get("warnings", [])]
        posted += 1

    if slot is not None:
        schedule.mark_done(root, slot)

    remaining = [i for i in items if i.name not in done]
    usable = [i for i in remaining if not i.problems]
    print(
        f"{posted} {'would be ' if args.dry_run else ''}posted, "
        f"{len(remaining)} left in queue."
    )
    if args.dry_run:
        return 0

    from . import preview

    preview.write_preview(root, config, now)
    if failed:
        notify(
            env, config, session, "Autoupload: items could not be posted",
            "These items were moved to `failed/`:\n\n- " + "\n- ".join(failed),
        )
    if warned:
        notify(
            env, config, session, "Autoupload: posted with warnings",
            "The Instagram post went out, but a follow-up step failed:\n\n- "
            + "\n- ".join(warned),
        )
    threshold = int(config["notify"]["low_queue"])
    if posted and len(usable) < threshold:
        notify(
            env, config, session, "Autoupload: the queue is running low",
            f"{len(usable)} post(s) left in `queue/`. Add more to keep the schedule going.",
        )
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
    post.add_argument(
        "--scheduled",
        action="store_true",
        help="only post if a slot from autoupload.toml has come due",
    )
    post.set_defaults(func=cmd_post)

    prep = sub.add_parser("prepare", help="fix images, draft captions, update QUEUE.md")
    prep.add_argument("--no-ai", action="store_true", help="do not draft captions")
    prep.set_defaults(func=cmd_prepare)

    sub.add_parser("list", help="show the queue and validate it").set_defaults(func=cmd_list)
    sub.add_parser("check", help="verify the credentials").set_defaults(func=cmd_check)
    sub.add_parser("preview", help="write QUEUE.md").set_defaults(func=cmd_preview)
    sub.add_parser("insights", help="write reports/insights.md").set_defaults(func=cmd_insights)
    refresh = sub.add_parser("refresh-token", help="renew the Instagram access token")
    refresh.add_argument(
        "--check-only", action="store_true", help="test the refresh without saving"
    )
    refresh.set_defaults(func=cmd_refresh_token)

    args = parser.parse_args(argv)
    load_dotenv(args.root / ".env")
    env = dict(os.environ)
    session = requests.Session()
    try:
        return args.func(args, env, session)
    except (ConfigError, InstagramError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if env.get("GITHUB_ACTIONS") and args.command in ("post", "refresh-token", "insights"):
            try:
                config = load_config(args.root)
            except ConfigError:
                from .config import DEFAULTS as config
            notify(
                env, config, session, f"Autoupload: `{args.command}` failed",
                f"```\n{exc}\n```\nNothing was lost; queued items stay in `queue/` "
                "and are retried on the next run.",
            )
        return 2


if __name__ == "__main__":
    sys.exit(main())
