#!/usr/bin/env python3
"""Run a private, manifest-driven PDF quality regression without storing document text."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.services.evidence_service import date_tokens, number_tokens


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _table_shapes(markdown: str) -> list[tuple[int, int]]:
    tables: list[list[str]] = []
    current: list[str] = []
    for line in markdown.splitlines():
        if line.strip().startswith("|") and line.count("|") >= 2:
            current.append(line.strip())
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    shapes: list[tuple[int, int]] = []
    for rows in tables:
        columns = rows[0].count("|") - 1
        data_rows = [
            row for row in rows if not set(row.replace("|", "").strip()) <= {"-", ":", " "}
        ]
        shapes.append((max(0, len(data_rows) - 1), columns))
    return shapes


def _check_scope(label: str, content: str, expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for anchor in expected.get("text_anchors", []):
        if anchor not in content:
            failures.append(f"{label}: missing text anchor {anchor!r}")
    for anchor in expected.get("forbidden_anchors", []):
        if anchor in content:
            failures.append(f"{label}: forbidden anchor present {anchor!r}")
    ordered = expected.get("ordered_anchors", [])
    positions = [content.find(anchor) for anchor in ordered]
    if ordered and (any(position < 0 for position in positions) or positions != sorted(positions)):
        failures.append(f"{label}: ordered anchors are absent or inverted")
    if "dates" in expected and sorted(date_tokens(content)) != sorted(expected["dates"]):
        failures.append(f"{label}: date set mismatch")
    available_numbers = set(number_tokens(content))
    for number in expected.get("numeric_anchors", []):
        if number not in available_numbers:
            failures.append(f"{label}: missing numeric anchor {number!r}")
    if "minimum_characters" in expected and len(content.strip()) < int(
        expected["minimum_characters"]
    ):
        failures.append(f"{label}: output is shorter than minimum_characters")
    if "maximum_characters" in expected and len(content.strip()) > int(
        expected["maximum_characters"]
    ):
        failures.append(f"{label}: output exceeds maximum_characters")
    shapes = _table_shapes(content)
    for shape in expected.get("table_shapes", []):
        required = (int(shape["rows"]), int(shape["columns"]))
        if required not in shapes:
            failures.append(f"{label}: missing table shape {required[0]}x{required[1]}")
    return failures


def evaluate_case(client: httpx.Client, root: Path, case: dict[str, Any]) -> dict[str, Any]:
    source = (root / case["file"]).resolve()
    failures: list[str] = []
    if not source.is_file():
        return {"id": case["id"], "passed": False, "failures": ["source file is missing"]}
    actual_sha = _sha256(source)
    if actual_sha != case["sha256"]:
        return {"id": case["id"], "passed": False, "failures": ["SHA-256 mismatch"]}
    form = {
        "profile": case.get("profile", "balanced"),
        "unit_range": case.get("unit_range", ""),
        "language": case.get("language", "zh,en"),
        "include_renderings": "true",
    }
    started = time.perf_counter()
    with source.open("rb") as handle:
        response = client.post(
            "/v1/content/parse",
            data=form,
            files={"file": (source.name, handle, "application/pdf")},
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if response.status_code != 200:
        return {
            "id": case["id"],
            "passed": False,
            "elapsed_ms": elapsed_ms,
            "failures": [f"HTTP {response.status_code}"],
        }
    payload = response.json()
    if elapsed_ms > int(case.get("max_duration_ms", 120_000)):
        failures.append("case exceeded max_duration_ms")
    failures.extend(
        _check_scope(
            "document",
            str((payload.get("renderings") or {}).get("markdown", "")),
            case.get("document", {}),
        )
    )
    pages = {int(page["page_number"]): page for page in payload.get("pages", [])}
    for expected in case.get("pages", []):
        page_number = int(expected["page_number"])
        page = pages.get(page_number)
        if page is None:
            failures.append(f"page {page_number}: missing page result")
            continue
        failures.extend(
            _check_scope(
                f"page {page_number}",
                str((page.get("renderings") or {}).get("markdown", "")),
                expected,
            )
        )
        diagnostics = page.get("diagnostics") or {}
        warning_codes = set(diagnostics.get("warning_codes", []))
        allowed = set(expected.get("allowed_warnings", []))
        unexpected = warning_codes - allowed
        if unexpected:
            failures.append(f"page {page_number}: unexpected warnings {sorted(unexpected)}")
        required = set(expected.get("required_warnings", []))
        if not required <= warning_codes:
            failures.append(
                f"page {page_number}: missing warnings {sorted(required - warning_codes)}"
            )
        for key in ("source_kind", "quality_verdict", "selected_strategy"):
            if key in expected and diagnostics.get(key) != expected[key]:
                failures.append(f"page {page_number}: {key} mismatch")
    summary = payload.get("diagnostics") or {}
    return {
        "id": case["id"],
        "passed": not failures,
        "elapsed_ms": elapsed_ms,
        "quality_counts": {
            key: summary.get(key)
            for key in (
                "trusted_pages",
                "degraded_pages",
                "untrusted_pages",
                "visual_pages",
                "unresolved_visual_conflicts",
            )
        }
        | {"qwen_calls": (payload.get("runtime") or {}).get("qwen_calls")},
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:12303")
    parser.add_argument("--api-key")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="run only the named case ID; may be supplied more than once",
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("version") != 2 or not isinstance(manifest.get("cases"), list):
        raise SystemExit("manifest must have version=2 and a cases list")
    cases = manifest["cases"]
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case.get("id") in requested]
        missing = requested - {str(case.get("id")) for case in cases}
        if missing:
            raise SystemExit(f"unknown case IDs: {sorted(missing)}")
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=180,
        trust_env=False,
    ) as client:
        results = [evaluate_case(client, args.manifest.parent, case) for case in cases]
    report = {"passed": all(item["passed"] for item in results), "cases": results}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
