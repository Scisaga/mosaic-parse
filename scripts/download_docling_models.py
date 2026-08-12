#!/usr/bin/env python3
"""Prefetch Docling model artifacts for offline or reproducible deployments."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("models/docling"),
        help="Target artifact directory (default: models/docling)",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model group accepted by docling-tools; may be repeated",
    )
    parser.add_argument("--all", action="store_true", help="Download every Docling model group")
    parser.add_argument("--force", action="store_true", help="Redownload existing artifacts")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.models:
        raise SystemExit("--all and --model are mutually exclusive")

    executable = shutil.which("docling-tools")
    if executable is None:
        raise SystemExit("docling-tools is not installed; run `uv sync --frozen` first")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    command = [executable, "models", "download", "--output-dir", str(args.output_dir)]
    if args.all:
        command.append("--all")
    else:
        # Standard PDF conversion needs layout and table structure models.
        command.extend(args.models or ["layout", "tableformer"])
    if args.force:
        command.append("--force")

    subprocess.run(command, check=True)
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
