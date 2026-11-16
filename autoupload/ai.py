"""Caption drafting with Claude for queue items that have no caption file."""

from __future__ import annotations

import base64
import io
from pathlib import Path

# Models that accept the server-side refusal fallback.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1", "claude-fable-5-2", "claude-sonnet-5-5")


class AIError(Exception):
    pass


def _image_block(path: Path) -> dict:
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1280, 1280))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(buffer.getvalue()).decode("ascii"),
        },
    }


def _instructions(config: dict) -> str:
    ai = config["ai"]
    own_tags = any(config["caption"]["hashtag_sets"])
    lines = [
        "You write captions for an Instagram account. You will be shown the "
        "image or images of one post, in order. Write the caption the account "
        "owner would publish with it.",
        f"Write in {ai['language']}.",
        "Describe only what is actually visible; do not invent names, places, "
        "dates, prices or other facts the image does not show.",
        "The caption is published automatically, so reply with the caption "
        "text only: no preamble, no quotation marks, no alternatives.",
    ]
    if own_tags:
        lines.append("Do not include hashtags; they are added separately.")
    else:
        lines.append("End with three to five relevant hashtags on their own line.")
    if ai["voice"].strip():
        lines.append("The owner describes the voice they want as follows:\n" + ai["voice"].strip())
    return "\n\n".join(lines)


def draft_caption(images: list[Path], config: dict, client=None) -> str:
    import anthropic

    client = client or anthropic.Anthropic()
    model = config["ai"]["model"]
    request = dict(
        model=model,
        max_tokens=8000,
        system=_instructions(config),
        messages=[
            {
                "role": "user",
                "content": [_image_block(p) for p in images]
                + [{"type": "text", "text": "Write the caption for this post."}],
            }
        ],
    )
    try:
        if model.startswith(_FALLBACK_MODELS):
            # If the model declines, the API retries on a fallback model itself.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
            )
        else:
            response = client.messages.create(**request)
    except anthropic.AuthenticationError as exc:
        raise AIError("ANTHROPIC_API_KEY was rejected") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Claude API rate limit reached; will retry next run") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError(f"could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise AIError("Claude declined to caption this image")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise AIError(f"Claude returned no caption (stop reason: {response.stop_reason})")
    return text
