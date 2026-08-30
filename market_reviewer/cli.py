"""Command line interface for the standalone reviewer."""

from __future__ import annotations

import argparse
from pathlib import Path

from .external import (
    FETCH_MODE_BOOTSTRAP,
    FETCH_MODE_PRODUCTION_REPLAY,
    ReplayHistoryTooOld,
    ReplayStateUnavailableForFetch,
    run_external_fetch,
)
from .external_evidence_providers import run_external_evidence_fetch
from .liquidation_collector import collect_liquidations, liquidation_status
from .missed_opportunity_live import explicit_backfill_v426, missed_opportunity_status
from .observation_coordinator import prepare_observation
from .observation_runner import RunnerConfig, observation_runner_status, run_observation_loop
from .review_only import run_review_only


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-reviewer")
    subparsers = parser.add_subparsers(dest="command")
    review = subparsers.add_parser("review-external", help="Review an existing DATA_READY snapshot")
    review.add_argument("snapshot", help="Path to market-data.v1 JSON artifact")
    review.add_argument("--thesis", help="Optional previous thesis JSON path")
    fetch = subparsers.add_parser("fetch-external", help="Fetch BTC/ETH market-data.v1 artifact")
    fetch.add_argument("--output-dir", default="artifact", help="Directory for market-data-v1.json")
    fetch.add_argument("--state", help="Review-state JSON path required for production replay fetch")
    fetch.add_argument(
        "--mode",
        choices=(FETCH_MODE_BOOTSTRAP, FETCH_MODE_PRODUCTION_REPLAY),
        default=FETCH_MODE_BOOTSTRAP,
        help="Fetch contract: bootstrap allows no state; production-replay requires replay state and fails closed",
    )
    fetch.add_argument("--research-store", default="research/missed-opportunities.json", help="Optional research tracker store used only to extend outcome coverage depth")
    evidence = subparsers.add_parser("fetch-external-evidence", help="Fetch research-only external-market-evidence.v1 artifact")
    evidence.add_argument("--output-dir", default="artifact", help="Directory for external-market-evidence-v1.json")
    liquidations = subparsers.add_parser("collect-liquidations", help="Collect research-only liquidation stream events")
    liquidations.add_argument("--root", default="artifact/liquidations", help="Directory for liquidation event store")
    liquidations.add_argument("--duration", type=int, default=None, help="Optional bounded collection duration in seconds")
    liq_status = subparsers.add_parser("liquidation-status", help="Show research-only liquidation collector status")
    liq_status.add_argument("--root", default="artifact/liquidations", help="Directory for liquidation event store")
    mot_backfill = subparsers.add_parser("backfill-missed-opportunities-v426", help="Explicitly seed validated #46-#49 missed-opportunity trackers")
    mot_backfill.add_argument("--path", default="research/missed-opportunities.json", help="Research tracker store path")
    mot_status = subparsers.add_parser("missed-opportunity-status", help="Show research-only missed opportunity tracker status")
    mot_status.add_argument("--path", default="research/missed-opportunities.json", help="Research tracker store path")
    prepare = subparsers.add_parser("prepare-observation", help="Coordinate market/external evidence readiness without running review")
    prepare.add_argument("--output-dir", default="artifact", help="Directory for runtime artifacts")
    prepare.add_argument("--state", default="reviews/thesis-baseline.json", help="Production review-state JSON path")
    prepare.add_argument("--research-store", default="research/missed-opportunities.json", help="Research tracker store path")
    prepare.add_argument("--liquidations", default="artifact/liquidations", help="Liquidation collector store root")
    runner = subparsers.add_parser("run-observation-loop", help="Run the automatic observation loop")
    runner.add_argument("--output-dir", default="artifact", help="Directory for runtime artifacts")
    runner.add_argument("--state", default="reviews/thesis-baseline.json", help="Production review-state JSON path")
    runner.add_argument("--research-store", default="research/missed-opportunities.json", help="Research tracker store path")
    runner.add_argument("--liquidations", default="artifact/liquidations", help="Liquidation collector store root")
    runner.add_argument("--weekday-interval-minutes", type=int, default=60, help="Weekday observation cadence")
    runner.add_argument("--weekend-interval-minutes", type=int, default=90, help="Weekend observation cadence")
    runner.add_argument("--max-cycles", type=int, default=None, help="Optional deterministic cycle limit")
    runner.add_argument("--dry-run", action="store_true", help="Prepare only; never persist a formal Observation")
    runner_status = subparsers.add_parser("observation-runner-status", help="Show automatic observation runner status")
    runner_status.add_argument("--path", default="artifact/observation-runner.json", help="Runner state path")
    runner_status.add_argument("--state", default="reviews/thesis-baseline.json", help="Production review-state JSON path")
    runner_status.add_argument("--research-store", default="research/missed-opportunities.json", help="Research tracker store path")
    runner_status.add_argument("--journal", default=None, help="Observation commit journal path")
    runner_status.add_argument("--production-head", default=None, help="Production observation head path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "review-external":
        print(run_review_only(args.snapshot, args.thesis))
        return 0
    if args.command == "fetch-external":
        try:
            state_path = Path(args.state) if args.state else None
            research_path = Path(args.research_store) if args.research_store else None
            path = run_external_fetch(Path(args.output_dir), state_path=state_path, mode=args.mode, research_tracker_path=research_path)
        except (ReplayStateUnavailableForFetch, ReplayHistoryTooOld) as exc:
            print(str(exc))
            return 2
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
    if args.command == "backfill-missed-opportunities-v426":
        import json
        print(json.dumps(explicit_backfill_v426(Path(args.path)), indent=2, sort_keys=True))
        return 0
    if args.command == "missed-opportunity-status":
        import json
        print(json.dumps(missed_opportunity_status(Path(args.path)), indent=2, sort_keys=True))
        return 0
    if args.command == "prepare-observation":
        import json
        print(json.dumps(
            prepare_observation(
                output_dir=Path(args.output_dir),
                state_path=Path(args.state),
                research_tracker_path=Path(args.research_store),
                liquidation_root=Path(args.liquidations),
            ),
            indent=2,
            sort_keys=True,
        ))
        return 0
    if args.command == "run-observation-loop":
        import json
        result = run_observation_loop(
            RunnerConfig(
                output_dir=Path(args.output_dir),
                state_path=Path(args.state),
                research_tracker_path=Path(args.research_store),
                liquidation_root=Path(args.liquidations),
                weekday_interval_minutes=args.weekday_interval_minutes,
                weekend_interval_minutes=args.weekend_interval_minutes,
                max_cycles=args.max_cycles,
                dry_run=args.dry_run,
            )
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") != "RUNNER_ALREADY_ACTIVE" else 2
    if args.command == "observation-runner-status":
        import json
        print(json.dumps(observation_runner_status(Path(args.path), state_path=Path(args.state), research_tracker_path=Path(args.research_store), journal_path=Path(args.journal) if args.journal else None, production_head_path=Path(args.production_head) if args.production_head else None), indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
