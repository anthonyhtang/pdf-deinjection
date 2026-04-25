from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ICON_SIZES = [16, 32, 48, 64, 128, 256]
SOURCE_ICON_NAME = "icon.png"
BACKGROUND_COLOR = "#1a2744"
DOCUMENT_COLOR = "#f7f8fb"
FOLD_COLOR = "#d8ddeb"
STROKE_COLOR = "#e8edf7"
TEXT_COLOR = "#ffffff"
SYRINGE_COLOR = "#ef6b4a"
SLASH_COLOR = "#ff5349"


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a bold-ish font when available and otherwise fall back safely."""

    for font_name in ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(font_name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_document(draw: ImageDraw.ImageDraw, size: int) -> None:
    """Draw the PDF document silhouette."""

    margin = size * 0.16
    right = size * 0.72
    bottom = size * 0.82
    fold = size * 0.14
    draw.rounded_rectangle(
        (margin, margin, right, bottom),
        radius=max(2, int(size * 0.04)),
        fill=DOCUMENT_COLOR,
        outline=STROKE_COLOR,
        width=max(1, size // 64),
    )
    draw.polygon(
        [
            (right - fold, margin),
            (right, margin),
            (right, margin + fold),
        ],
        fill=FOLD_COLOR,
        outline=STROKE_COLOR,
    )

    font = _load_font(max(8, int(size * 0.16)))
    text = "PDF"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = margin + ((right - margin) - text_width) / 2
    y = margin + ((bottom - margin) - text_height) / 2
    draw.text((x, y), text, font=font, fill=BACKGROUND_COLOR)


def _draw_syringe(draw: ImageDraw.ImageDraw, size: int) -> None:
    """Draw a small crossed-out syringe overlay."""

    body_width = size * 0.18
    body_height = size * 0.08
    x0 = size * 0.56
    y0 = size * 0.58
    x1 = x0 + body_width
    y1 = y0 + body_height
    line_width = max(2, size // 32)

    draw.rounded_rectangle((x0, y0, x1, y1), radius=max(2, int(size * 0.02)), fill=SYRINGE_COLOR)
    draw.line((x1, y0 + body_height / 2, x1 + size * 0.08, y0 + body_height / 2), fill=SYRINGE_COLOR, width=line_width)
    draw.line((x0 - size * 0.05, y0 + body_height / 2, x0, y0 + body_height / 2), fill=SYRINGE_COLOR, width=line_width)
    draw.line((x0 + body_width * 0.2, y0, x0 + body_width * 0.2, y0 - size * 0.05), fill=SYRINGE_COLOR, width=line_width)
    draw.line((x0 + body_width * 0.5, y0, x0 + body_width * 0.5, y0 - size * 0.05), fill=SYRINGE_COLOR, width=line_width)
    draw.line((x0 - size * 0.02, y1 + size * 0.12, x1 + size * 0.12, y0 - size * 0.04), fill=SLASH_COLOR, width=line_width)


def _render_icon(size: int) -> Image.Image:
    """Render one icon frame."""

    image = Image.new("RGBA", (size, size), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)
    _draw_document(draw, size)
    _draw_syringe(draw, size)
    return image


def _render_icon_from_source(source_image: Image.Image, size: int) -> Image.Image:
    """Resize the provided source art into one icon frame."""

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    resized = source_image.copy()
    resized.thumbnail((size, size), Image.Resampling.LANCZOS)
    x = (size - resized.width) // 2
    y = (size - resized.height) // 2
    canvas.alpha_composite(resized, (x, y))
    return canvas


def generate_icon(output_path: Path | None = None) -> Path:
    """Generate the multi-size application icon and return its path."""

    target_path = output_path or Path(__file__).with_name("icon.ico")
    source_path = Path(__file__).with_name(SOURCE_ICON_NAME)
    if source_path.exists():
        with Image.open(source_path) as source_image:
            prepared_source = source_image.convert("RGBA")
            frames = [_render_icon_from_source(prepared_source, size) for size in ICON_SIZES]
    else:
        frames = [_render_icon(size) for size in ICON_SIZES]
    frames[0].save(target_path, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    return target_path


if __name__ == "__main__":
    generated_path = generate_icon()
    print(f"Generated {generated_path}")