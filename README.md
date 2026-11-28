# Autoupload

Drop images and videos into a folder and they get posted to Instagram on a
schedule: feed posts, carousels, Reels and Stories.

It uses Instagram's official content publishing API (no password, no browser
automation) and runs on GitHub Actions, so there is no server to maintain.

## How it works

1. You commit files to `queue/`.
2. Straight away, the **Prepare queue** workflow converts and resizes images
   that Instagram would reject, drafts captions if you enabled AI captions,
   and refreshes [`QUEUE.md`](QUEUE.md), a preview of what goes out when.
3. Every hour the **Post to Instagram** workflow checks whether a posting slot
   from `autoupload.toml` has come due. If so it publishes the next item and
   moves it to `posted/`, logging the link in `posted/log.jsonl`.
4. An item Instagram can never accept is moved to `failed/` with the reason,
   so one bad file never blocks the queue.
5. You are told when something needs you: a GitHub issue is opened when a run
   fails, an item is rejected, or the queue is running low.

Instagram does not accept uploads; it downloads each file from a public URL.
On a **public** repository the file's `raw.githubusercontent.com` address is
used automatically. On a **private** repository you must host the files
somewhere public and set `PUBLIC_BASE_URL` (see [Secrets and variables](#secrets-and-variables)).

## The queue

```
queue/
  001-sunrise.jpg          image post
  001-sunrise.txt          its caption (optional)
  002-trip/                carousel: 2-10 images or videos, in filename order
    a.jpg
    b.jpg
    caption.txt
  003-clip.mp4             Reel
  2026-12-25-xmas.jpg      held until 25 December
  stories/                 everything in here is posted as a Story
    teaser.jpg
```

- **Order.** Items go out in filename order, so prefix names with numbers.
- **Dates.** A name starting `YYYY-MM-DD` (optionally `YYYY-MM-DD-HHMM`) is
  held until then, and goes out at the first slot after it, ahead of undated
  items. Dates are read in the timezone from `autoupload.toml`.
- **Formats.** JPEG, PNG, WebP, HEIC, GIF, BMP and TIFF images (all converted
  to JPEG, which is the only format Instagram takes), and MP4/MOV video.
- **Stories** take no caption and are not cross-posted. They use a posting
  slot like any other item.

### What gets fixed automatically

| Problem | Fix |
| --- | --- |
| Not a JPEG | Converted; transparency is filled with `pad_color` |
| Aspect ratio outside 4:5 to 1.91:1 | Padded with borders, or cropped if `fit = "crop"` |
| Wider than 1440 px or over 8 MB | Scaled down and recompressed |
| Phone photo stored sideways | Rotated upright |

Fixed files replace the originals in `queue/`; the originals remain in git
history. Videos are never modified, so they must already meet Instagram's
requirements (MP4/MOV, H.264 or HEVC, AAC audio; Reels 9:16 recommended) and
be under 100 MB, GitHub's file limit.

### Caption files

The caption is the text of the `.txt` file. Lines starting with `@` set
options and are not published:

```
Golden hour at the lake.

@alt: Orange sunrise reflected in a still lake
@location: 106078429431815
@comment: Shot on a 35mm lens
@crosspost: off
```

| Line | Effect |
| --- | --- |
| `@alt:` | Alt text for screen readers. Repeat it once per image in a carousel. Images only. |
| `@location:` | Tags a place. The value is the Facebook Page id of the location. |
| `@comment:` | Posted as the first comment after publishing. |
| `@crosspost: off` | Keep this item on Instagram only. |

Limits checked before posting: 2,200 characters and 30 hashtags, counted
after the footer and hashtag set are added.

## Settings: `autoupload.toml`

Every option is in the file with its default. The main ones:

```toml
[schedule]
timezone = "Asia/Kolkata"
slots = ["09:00", "18:30"]       # one post per slot, every day
jitter_minutes = 10              # random delay so posts don't hit the same minute

[schedule.days]                  # optional per-weekday override
sat = ["11:00"]
sun = []                         # no posts on Sunday

[caption]
footer = "Prints available, link in bio."
hashtag_sets = [["#art", "#daily"], ["#sketch", "#ink"]]   # rotated per post
hashtags_in_first_comment = true

[ai]
enabled = true
voice = "Warm, short, first person, no emoji."

[crosspost]
facebook = true
threads = true
```

The first scheduled run after installing only starts the clock; the first
post goes out at the next slot.

## Setup

### 1. Instagram credentials

You need an Instagram **Business or Creator** account (personal accounts
cannot publish through the API).

1. Create an app at <https://developers.facebook.com/apps> and add the
   Instagram product.
2. Generate a long-lived access token for your account. Permissions:

   | Feature | Instagram Login | Facebook Login |
   | --- | --- | --- |
   | Posting | `instagram_business_content_publish` | `instagram_content_publish` |
   | First comment | `instagram_business_manage_comments` | `instagram_manage_comments` |
   | Insights report | `instagram_business_manage_insights` | `instagram_manage_insights` |

3. Note your Instagram user id, shown next to the token.
4. If your token came from Instagram Login, set the variable `IG_API_BASE` to
   `https://graph.instagram.com`.

### 2. Secrets and variables

Add these under **Settings → Secrets and variables → Actions**. Only the
first two are required; add the others for the features you want.

| Secret | For |
| --- | --- |
| `IG_USER_ID`, `IG_ACCESS_TOKEN` | Posting (required) |
| `GH_SECRETS_TOKEN` | Automatic token refresh: a fine-grained GitHub token for this repository with **Secrets: read and write** |
| `FB_APP_ID`, `FB_APP_SECRET` | Token refresh, Facebook Login tokens only |
| `ANTHROPIC_API_KEY` | AI captions |
| `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN` | Cross-posting to a Facebook Page (`pages_manage_posts`) |
| `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN` | Cross-posting to Threads (`threads_content_publish`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts by Telegram as well as GitHub issues |

| Variable | Purpose |
| --- | --- |
| `PUBLIC_BASE_URL` | Public HTTPS address serving this repository's files, e.g. `https://cdn.example.com/autoupload`. A file at `queue/a.jpg` must be at `<PUBLIC_BASE_URL>/queue/a.jpg`. Required for private repositories and local runs. |
| `IG_API_BASE` | `https://graph.facebook.com` (default) or `https://graph.instagram.com` |
| `IG_API_VERSION` | Graph API version, default `v24.0` |

### 3. Turn it on

Scheduled workflows only run from the default branch, so this code has to be
on it. To test, open **Actions → Post to Instagram → Run workflow** and tick
**dry_run**: it lists what would be posted without posting. Run it without
dry_run to publish immediately, outside the schedule.

## Features in detail

**Token refresh.** Long-lived tokens expire after about 60 days. The *Refresh
Instagram token* workflow renews the token twice a month and writes it back
to the `IG_ACCESS_TOKEN` secret. Without `GH_SECRETS_TOKEN` it cannot save
the token and opens an issue instead; you would then replace the secret by
hand.

**AI captions.** With `[ai] enabled = true`, Claude looks at each image that
has no caption file and writes one into the queue as a normal `.txt` file.
You can edit it before it is posted; an existing caption file is never
overwritten. Each caption is one API call billed to your Anthropic account.
Videos are not captioned.

**Cross-posting.** After an item is live on Instagram it is also published to
the Facebook Page and/or Threads. Threads text is cut to 500 characters. If a
cross-post fails, the Instagram post stays up and you get an alert.

**Alerts.** Issues are labelled `autoupload`. The same alert is not repeated
while its issue is open and less than `repeat_hours` old; close the issue
once you have dealt with it.

**Insights.** Every Monday the *Insights report* workflow writes
[`reports/insights.md`](reports/insights.md): reach, views, likes, comments,
saves and shares for the last 30 days of posts, plus average reach by hour
and weekday posted. Stories are excluded because their metrics expire after a
day.

**Queue preview.** [`QUEUE.md`](QUEUE.md) shows the upcoming grid and each
post with its estimated time, final caption, first comment and any problems.
GitHub renders it with the images.

## Several Instagram accounts

List the accounts under `[accounts]` in `autoupload.toml`:

```toml
[accounts.clinic]                # the original account (IG_USER_ID secret)
[accounts.doctor]
user_id = "17841400000000000"    # the second account's Instagram user id
```

- Posts in `queue/` go to **every** account.
- Posts in `queue/@doctor/` go to **that account only**. A line
  `@account: doctor` in a caption file does the same for a single post.
- Each posting slot publishes one item, whoever it is for.
- One access token covers all accounts if it was generated with every
  account ticked in Meta's consent screen. An account with a separate token
  sets `token_secret = "NAME_OF_SECRET"`.
- If a shared post reaches the first account but fails on another, it is not
  retried (that would post it twice on the first); you get an alert instead.

## Posts created by Claude

A scheduled Claude task prepares posts for the clinic (skincare tips, myth
vs fact, treatment explainers) and opens a pull request titled "Posts for
review". Open it to see every image and caption. **Merging it queues the
posts; nothing is published before that.** To drop a post, delete its two
files on that branch first; to change a caption, edit its `.txt` file there.

- [`content/brief.md`](content/brief.md) is where you steer it: voice,
  treatments offered, topics to favour or avoid. Treatment posts are only
  written for treatments listed there.
- [`content/PLAYBOOK.md`](content/PLAYBOOK.md) holds the rules the task
  follows, including the medical-content limits.
- Images are branded cards drawn by `python -m autoupload card`; colours and
  the clinic name are under `[brand]` in `autoupload.toml`. Some cards carry
  an AI-generated picture, which spends Higgsfield credits.
- Files it creates start with `auto-`, so your own numbered photos are
  always posted ahead of them.

## Running locally

```
pip install -r requirements.txt
cp .env.example .env                  # fill in the values

python -m autoupload list             # show and validate the queue
python -m autoupload prepare          # fix images, draft captions, write QUEUE.md
python -m autoupload check            # verify the credentials
python -m autoupload post --dry-run   # preview
python -m autoupload post --count 2   # publish the next two items now
python -m autoupload insights         # write reports/insights.md
python -m autoupload refresh-token --check-only
```

Tests: `python -m unittest discover -s tests`

## When a run fails

Nothing is lost: the item stays in `queue/` and is retried at the next run.

- **Auth error (code 190)**: the token expired or was revoked; generate a new
  one and update the secret.
- **Media URL returned HTTP 404**: the repository is private or the file is
  not pushed; see `PUBLIC_BASE_URL`.
- **Rate limit**: Instagram caps API publishing per account per 24 hours.
- **Missing permission**: a feature such as first comments or insights needs
  an extra permission on the token; see the table above.

## Cost

On a public repository GitHub Actions is free. On a private one the hourly
check uses roughly 700 to 1,500 of the 2,000 free minutes a month; to use
fewer, narrow the `cron` line in `.github/workflows/post.yml` to the hours
around your slots.
