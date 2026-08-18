#!/usr/bin/env python3
"""Export MosaicParse raster assets from the SVG composition master."""

from __future__ import annotations

import io
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "assets" / "mosaicparse-logo.svg"


def png(size: int) -> bytes:
    return cairosvg.svg2png(
        url=str(MASTER),
        output_width=size,
        output_height=size,
        # The composition intentionally retains the original raster artwork.
        unsafe=True,
    )


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def main() -> int:
    image_1024 = png(1024)
    image_256 = png(256)
    image_180 = png(180)
    image_64 = png(64)
    write(ROOT / "assets" / "mosaicparse-logo-1024.png", image_1024)
    write(ROOT / "assets" / "mosaicparse-logo-256.png", image_256)
    write(ROOT / "logo.png", image_1024)
    write(ROOT / "favicon.png", image_64)
    write(ROOT / "frontend" / "public" / "logo.png", image_256)
    write(ROOT / "frontend" / "public" / "favicon.png", image_64)
    write(ROOT / "frontend" / "public" / "apple-touch-icon.png", image_180)
    with Image.open(io.BytesIO(image_256)) as source:
        buffer = io.BytesIO()
        source.save(buffer, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        ico = buffer.getvalue()
    write(ROOT / "favicon.ico", ico)
    write(ROOT / "frontend" / "public" / "favicon.ico", ico)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
