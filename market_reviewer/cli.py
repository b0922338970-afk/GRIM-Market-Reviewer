"""Command line interface for the standalone reviewer."""

from __future__ import annotations

import argparse
from pathlib import Path

from .external import run_external_fetch
from .review_only import run_review_only


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-reviewer")
    subparsers = parser.add_subparsers(dest="command")
    review = subparsers.add_parser("review-external", help="Review an existing DATA_READY snapshot")
    review.add_argument("snapshot", help="Path to market-data.v1 JSON artifact")
    review.add_argument("--thesis", help="Optional previous thesis JSON path")
    fetch = subparsers.add_parser("fetch-external", help="Fetch BTC/ETH market-data.v1 artifact")
    fetch.add_argument("--output-dir", default="artifact", help="Directory for market-data-v1.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "review-external":
        print(run_review_only(args.snapshot, args.thesis))
        return 0
    if args.command == "fetch-external":
        path = run_external_fetch(Path(args.output_dir))
        print(path)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
