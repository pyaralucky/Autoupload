# Autoupload

Drop images and videos into a folder and they get posted to Instagram on a
schedule: feed posts, carousels, Reels and Stories.

It uses Instagram's official content publishing API (no password, no browser
automation) and runs on GitHub Actions, so there is no server to maintain.

It also uploads videos to YouTube straight from Telegram: send a video to
your bot and it appears on your channel. See [Telegram to YouTube](#telegram-to-youtube).

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

## Telegram to YouTube

Send or forward a video to your Telegram bot, or post it in a group or channel
the bot is in, and it is uploaded to your YouTube channel. The bot replies to
the video with the YouTube link, or with the reason it could not be uploaded.

1. Every 15 minutes the **Telegram to YouTube** workflow reads the bot's new
   messages.
2. Each video from an allowed chat is downloaded and uploaded with the title,
   description and tags taken from its caption.
3. The result is logged in `posted/youtube.jsonl`.

A video that fails for a passing reason (network, YouTube quota) stays queued
and is retried; one YouTube will never accept is dropped and you are told why.
The same video is never uploaded twice.

### Captions

```
My trip to Goa
Day one on the beach. #travel #goa

@privacy: public
@schedule: 2026-12-25 18:00
@tags: vlog, beach life
@playlist: PLxxxxxxxxxxxxxxxx
```

- The **first line** is the title, the rest is the description. Hashtags also
  become tags.
- Without a caption the title is the file name, or `default_title` from
  `autoupload.toml`.
- `@` lines are optional and are not published:

| Line | Effect |
| --- | --- |
| `@privacy:` | `public`, `unlisted` or `private`, overriding the default |
| `@schedule:` | Upload now as private and go public at this time (timezone from `[schedule]`) |
| `@tags:` | Extra tags, comma separated |
| `@playlist:` | Playlist id to add the video to |
| `@category:` | YouTube category id |

Defaults for all of these are in the `[youtube]` section of `autoupload.toml`.
**Uploads are private by default**; set `privacy = "public"` there once you
are happy with how it works. Vertical videos of up to three minutes become
Shorts automatically.

### Setup

**1. Telegram bot.** Message [@BotFather](https://t.me/BotFather), send
`/newbot` and save the token as the `TELEGRAM_BOT_TOKEN` secret. Use a bot
that no other program reads messages from. To use it in a group, also send
BotFather `/setprivacy` and choose *Disable* so the bot can see videos; in a
channel, add the bot as an administrator.

**2. Google credentials.**

1. In the [Google Cloud console](https://console.cloud.google.com/) create a
   project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen (audience *External*), then set its
   publishing status to **In production**. While it is left in *Testing*, the
   sign-in below stops working after 7 days.
3. Under **Credentials** create an **OAuth client ID** of type **Desktop app**.
4. On your own computer, put the client id and secret in `.env` as
   `YT_CLIENT_ID` and `YT_CLIENT_SECRET`, then run:

   ```
   pip install -r requirements.txt
   python -m autoupload youtube-auth
   ```

   Sign in with the Google account that owns the channel. Google shows an
   "unverified app" warning for your own app; continue past it. The command
   prints a refresh token.
5. Save `YT_CLIENT_ID`, `YT_CLIENT_SECRET` and `YT_REFRESH_TOKEN` as
   repository secrets.

**3. Allow your chat.** Only chats you list can upload; until you list one,
nothing is uploaded. Send `/id` to the bot, run the workflow once from the
**Actions** tab, and the bot replies with the chat's id. Save it as the
repository **variable** `TELEGRAM_ALLOWED_CHATS` (several ids separated by
commas). Everyone who can post in an allowed chat can upload to your channel.

**4. Videos over 20 MB (optional).** Telegram only lets bots download 20 MB
the usual way. To raise that to 2 GB, create an application at
<https://my.telegram.org> (*API development tools*) and save its
`api_id` and `api_hash` as the secrets `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

Check everything with `python -m autoupload youtube-check`.

### Things to know

- **Google's limits.** A new Google Cloud project has a daily API quota that
  covers only a few uploads a day; when it runs out, videos wait and go up
  after it resets. Google also locks uploads from API projects that have not
  passed its [compliance audit](https://support.google.com/youtube/contact/yt_api_form)
  to *private*: you can switch them to public by hand in YouTube Studio, and
  the audit removes the restriction.
- **Speed.** GitHub runs the schedule roughly every 15 minutes, sometimes
  later. For uploads within seconds, run `python -m autoupload youtube --watch`
  on any always-on computer instead and disable the workflow; do not run both.
- **Waiting time.** Telegram keeps unread bot messages for 24 hours, so the
  workflow must not be off for longer than that.
- **Public repositories.** The titles of uploaded videos and the ids of the
  chats they came from are stored in `posted/`, which is visible to anyone who
  can see the repository.

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

python -m autoupload youtube-check    # verify the YouTube and Telegram credentials
python -m autoupload youtube --dry-run   # show the videos waiting in Telegram
python -m autoupload youtube          # upload them now
python -m autoupload youtube --watch  # keep running and upload as videos arrive
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
The Telegram to YouTube check every 15 minutes adds about 2,900 minutes a
month, more than the free allowance of a private repository: lengthen the
`cron` interval in `.github/workflows/youtube.yml`, or use `--watch` instead.
