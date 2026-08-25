"""Command line interface for the standalone reviewer."""

from __future__ import annotations

import argparse
from pathlib import Path

from .external import run_external_fetch
from .external_evidence_providers import run_external_evidence_fetch
from .liquidation_collector import collect_liquidations, liquidation_status
from .review_only import run_review_only


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-reviewer")
    subparsers = parser.add_subparsers(dest="command")
    review = subparsers.add_parser("review-external", help="Review an existing DATA_READY snapshot")
    review.add_argument("snapshot", help="Path to market-data.v1 JSON artifact")
    review.add_argument("--thesis", help="Optional previous thesis JSON path")
    fetch = subparsers.add_parser("fetch-external", help="Fetch BTC/ETH market-data.v1 artifact")
    fetch.add_argument("--output-dir", default="artifact", help="Directory for market-data-v1.json")
    evidence = subparsers.add_parser("fetch-external-evidence", help="Fetch research-only external-market-evidence.v1 artifact")
    evidence.add_argument("--output-dir", default="artifact", help="Directory for external-market-evidence-v1.json")
    liquidations = subparsers.add_parser("collect-liquidations", help="Collect research-only liquidation stream events")
    liquidations.add_argument("--root", default="artifact/liquidations", help="Directory for liquidation event store")
    liquidations.add_argument("--duration", type=int, default=30, help="Collection duration in seconds")
    liq_status = subparsers.add_parser("liquidation-status", help="Show research-only liquidation collector status")
    liq_status.add_argument("--root", default="artifact/liquidations", help="Directory for liquidation event store")
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
    if args.command == "fetch-external-evidence":
        path = run_external_evidence_fetch(Path(args.output_dir))
        print(path)
        return 0
    if args.command == "collect-liquidations":
        import json
        print(json.dumps(collect_liquidations(Path(args.root), args.duration), indent=2, sort_keys=True))
        return 0
    if args.command == "liquidation-status":
        import json
        print(json.dumps(liquidation_status(Path(args.root)), indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
