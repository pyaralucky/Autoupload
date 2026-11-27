# Playbook: preparing a batch of posts

Instructions for the scheduled Claude task that creates Instagram posts for
this repository. The owner reviews every batch as a pull request; nothing
you create is published until they merge it.

## Goal

Keep about one week of clinic content waiting in the queue. The account posts
twice a day, so a full week is 14 posts.

## Steps

1. Read `content/brief.md` (what the owner wants) and `content/history.md`
   (topics already used). Read `README.md` for the queue and caption format.
2. If a pull request whose branch starts with `content/` is already open,
   stop: the owner has not reviewed the last batch yet. Say so in your final
   message and create nothing.
3. Count the files in `queue/` on the default branch whose names start with
   `auto-` and end in `.jpg`. Create `14 - that count` posts (none if the
   count is 14 or more).
4. Plan the posts as a balanced mix of the three kinds below, on topics not
   in the history file and not repeating each other.
5. Create each post (image + caption file) in `queue/`, on a new branch named
   `content/YYYY-MM-DD` (today's date) made from the default branch.
6. Run `python -m autoupload list` and fix anything it flags.
7. Add one line per post to `content/history.md`: date, kind, topic.
8. Commit, push the branch, and open a pull request into the default branch
   (details below). Do not merge it.

## Kinds of post

| Kind | Label on the card | Notes |
| --- | --- | --- |
| Skincare tip | `Skincare tip` | One practical habit per post. |
| Myth vs fact | `Myth vs fact` | A common misconception and the correction. |
| Treatment explained | `Treatment explained` | Only for treatments listed in the brief. If that list is empty, make none and split the batch between the other two kinds. |

When the brief lists treatments, about a third of each batch is treatment
explainers, one treatment per post, starting with treatments that do not yet
appear in the history file. In a small top-up batch, uncovered treatments
come before tips and myths.

A treatment explainer says, in general terms, what the treatment is, what
concerns it is used for, and what a session and recovery typically involve.
It always says that suitability depends on an assessment by the
dermatologist. It does not name brands, give doses or settings, quote the
number of sessions as a promise, or describe results as certain. Refer to
injectables generically ("anti-wrinkle injections", "dermal fillers").

## Content rules

These are firm; a post that cannot meet them is dropped, not bent.

- General education only. Never diagnose, never tell a reader what they
  personally should do about a condition, never give drug names with doses.
- No promises or guarantees of results, no "cure", no "permanent", no
  comparisons with other clinics.
- No before/after claims, no patient stories, no testimonials.
- No prices, discounts or offers. Those come only from the owner.
- State only well-established dermatology facts. If you are not sure a
  number or claim is right, leave it out rather than hedge it.
- Where a topic involves a condition or a procedure, the caption says that a
  dermatologist should assess the individual case.

## Making the image

Every image is a branded card drawn by the repository's own tool, so the
look stays consistent. Install its dependencies first
(`pip install -r requirements.txt`).

Text card (tips):

    python -m autoupload card --out queue/NAME.jpg --label "Skincare tip" \
        --title "Short headline" --body "One or two supporting sentences."

Myth/fact card:

    python -m autoupload card --out queue/NAME.jpg --label "Myth vs fact" \
        --myth "The misconception" --fact "The correction"

Image card (a picture above the headline):

    python -m autoupload card --out queue/NAME.jpg --label "Treatment explained" \
        --title "Short headline" --image /path/to/picture.jpg

Keep headlines under about 60 characters and body text under about 220, and
look at each finished card to check nothing is cut off or awkwardly wrapped.

### Pictures for image cards

About a third of the batch should be image cards (treatment posts first,
then some tips); the rest are text and myth/fact cards.

Generate the picture with the Higgsfield image tool, portrait or square.
Each one spends the owner's credits, so:

- check the credit balance first; if it cannot cover the pictures planned,
  make text cards instead and say so in the pull request;
- generate one picture per image card, no variations or retries for taste.

What the pictures show: calm, clean still-life and ambience, such as
unbranded skincare bottles and jars, cream textures, botanicals, water,
soft fabrics, a tidy clinic interior. What they never show: faces or
identifiable people, skin conditions, wounds, needles or procedures in
progress, before/after pairs, brand names, or any text or lettering.

Download the picture to a temporary folder outside the repository, use it
with `--image`, and do not commit the original.

## Files

Name each post `auto-YYYYMMDD-NN-short-topic` (batch date, two-digit number,
a few words), for example `auto-20261128-03-sunscreen-cloudy-days.jpg`, with
the caption in the matching `.txt`. The `auto-` prefix matters: the owner's
own photos sort ahead of it and are posted first.

Caption file:

- Two to five short sentences that add to the card rather than repeat it.
- The call to action from the brief, if there is one.
- Five to eight relevant hashtags on a final line.
- A line `@alt: ...` describing the card for screen readers, including its
  headline text.

## The pull request

Title: `Posts for review: N posts (YYYY-MM-DD)`.

Images cannot be embedded in the pull request description (the repository
may be private, where such links do not load). Instead, write the preview as
a file on the branch, `content/REVIEW.md`, replacing any earlier one:

- a line saying which batch this is and how to approve it: "Merge the pull
  request to queue these posts. To drop one, delete its two files in
  `queue/` on this branch. To change a caption, edit its .txt file.";
- then every post in order: a heading, the image as
  `<img src="../queue/FILE.jpg" width="360">` (a relative path, which
  GitHub displays in private repositories too), the caption as a quote, and
  the two file names.

Keep the description short: what the batch contains, a link to
`https://github.com/OWNER/REPO/blob/BRANCH/content/REVIEW.md` as the place
to see the images, the same approval instructions, and anything you could
not do (no treatments listed, not enough credits, a post dropped under the
content rules).

Your final message repeats the pull request link and that summary.
