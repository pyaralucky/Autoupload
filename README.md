# Autoupload

Drop images into a folder, and they get posted to Instagram on a schedule.

It uses Instagram's official content publishing API (no password, no browser
automation) and runs on GitHub Actions, so there is no server to maintain.

## How it works

1. You commit JPEGs to `queue/`.
2. Once a day a GitHub Actions workflow takes the first item (by filename),
   publishes it, and moves it to `posted/` with a line in `posted/log.jsonl`
   holding the Instagram link.
3. Anything Instagram can never accept (wrong format, bad aspect ratio, ...)
   is moved to `failed/` with the reason in `failed/log.jsonl`, so one bad
   file never blocks the queue.

Instagram does not accept uploads; it downloads each image from a public URL.
On a **public** repository the workflow uses the file's
`raw.githubusercontent.com` address automatically. On a **private** repository
you must host the images somewhere public and set `PUBLIC_BASE_URL` (see
[Settings](#settings)).

## Queue format

```
queue/
  001-sunrise.jpg        single-image post
  001-sunrise.txt        its caption (optional)
  002-trip/              carousel post: 2-10 JPEGs, shown in filename order
    a.jpg
    b.jpg
    caption.txt          its caption (optional)
```

Name files with a numeric or date prefix to control the order.

Instagram's rules, checked before anything is sent:

| Rule | Limit |
| --- | --- |
| Format | JPEG only |
| File size | 8 MB |
| Aspect ratio | between 4:5 (portrait) and 1.91:1 (landscape) |
| Caption | 2,200 characters, 30 hashtags |

## Setup

### 1. Instagram credentials

You need an Instagram **Business or Creator** account (personal accounts
cannot publish through the API).

1. Create an app at <https://developers.facebook.com/apps> and add the
   Instagram product.
2. Generate a long-lived access token for your account with the
   `instagram_business_content_publish` permission (Instagram Login), or
   `instagram_content_publish` (Facebook Login with a linked Page).
3. Note your Instagram user id, shown next to the token.

Long-lived tokens expire after about 60 days. Refresh the token before then
and update the secret, or the workflow will start failing with an auth error.

### 2. Repository secrets

In **Settings → Secrets and variables → Actions** add:

| Secret | Value |
| --- | --- |
| `IG_USER_ID` | your Instagram user id |
| `IG_ACCESS_TOKEN` | the long-lived token |

### 3. Enable the schedule

Scheduled workflows only run from the default branch, so this code has to be
on it. The schedule lives in `.github/workflows/post.yml`
(`cron: "17 13 * * *"`, daily at 13:17 UTC); edit that line to change it.

To test, open **Actions → Post to Instagram → Run workflow** and tick
**dry_run**: it lists what would be posted without posting.

## Settings

Secrets are above. These optional values go under **Variables** in the same
settings page (or in `.env` for local runs):

| Name | Purpose |
| --- | --- |
| `PUBLIC_BASE_URL` | Public HTTPS address serving this repository's files, e.g. `https://cdn.example.com/autoupload`. An image at `queue/a.jpg` must be at `<PUBLIC_BASE_URL>/queue/a.jpg`. Required for private repositories and local runs. |
| `IG_API_BASE` | `https://graph.facebook.com` (default) or `https://graph.instagram.com` for tokens from Instagram Login. |
| `IG_API_VERSION` | Graph API version, default `v24.0`. |
| `DEFAULT_CAPTION` | Caption for items that have no caption file. |

## Running locally

```
pip install -r requirements.txt
cp .env.example .env        # fill in the values

python -m autoupload list            # show and validate the queue
python -m autoupload check           # verify the credentials
python -m autoupload post --dry-run  # preview
python -m autoupload post --count 2  # publish the next two items
```

Tests: `python -m unittest discover -s tests`

## When a run fails

A red workflow run means nothing was lost: the item stays in `queue/` and is
retried next time. Common causes:

- **Auth error (code 190)**: the token expired; generate a new one.
- **Image URL returned HTTP 404**: the repository is private or the image is
  not pushed; see `PUBLIC_BASE_URL`.
- **Rate limit**: Instagram caps API publishing per account per 24 hours.
