# Benchmark guide

Parsing quality is evaluated semantically, not by exact whole-document string
equality. Docling and model upgrades can make harmless Markdown formatting
changes.

## Corpus policy

The committed fixtures are small, original documents and are suitable for CI.
Real GPU regression tests should use an access-controlled corpus whose license,
retention, and confidentiality are documented. Do not commit customer reports
or copyrighted research PDFs.

## Metrics

Record at least:

- document and page success rates;
- exact preservation of numbers, signs, decimal places, and units;
- heading/paragraph reading order;
- table cell and row/column structure;
- empty-page and repeated-text rates;
- mean and p95 seconds per page;
- peak parser RAM;
- GLM call count and VLM fallback rate.

Unknown route counts are not zeros. Report them as unavailable when the parser
or upstream component cannot observe them reliably.

## HTTP smoke benchmark

Start the service, then run:

```bash
uv run python scripts/benchmark.py \
  tests/fixtures/native-report.pdf \
  tests/fixtures/table-report.pdf \
  --mode standard \
  --json-output benchmark-results/standard.json
```

For the GPU path:

```bash
uv run python scripts/benchmark.py \
  tests/fixtures/scanned-report.pdf \
  tests/fixtures/sample-image.png \
  --mode ocr \
  --json-output benchmark-results/glm-ocr.json
```

The helper records transport status and elapsed time. It is intentionally not a
load generator. Use a controlled tool and fixed concurrency for capacity tests.

## Golden assertions

Prefer focused assertions such as:

- native fixture contains `12,345.67` and `18.25%`;
- mixed fixture preserves the native first page and obtains non-empty OCR text
  for the second page;
- column headings precede their respective bodies;
- table output contains all expected cells in row order;
- no unexpected Unicode replacement characters or repeated short blocks.

Record parser version, Docling/plugin versions, model identity/revision, mode,
profile, hardware, and all non-secret tuning values with every comparison.

