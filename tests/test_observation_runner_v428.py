from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os._walk_symlinks_as_files = False

from market_reviewer.cli import main
from market_reviewer.observation_coordinator import BLOCKED, READY, WAITING_FOR_M5_CLOSE
from market_reviewer.observation_runner import (
    RUNNER_ALREADY_ACTIVE,
    RunnerConfig,
    cadence_seconds,
    next_observation_number,
    observation_runner_status,
    run_observation_loop,
)
from market_reviewer.persistence import atomic_write_json


def write_json(path: Path, data: dict) -> None:
    atomic_write_json(path, data)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state() -> dict:
    return {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": {"BTC": {"previous_review_timestamp": 100}, "ETH": {"previous_review_timestamp": 100}}}


def research_store(*, latest_observation: int = 53, btc_episode: str = "BROKEN", eth_episode: str = "OPEN") -> dict:
    return {
        "schema": "missed-opportunity-tracker.v1",
        "generated_at": "fixed",
        "records": [
            {
                "tracker_id": "BTC-LONG-MOT-1787827800-f291e0be3b60",
                "symbol": "BTC",
                "direction": "LONG",
                "status": "ACTIVE",
                "episode_status": btc_episode,
                "converted_to_production": False,
                "snapshots": [{"observation_number": latest_observation - 1}, {"observation_number": latest_observation}],
                "outcomes": {"1H": {"horizon_status": "COMPLETE"}, "4H": {"horizon_status": "COMPLETE"}, "12H": {"horizon_status": "COMPLETE"}, "24H": {"horizon_status": "COMPLETE"}},
            },
            {
                "tracker_id": "ETH-LONG-MOT-1787827800-f291e0be3b60",
                "symbol": "ETH",
                "direction": "LONG",
                "status": "ACTIVE",
                "episode_status": eth_episode,
                "converted_to_production": False,
                "snapshots": [{"observation_number": latest_observation - 1}, {"observation_number": latest_observation}],
                "outcomes": {"1H": {"horizon_status": "COMPLETE"}, "4H": {"horizon_status": "COMPLETE"}, "12H": {"horizon_status": "COMPLETE"}, "24H": {"horizon_status": "COMPLETE"}},
            },
        ],
    }


def ready_preparation(root: Path, checkpoint: int = 1_000) -> dict:
    return {
        "status": READY,
        "canonical_checkpoint": checkpoint,
        "market_path": str(root / "artifact" / "market-data-v1.json"),
        "external_path": str(root / "artifact" / "external-market-evidence-v1.json"),
        "market_fetch_count": 1,
        "external_fetch_count": 1,
    }


def waiting_preparation(root: Path, checkpoint: int = 1_300) -> dict:
    return {
        "status": WAITING_FOR_M5_CLOSE,
        "reason": WAITING_FOR_M5_CLOSE,
        "next_required_checkpoint": checkpoint,
        "market_fetch_count": 1,
        "external_fetch_count": 1,
    }


def blocked_preparation(root: Path) -> dict:
    return {"status": BLOCKED, "reason": "TRANSIENT_DATA_BLOCK", "market_fetch_count": 1, "external_fetch_count": 1}


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


class FakeCoordinator:
    def __init__(self, root: Path, statuses: list[str]) -> None:
        self.root = root
        self.statuses = statuses
        self.calls = 0
        self.kwargs = []

    def __call__(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        status = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        if status == READY:
            return ready_preparation(self.root)
        if status == WAITING_FOR_M5_CLOSE:
            return waiting_preparation(self.root)
        return blocked_preparation(self.root)


class ObservationRunnerV428Tests(unittest.TestCase):
    def root(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        write_json(root / "reviews" / "thesis-baseline.json", state())
        write_json(root / "research" / "missed-opportunities.json", research_store())
        return root

    def config(self, root: Path, **kwargs) -> RunnerConfig:
        return RunnerConfig(
            output_dir=root / "artifact",
            state_path=root / "reviews" / "thesis-baseline.json",
            research_tracker_path=root / "research" / "missed-opportunities.json",
            liquidation_root=root / "artifact" / "liquidations",
            **kwargs,
        )

    def production(self, calls: list[int]):
        def execute(preparation, observation_number):
            calls.append(observation_number)
            return {"observation_number": observation_number, "production_hash": "prod", "market_path": preparation.get("market_path"), "reviews": {}, "opportunity_snapshots": {}, "external_evidence": {}}
        return execute

    def research(self, calls: list[int], fail: bool = False):
        def execute(payload):
            calls.append(int(payload["observation_number"]))
            if fail:
                return {"research_persistence": "FAIL", "message": "research failed"}
            return {"research_persistence": "PASS", "fresh_reload_match": True}
        return execute

    def test_default_weekday_cadence_is_60_minutes(self) -> None:
        self.assertEqual(cadence_seconds(1_787_827_800), 3_600)

    def test_default_weekend_cadence_is_90_minutes(self) -> None:
        self.assertEqual(cadence_seconds(1_787_974_500), 5_400)

    def test_cadence_override_works(self) -> None:
        self.assertEqual(cadence_seconds(1_787_974_500, weekday_minutes=10, weekend_minutes=11), 660)

    def test_weekend_calculation_is_deterministic(self) -> None:
        self.assertEqual(cadence_seconds(1_787_974_500), cadence_seconds(1_787_974_500))

    def test_ready_executes_exactly_one_observation(self) -> None:
        root = self.root()
        prod_calls: list[int] = []
        research_calls: list[int] = []
        result = run_observation_loop(self.config(root, max_cycles=1), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production(prod_calls), research_executor=self.research(research_calls), clock=Clock(1_000))
        self.assertEqual(prod_calls, [54])
        self.assertEqual(research_calls, [54])
        self.assertEqual(result["cycles"][0]["production_result"], "PASS")

    def test_waiting_does_not_execute_observation(self) -> None:
        root = self.root()
        prod_calls: list[int] = []
        result = run_observation_loop(self.config(root, max_cycles=1), coordinator=FakeCoordinator(root, [WAITING_FOR_M5_CLOSE]), production_executor=self.production(prod_calls), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(prod_calls, [])
        self.assertEqual(result["cycles"][0]["preparation_status"], WAITING_FOR_M5_CLOSE)

    def test_waiting_resumes_same_frozen_cycle(self) -> None:
        root = self.root()
        coordinator = FakeCoordinator(root, [WAITING_FOR_M5_CLOSE, READY])
        slept: list[float] = []
        clock = Clock(1_000)
        def sleep(seconds):
            slept.append(seconds)
            clock.advance(int(seconds))
        run_observation_loop(self.config(root, max_cycles=2), coordinator=coordinator, production_executor=self.production([]), research_executor=self.research([]), clock=clock, sleeper=sleep)
        self.assertEqual(coordinator.calls, 2)
        self.assertGreaterEqual(slept[0], 300)

    def test_blocked_does_not_mutate_production(self) -> None:
        root = self.root()
        before = sha(root / "reviews" / "thesis-baseline.json")
        run_observation_loop(self.config(root, max_cycles=1), coordinator=FakeCoordinator(root, [BLOCKED]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(before, sha(root / "reviews" / "thesis-baseline.json"))

    def test_blocked_does_not_mutate_research(self) -> None:
        root = self.root()
        before = sha(root / "research" / "missed-opportunities.json")
        run_observation_loop(self.config(root, max_cycles=1), coordinator=FakeCoordinator(root, [BLOCKED]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(before, sha(root / "research" / "missed-opportunities.json"))

    def test_single_runner_lock(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1)
        write_json(cfg.runner_state_path, {"schema": "observation-runner.v1", "pid": 999999, "status": "IDLE", "last_heartbeat_at": 1_000})
        with mock.patch("market_reviewer.observation_runner._pid_is_live", return_value=True):
            result = run_observation_loop(cfg, coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_100))
        self.assertEqual(result["status"], RUNNER_ALREADY_ACTIVE)

    def test_stale_runner_lock_recovery(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1, stale_lock_seconds=10)
        write_json(cfg.runner_state_path, {"schema": "observation-runner.v1", "pid": 999999, "status": "IDLE", "last_heartbeat_at": 1_000})
        with mock.patch("market_reviewer.observation_runner._pid_is_live", return_value=True):
            result = run_observation_loop(cfg, coordinator=FakeCoordinator(root, [BLOCKED]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(2_000))
        self.assertTrue(result["cycles"])

    def test_clean_ctrl_c_shutdown_semantics(self) -> None:
        root = self.root()
        def interrupt(**kwargs):
            raise KeyboardInterrupt()
        result = run_observation_loop(self.config(root, max_cycles=1), coordinator=interrupt, production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        status = observation_runner_status(self.config(root).runner_state_path, clock=Clock(1_000))
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(status["status"], "STOPPED")

    def test_heartbeat_updates(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1)
        run_observation_loop(cfg, coordinator=FakeCoordinator(root, [BLOCKED]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_234))
        status = observation_runner_status(cfg.runner_state_path, clock=Clock(1_235))
        self.assertEqual(status["last_heartbeat_at"], 1_234)

    def test_status_cli_read_only(self) -> None:
        root = self.root()
        cfg = self.config(root)
        write_json(cfg.runner_state_path, {"schema": "observation-runner.v1", "pid": 1, "status": "STOPPED"})
        before = sha(cfg.runner_state_path)
        self.assertEqual(main(["observation-runner-status", "--path", str(cfg.runner_state_path)]), 0)
        self.assertEqual(before, sha(cfg.runner_state_path))

    def test_next_observation_numbering_restart_safe(self) -> None:
        root = self.root()
        self.assertEqual(next_observation_number(root / "research" / "missed-opportunities.json"), 54)

    def test_no_duplicate_observation_after_restart_pending_research(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        payload = {"observation_number": 54, "production_hash": sha(cfg.state_path)}
        write_json(cfg.runner_state_path, {"schema": "observation-runner.v1", "pid": os.getpid(), "status": "PERSISTING", "pending_research": {"observation_number": 54, "payload": payload}})
        prod_calls: list[int] = []
        research_calls: list[int] = []
        run_observation_loop(cfg, coordinator=FakeCoordinator(root, [READY]), production_executor=self.production(prod_calls), research_executor=self.research(research_calls), clock=Clock(1_000))
        self.assertEqual(prod_calls, [])
        self.assertEqual(research_calls, [54])

    def test_production_persist_research_failure_recovery(self) -> None:
        root = self.root()
        prod_calls: list[int] = []
        try:
            run_observation_loop(self.config(root, max_cycles=1), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production(prod_calls), research_executor=self.research([], fail=True), clock=Clock(1_000))
        except ValueError:
            pass
        state = json.loads((root / "artifact" / "observation-runner.json").read_text(encoding="utf-8"))
        self.assertIn("pending_research", state)
        self.assertEqual(prod_calls, [54])

    def test_coordinator_external_fetch_max_preserved(self) -> None:
        root = self.root()
        result = run_observation_loop(self.config(root, max_cycles=1, dry_run=True), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(result["cycles"][0]["preparation_status"], READY)

    def test_coordinator_market_fetch_max_preserved(self) -> None:
        root = self.root()
        coordinator = FakeCoordinator(root, [WAITING_FOR_M5_CLOSE, READY])
        run_observation_loop(self.config(root, max_cycles=2, dry_run=True), coordinator=coordinator, production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000), sleeper=lambda seconds: None)
        self.assertEqual(coordinator.calls, 2)

    def test_btc_broken_tracker_not_appended_by_dry_run(self) -> None:
        root = self.root()
        before = copy.deepcopy(json.loads((root / "research" / "missed-opportunities.json").read_text(encoding="utf-8")))
        run_observation_loop(self.config(root, max_cycles=1, dry_run=True), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(before, json.loads((root / "research" / "missed-opportunities.json").read_text(encoding="utf-8")))

    def test_eth_open_tracker_behavior_preserved_by_dry_run(self) -> None:
        root = self.root()
        before = json.loads((root / "research" / "missed-opportunities.json").read_text(encoding="utf-8"))["records"][1]
        run_observation_loop(self.config(root, max_cycles=1, dry_run=True), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        after = json.loads((root / "research" / "missed-opportunities.json").read_text(encoding="utf-8"))["records"][1]
        self.assertEqual(before, after)

    def test_completed_outcomes_remain_immutable_in_dry_run(self) -> None:
        root = self.root()
        before = json.loads((root / "research" / "missed-opportunities.json").read_text(encoding="utf-8"))["records"][0]["outcomes"]
        run_observation_loop(self.config(root, max_cycles=1, dry_run=True), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        after = json.loads((root / "research" / "missed-opportunities.json").read_text(encoding="utf-8"))["records"][0]["outcomes"]
        self.assertEqual(before, after)

    def test_production_hash_unchanged_in_dry_run(self) -> None:
        root = self.root()
        before = sha(root / "reviews" / "thesis-baseline.json")
        run_observation_loop(self.config(root, max_cycles=1, dry_run=True), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(before, sha(root / "reviews" / "thesis-baseline.json"))

    def test_research_hash_unchanged_in_dry_run(self) -> None:
        root = self.root()
        before = sha(root / "research" / "missed-opportunities.json")
        run_observation_loop(self.config(root, max_cycles=1, dry_run=True), coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertEqual(before, sha(root / "research" / "missed-opportunities.json"))

    def test_max_cycles_exits_deterministically(self) -> None:
        root = self.root()
        result = run_observation_loop(self.config(root, max_cycles=2, dry_run=True), coordinator=FakeCoordinator(root, [BLOCKED, BLOCKED]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000), sleeper=lambda seconds: None)
        self.assertEqual(len(result["cycles"]), 2)

    def test_no_busy_loop_waiting_schedules_future_retry(self) -> None:
        root = self.root()
        result = run_observation_loop(self.config(root, max_cycles=1), coordinator=FakeCoordinator(root, [WAITING_FOR_M5_CLOSE]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertGreater(result["cycles"][0]["next_scheduled_run"], 1_000)

    def test_runner_runtime_state_written_under_artifact(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1, dry_run=True)
        run_observation_loop(cfg, coordinator=FakeCoordinator(root, [READY]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000))
        self.assertTrue(cfg.runner_state_path.exists())

    def test_runner_log_is_append_only_runtime_file(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=2, dry_run=True)
        run_observation_loop(cfg, coordinator=FakeCoordinator(root, [BLOCKED, BLOCKED]), production_executor=self.production([]), research_executor=self.research([]), clock=Clock(1_000), sleeper=lambda seconds: None)
        self.assertEqual(len(cfg.runner_log_path.read_text(encoding="utf-8").splitlines()), 2)
