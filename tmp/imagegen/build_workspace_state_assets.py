from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "tmp/imagegen/workspace-states"
OUTPUT_DIR = ROOT / "frontend/public/illustrations"
SCENES = ("input", "markdown", "text", "pages", "loading", "info", "error")


def fit_scene(source: Image.Image, *, width: int = 640, height: int = 512) -> Image.Image:
    rgba = source.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        raise RuntimeError("Generated workspace illustration has no visible pixels")

    crop = rgba.crop(bbox)
    available_width = width - 52
    available_height = height - 42
    scale = min(available_width / crop.width, available_height / crop.height)
    resized = crop.resize(
        (round(crop.width * scale), round(crop.height * scale)),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(
        resized,
        ((width - resized.width) // 2, (height - resized.height) // 2),
    )
    return canvas


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for scene in SCENES:
        source = SOURCE_DIR / f"{scene}-alpha.png"
        target = OUTPUT_DIR / f"workspace-{scene}.png"
        fit_scene(Image.open(source)).save(target, format="PNG", optimize=True)


if __name__ == "__main__":
    main()
