from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tmp/imagegen/logo-copybot-v4-transparent.png"


def square_master(source: Image.Image, size: int = 1024, margin: int = 28) -> Image.Image:
    rgba = source.convert("RGBA")
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        raise RuntimeError("Logo source has no visible pixels")
    crop = rgba.crop(bbox)
    target = size - (margin * 2)
    scale = min(target / crop.width, target / crop.height)
    resized = crop.resize(
        (round(crop.width * scale), round(crop.height * scale)),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(
        resized,
        ((size - resized.width) // 2, (size - resized.height) // 2),
    )
    return canvas


def save_png(master: Image.Image, path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    master.resize((size, size), Image.Resampling.LANCZOS).save(
        path,
        format="PNG",
        optimize=True,
    )


def main() -> None:
    master = square_master(Image.open(SOURCE))
    save_png(master, ROOT / "logo.png", 1024)
    save_png(master, ROOT / "frontend/public/logo.png", 256)
    save_png(master, ROOT / "favicon.png", 64)
    save_png(master, ROOT / "frontend/public/favicon.png", 64)
    save_png(master, ROOT / "frontend/public/apple-touch-icon.png", 180)

    for target in (ROOT / "favicon.ico", ROOT / "frontend/public/favicon.ico"):
        target.parent.mkdir(parents=True, exist_ok=True)
        master.save(
            target,
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
        )


if __name__ == "__main__":
    main()
