# Test fixtures

Every file in this directory is generated from original test text by
[`scripts/generate_fixtures.py`](../../scripts/generate_fixtures.py). No company
report, article, logo, or other third-party copyrighted document is included.

| File | Purpose |
|---|---|
| `native-report.pdf` | One-page PDF with an extractable text layer and exact numbers |
| `scanned-report.pdf` | One-page image-only PDF for OCR routing |
| `mixed-report.pdf` | Native first page plus scanned second page |
| `multi-column-research.pdf` | Two text columns for reading-order checks |
| `table-report.pdf` | Ruled numeric table for table-structure checks |
| `sample-image.png` | Standalone image upload and OCR fixture |
| `natural-scene.{png,webp,bmp,tiff}` | Pure visual image routing in supported raster formats |
| `mixed-screenshot.png` | Text plus chart image for mixed routing |
| `embedded-image.docx` | DOCX body plus an embedded image |
| `embedded-image.pptx` | PPTX slide plus an embedded image |
| `embedded-video.pptx` | Embedded video and poster; video relationships must be ignored |
| `scene-switch.mp4` | Six-second, three-scene, silent video for deterministic keyframes |

Regenerate and validate:

```bash
uv run python scripts/generate_fixtures.py
uv run python scripts/generate_fixtures.py --check
```

The fixtures are deliberately tiny. They validate routing and contracts, not
model quality. GPU golden tests should use a separately governed private corpus.
