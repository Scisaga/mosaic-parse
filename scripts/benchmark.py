#!/usr/bin/env python3
"""Run a small HTTP benchmark against the synchronous parse endpoint."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF or image files")
    parser.add_argument("--base-url", default="http://127.0.0.1:12303")
    parser.add_argument("--api-key")
    parser.add_argument("--mode", choices=("auto", "standard", "ocr", "vlm"), default="auto")
    parser.add_argument("--profile", choices=("fast", "balanced", "accurate"), default="balanced")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def run_one(client: httpx.Client, path: Path, args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    with path.open("rb") as source:
        response = client.post(
            "/v1/documents/parse",
            files={"file": (path.name, source, "application/octet-stream")},
            data={
                "mode": args.mode,
                "profile": args.profile,
                "output_format": "markdown",
                "include_diagnostics": "true",
            },
        )
    elapsed = time.perf_counter() - started
    record: dict[str, Any] = {
        "file": str(path),
        "input_bytes": path.stat().st_size,
        "http_status": response.status_code,
        "duration_seconds": round(elapsed, 4),
    }
    try:
        payload = response.json()
    except ValueError:
        payload = {"body": response.text[:500]}
    record["response"] = payload
    return record


def main() -> int:
    args = parse_args()
    missing = [str(path) for path in args.inputs if not path.is_file()]
    if missing:
        raise SystemExit(f"input files do not exist: {', '.join(missing)}")

    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    with httpx.Client(
        base_url=args.base_url.rstrip("/"), headers=headers, timeout=args.timeout
    ) as client:
        records = [run_one(client, path, args) for path in args.inputs]

    durations = [float(record["duration_seconds"]) for record in records]
    report = {
        "mode": args.mode,
        "profile": args.profile,
        "requests": records,
        "summary": {
            "count": len(records),
            "successful": sum(200 <= int(r["http_status"]) < 300 for r in records),
            "mean_seconds": round(statistics.fmean(durations), 4),
            "max_seconds": max(durations),
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 0 if report["summary"]["successful"] == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
