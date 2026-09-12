"""Render readable key labels and user icons to device-sized PNGs."""
from hashlib import sha256
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def font(size):
    for path in ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/noto/NotoSans-Regular.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def tile_image(item, size=(112, 112), detail=""):
    canvas = Image.new("RGB", size, "#111d2b")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((1, 1, size[0]-2, size[1]-2), radius=13, fill=item.get("color", "#247a89"))
    source = item.get("image")
    if source:
        try:
            with Image.open(Path(source).expanduser()) as original:
                icon = ImageOps.fit(original.convert("RGBA"), (size[0]-12, size[1]-37))
                canvas.paste(icon, (6, 5), icon)
        except Image.DecompressionBombError as exc:
            raise ValueError("Image is too large; choose a smaller image") from exc
        draw.rectangle((4, size[1]-35, size[0]-4, size[1]-4), fill="#111d2b")
    label = item.get("label", "Empty")
    lines = []
    current = ""
    text_font = font(14)
    for word in label.split():
        candidate = (current + " " + word).strip()
        if draw.textlength(candidate, font=text_font) > size[0]-14 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    lines = lines[:2]
    for index, line in enumerate(lines):
        while draw.textlength(line, font=text_font) > size[0]-12 and len(line) > 1:
            line = line[:-2] + "…"
        y = size[1]-32 if source else (25 if detail else 40)
        draw.text((size[0]/2, y + index*18), line, anchor="mt", font=text_font, fill="white")
    if detail and not source:
        detail_font = font(23 if len(detail) < 9 else 14)
        draw.text((size[0]/2, 77), detail[:20], anchor="mm", font=detail_font, fill="white")
    return canvas


def render_tile(item, cache, key, detail=""):
    size = (176, 112) if 11 <= key <= 14 else (112, 112)
    canvas = tile_image(item, size, detail)
    digest = sha256(canvas.tobytes()).hexdigest()[:20]
    path = Path(cache) / f"{key}-{digest}.png"
    if not path.exists():
        canvas.save(path)
    return str(path)
