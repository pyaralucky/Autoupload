"""Draw branded 4:5 graphic cards for text-led posts.

Three layouts:

* text card         label, headline, body
* myth/fact card    a struck-through myth above the fact
* image card        a picture on top with the label and headline below

Colours and the clinic name come from ``[brand]`` in ``autoupload.toml``.
"""

from __future__ import annotations

from pathlib import Path

WIDTH, HEIGHT = 1080, 1350
MARGIN = 96

_FONTS = {
    "bold": [
        "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf",
        "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "regular": [
        "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}


def _font(weight: str, size: int):
    from PIL import ImageFont

    for path in _FONTS[weight]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)  # Pillow's built-in scalable font


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words, line = paragraph.split(), ""
        for word in words:
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _fit(draw, text: str, weight: str, sizes, max_width: int, max_height: int, spacing=1.25):
    """Largest font from ``sizes`` whose wrapped text fits the box."""
    for size in sizes:
        font = _font(weight, size)
        lines = _wrap(draw, text, font, max_width)
        height = int(len(lines) * size * spacing)
        widest = max((draw.textlength(l, font=font) for l in lines), default=0)
        if height <= max_height and widest <= max_width:
            break
    return font, lines, int(size * spacing), height


def _block(draw, xy, lines, font, line_height, fill) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _pill(draw, xy, text: str, brand: dict) -> int:
    font = _font("bold", 30)
    x, y = xy
    text = text.upper()
    width = draw.textlength(text, font=font) + 56
    draw.rounded_rectangle((x, y, x + width, y + 60), radius=30, fill=brand["accent"])
    draw.text((x + 28, y + 30), text, font=font, fill=brand["background"], anchor="lm")
    return y + 60


def _footer(draw, brand: dict) -> None:
    y = HEIGHT - MARGIN
    draw.line((MARGIN, y - 52, WIDTH - MARGIN, y - 52), fill=brand["accent"], width=3)
    draw.text((MARGIN, y), brand["name"], font=_font("bold", 32), fill=brand["text"], anchor="ls")
    if brand["handle"]:
        draw.text(
            (WIDTH - MARGIN, y), brand["handle"], font=_font("regular", 30),
            fill=brand["muted"], anchor="rs",
        )


def render(
    out: Path,
    brand: dict,
    *,
    label: str,
    title: str = "",
    body: str = "",
    myth: str = "",
    fact: str = "",
    image: Path | None = None,
) -> Path:
    from PIL import Image, ImageDraw, ImageOps

    card = Image.new("RGB", (WIDTH, HEIGHT), brand["background"])
    draw = ImageDraw.Draw(card)
    inner = WIDTH - 2 * MARGIN
    bottom = HEIGHT - MARGIN - 100  # keep clear of the footer

    if image is not None:
        photo_h = 760
        with Image.open(image) as photo:
            photo = ImageOps.exif_transpose(photo).convert("RGB")
            card.paste(ImageOps.fit(photo, (WIDTH, photo_h), Image.LANCZOS), (0, 0))
        y = _pill(draw, (MARGIN, photo_h + 48), label, brand) + 36
        font, lines, lh, _ = _fit(draw, title, "bold", range(72, 40, -4), inner, bottom - y)
        _block(draw, (MARGIN, y), lines, font, lh, brand["text"])
    elif myth or fact:
        y = _pill(draw, (MARGIN, MARGIN), label, brand) + 70
        half = (bottom - y - 80) // 2
        draw.text((MARGIN, y), "MYTH", font=_font("bold", 34), fill=brand["muted"])
        font, lines, lh, height = _fit(draw, myth, "bold", range(64, 36, -4), inner, half - 60)
        top = y + 60
        _block(draw, (MARGIN, top), lines, font, lh, brand["muted"])
        for index, line in enumerate(lines):  # strike the myth through
            mid = top + index * lh + font.size * 0.62
            draw.line(
                (MARGIN, mid, MARGIN + draw.textlength(line, font=font), mid),
                fill=brand["accent"], width=5,
            )
        y = top + height + 70
        draw.text((MARGIN, y), "FACT", font=_font("bold", 34), fill=brand["accent"])
        font, lines, lh, _ = _fit(draw, fact, "bold", range(64, 36, -4), inner, bottom - y - 60)
        _block(draw, (MARGIN, y + 60), lines, font, lh, brand["text"])
    else:
        y = _pill(draw, (MARGIN, MARGIN), label, brand) + 80
        title_room = (bottom - y) * (0.5 if body else 1.0)
        font, lines, lh, height = _fit(draw, title, "bold", range(96, 48, -4), inner, title_room)
        y = _block(draw, (MARGIN, y), lines, font, lh, brand["text"]) + 48
        if body:
            font, lines, lh, _ = _fit(
                draw, body, "regular", range(46, 30, -2), inner, bottom - y, spacing=1.45
            )
            _block(draw, (MARGIN, y), lines, font, lh, brand["text"])

    _footer(draw, brand)
    out.parent.mkdir(parents=True, exist_ok=True)
    card.save(out, "JPEG", quality=92, optimize=True)
    return out
