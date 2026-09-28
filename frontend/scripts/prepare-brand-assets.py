#!/usr/bin/env python3
"""Prepare AICC Builder web assets from the approved source logo.

BRIEF: Preserve the supplied violet/teal AICC Builder artwork. Produce a
square agent mark for compact headers and the full conversation-to-agent
symbol for larger centered placements, with the source white matte restored
to transparency for clean light/dark-theme compositing.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image

FRONTEND_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = FRONTEND_DIR / "brand"
OUTPUT_DIR = FRONTEND_DIR / "public" / "brand"

# Coordinates target the approved 1254 x 1254 source artwork.
AGENT_CROP = (590, 245, 1210, 865)
SYMBOL_CROP = (90, 245, 1205, 865)


def remove_white_matte(image: Image.Image) -> Image.Image:
    """Recover colored artwork while removing white/gray canvas and shadows."""
    rgba_pixels: list[tuple[int, int, int, int]] = []
    for red, green, blue in image.convert("RGB").getdata():
        chroma = max(red, green, blue) - min(red, green, blue)
        if chroma <= 6:
            rgba_pixels.append((0, 0, 0, 0))
            continue

        alpha = 255 if chroma >= 80 else round((chroma - 6) * 255 / (80 - 6))
        scale = 255 / alpha

        def unmatte(channel: int) -> int:
            return max(0, min(255, round(255 + (channel - 255) * scale)))

        rgba_pixels.append((unmatte(red), unmatte(green), unmatte(blue), alpha))

    transparent = Image.new("RGBA", image.size, (0, 0, 0, 0))
    transparent.putdata(rgba_pixels)
    return transparent


def render_crop(image: Image.Image, crop: tuple[int, int, int, int], size: tuple[int, int], output: Path) -> None:
    """Crop, remove the white matte, contain, and save an optimized PNG."""
    artwork = remove_white_matte(image.crop(crop))
    artwork.thumbnail(size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    x = (size[0] - artwork.width) // 2
    y = (size[1] - artwork.height) // 2
    canvas.alpha_composite(artwork, (x, y))
    canvas.save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to the approved square PNG source")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    image = Image.open(source)
    if image.size != (1254, 1254):
        raise ValueError(f"Expected a 1254 x 1254 source image, got {image.size}")

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, SOURCE_DIR / "aicc-builder-logo-source.png")

    render_crop(image, AGENT_CROP, (256, 256), OUTPUT_DIR / "aicc-builder-agent.png")
    render_crop(image, SYMBOL_CROP, (640, 356), OUTPUT_DIR / "aicc-builder-symbol.png")


if __name__ == "__main__":
    main()
