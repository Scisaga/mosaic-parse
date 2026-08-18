#!/usr/bin/env python3
"""Generate the application OpenAPI schema and perform a minimal contract check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REQUIRED_PATHS = {
    "/health",
    "/ready",
    "/v1/backends",
    "/v1/content/parse",
    "/v1/content/jobs",
    "/v1/content/jobs/{job_id}",
    "/v1/content/jobs/{job_id}/events",
    "/v1/content/jobs/{job_id}/result",
    "/v1/content/jobs/{job_id}/rendering/{format}",
    "/v1/content/jobs/{job_id}/assets",
    "/v1/content/jobs/{job_id}/assets/{asset_id}",
    "/v1/content/jobs/{job_id}/bundle",
    "/v1/content/jobs/{job_id}/retry",
    "/admin/reload",
    "/admin/cleanup",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")
    parser.add_argument("--check", action="store_true", help="Only validate the generated contract")
    return parser.parse_args()


def validate(schema: dict[str, Any]) -> None:
    if schema.get("openapi") is None or schema.get("info", {}).get("title") is None:
        raise SystemExit("generated document is not an OpenAPI schema")
    paths = set(schema.get("paths", {}))
    if any(path.startswith("/v1/documents") for path in paths):
        raise SystemExit("OpenAPI still exposes the removed /v1/documents contract")
    missing = REQUIRED_PATHS - paths
    if missing:
        raise SystemExit(f"OpenAPI schema is missing required paths: {sorted(missing)}")
    if schema.get("info", {}).get("title") != "MosaicParse":
        raise SystemExit("OpenAPI title is not MosaicParse")


def main() -> int:
    args = parse_args()
    from app.main import app

    schema = app.openapi()
    validate(schema)
    if args.check:
        print(f"OpenAPI {schema['openapi']}: {len(schema['paths'])} paths validated")
        return 0

    rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
