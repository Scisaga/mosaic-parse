#!/usr/bin/env python3
"""Ask a running MosaicParse service to remove expired jobs."""

from __future__ import annotations

import argparse
import os

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:12303")
    parser.add_argument(
        "--admin-token",
        default=os.getenv("ADMIN_TOKEN"),
        help="Admin token (defaults to ADMIN_TOKEN environment variable)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.admin_token:
        raise SystemExit("provide --admin-token or set ADMIN_TOKEN")

    response = httpx.post(
        f"{args.base_url.rstrip('/')}/admin/cleanup",
        headers={"Authorization": f"Bearer {args.admin_token}"},
        timeout=args.timeout,
    )
    response.raise_for_status()
    print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
