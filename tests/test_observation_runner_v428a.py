from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os._walk_symlinks_as_files = False

from market_reviewer.observation_coordinator import READY
from market_reviewer.observation_runner import (
    BLOCKED_INVALID_OBSERVATION_STATE,
    BLOCKED_RECOVERY_EVIDENCE_MISSING,
    BLOCKED_RECOVERY_GAP,
    BLOCKED_RECOVERY_METADATA_MISSING,
    BLOCKED_RESEARCH_IDENTITY_MISMATCH,
    BLOCKED_STALE_PREPARED_BASELINE,
    PENDING_RESEARCH_RECOVERY,
    RunnerConfig,
    latest_production_observation,
    next_observation_number,
    observation_runner_status,
    run_observation_loop,
)
from market_reviewer.persistence import atomic_write_json


def write_json(path: Path, data: dict) -> None:
    atomic_write_json(path, data)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_head(path: Path, observation: int, state_hash: str, checkpoint: int = 5400) -> None:
    write_json(path, {
        "schema": "production-observation-head.v1",
        "observation_number": observation,
        "canonical_checkpoint": checkpoint,
        "production_previous_review_timestamp": {"BTC": checkpoint, "ETH": checkpoint},
        "production_state_sha256": state_hash,
        "cycle_id": f"cycle-{observation}",
        "committed_at": checkpoint,
    })


def production_state(timestamp: int = 5300) -> dict:
    return {
        "persistence_version": 2,
        "state_schema": "review-state.v2",
        "symbols": {
            "BTC": {"previous_review_timestamp": timestamp, "sequence_id": "BTC-seq", "sequence_state": "INVALIDATED", "previous_state": "NO_TRADE"},
            "ETH": {"previous_review_timestamp": timestamp, "sequence_id": "ETH-seq", "sequence_state": "RETEST_PENDING", "previous_state": "WATCH"},
        },
    }


def research_store(latest: int = 53, timestamp: int | None = None) -> dict:
    snapshot_timestamp = timestamp if timestamp is not None else (5300 if latest == 53 else latest * 100)
    return {
        "schema": "missed-opportunity-tracker.v1",
        "records": [
            {"tracker_id": "BTC", "symbol": "BTC", "direction": "LONG", "status": "DETERIORATING", "episode_status": "OPEN", "snapshots": [{"observation_number": latest, "snapshot_timestamp": snapshot_timestamp}], "outcomes": {}},
            {"tracker_id": "ETH", "symbol": "ETH", "direction": "LONG", "status": "DETERIORATING", "episode_status": "OPEN", "snapshots": [{"observation_number": latest, "snapshot_timestamp": snapshot_timestamp}], "outcomes": {}},
        ],
    }


def append_research(path: Path, observation_number: int) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for record in data.get("records", []):
        existing = {item.get("observation_number") for item in record.get("snapshots", [])}
        if observation_number not in existing:
            record.setdefault("snapshots", []).append({"observation_number": observation_number, "snapshot_timestamp": observation_number * 100})
    write_json(path, data)


def ready(root: Path) -> dict:
    return {
        "status": READY,
        "canonical_checkpoint": 5400,
        "market_path": str(root / "artifact" / "market-data-v1.json"),
        "external_path": str(root / "artifact" / "external-market-evidence-v1.json"),
    }


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class V428aSplitRecoveryTests(unittest.TestCase):
    def root(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        write_json(root / "reviews" / "thesis-baseline.json", production_state())
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

    def research(self, cfg: RunnerConfig, calls: list[int], fail: bool = False):
        def execute(payload):
            observation = int(payload["observation_number"])
            calls.append(observation)
            if fail:
                return {"research_persistence": "FAIL", "message": "boom"}
            append_research(cfg.research_tracker_path, observation)
            return {"research_persistence": "PASS", "fresh_reload_match": True}
        return execute

    def production(self, cfg: RunnerConfig, calls: list[int]):
        def execute(preparation, observation_number):
            calls.append(observation_number)
            journal = json.loads(cfg.commit_journal_path.read_text(encoding="utf-8"))
            self.assertEqual(journal["transactions"][-1]["status"], "PRODUCTION_COMMITTING")
            write_json(cfg.state_path, production_state(timestamp=5400))
            return {
                "observation_number": observation_number,
                "production_hash": sha(cfg.state_path),
                "market_path": preparation.get("market_path"),
                "external_path": preparation.get("external_path"),
                "reviews": {},
                "opportunity_snapshots": {},
                "external_evidence": {},
            }
        return execute

    def write_tx(self, cfg: RunnerConfig, *, status: str, observation: int = 54, starting_hash: str | None = None, production_hash: str | None = None, payload: dict | None = None) -> None:
        tx = {
            "cycle_id": "cycle-test",
            "observation_number": observation,
            "canonical_checkpoint": 5400,
            "market_path": str(cfg.output_dir / "market-data-v1.json"),
            "external_path": str(cfg.output_dir / "external-market-evidence-v1.json"),
            "starting_production_hash": starting_hash or sha(cfg.state_path),
            "starting_research_hash": sha(cfg.research_tracker_path),
            "status": status,
            "research_status": "PENDING",
        }
        if production_hash is not None:
            tx["production_hash"] = production_hash
        if payload is not None:
            tx["recovery_payload"] = payload
        tx["research_identity"] = {"canonical_checkpoint": 5400, "symbols": {"BTC": {"tracker_id": "BTC"}, "ETH": {"tracker_id": "ETH"}}}
        write_json(cfg.commit_journal_path, {"schema": "observation-commit-journal.v1", "transactions": [tx]})

    def payload(self, cfg: RunnerConfig, observation: int = 54) -> dict:
        return {"observation_number": observation, "production_hash": sha(cfg.state_path), "market_path": str(cfg.output_dir / "market-data-v1.json"), "external_path": str(cfg.output_dir / "external-market-evidence-v1.json"), "reviews": {}, "opportunity_snapshots": {}, "external_evidence": {}}

    def test_normal_53_to_54_success_then_next_is_55(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1)
        prod_calls: list[int] = []
        research_calls: list[int] = []
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, prod_calls), research_executor=self.research(cfg, research_calls), clock=Clock(1000))
        self.assertEqual(prod_calls, [54])
        self.assertEqual(research_calls, [54])
        self.assertEqual(result["cycles"][0]["production_result"], "PASS")
        journal = json.loads(cfg.commit_journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["transactions"][-1]["status"], "COMPLETE")
        self.assertEqual(next_observation_number(cfg.research_tracker_path, cfg.commit_journal_path), 55)

    def test_crash_after_prepared_before_production_is_abandoned(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        self.write_tx(cfg, status="PREPARED")
        run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        journal = json.loads(cfg.commit_journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["transactions"][-1]["status"], "ABANDONED")
        self.assertEqual(next_observation_number(cfg.research_tracker_path, cfg.commit_journal_path), 54)

    def test_crash_during_production_before_persist_is_abandoned(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        self.write_tx(cfg, status="PRODUCTION_COMMITTING")
        run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        journal = json.loads(cfg.commit_journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["transactions"][-1]["abandon_reason"], "PRODUCTION_NOT_COMMITTED")

    def test_crash_immediately_after_production_persist_recovers_research(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        old_hash = sha(cfg.state_path)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="PRODUCTION_COMMITTING", starting_hash=old_hash, payload=self.payload(cfg))
        research_calls: list[int] = []
        with mock.patch("market_reviewer.observation_runner._recovery_payload_from_transaction", return_value=self.payload(cfg)):
            result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, research_calls), clock=Clock(1000))
        self.assertEqual(research_calls, [54])
        self.assertEqual(result["recovered_pending_research"]["recovery"], PENDING_RESEARCH_RECOVERY)
        self.assertTrue(any(s.get("observation_number") == 54 for r in json.loads(cfg.research_tracker_path.read_text(encoding="utf-8"))["records"] for s in r["snapshots"]))

    def test_crash_after_production_reload_recovers_research_only(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        prod_calls: list[int] = []
        research_calls: list[int] = []
        run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, prod_calls), research_executor=self.research(cfg, research_calls), clock=Clock(1000))
        self.assertEqual(prod_calls, [])
        self.assertEqual(research_calls, [54])

    def test_research_failure_blocks_and_55_cannot_start(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        prod_calls: list[int] = []
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, prod_calls), research_executor=self.research(cfg, [], fail=True), clock=Clock(1000))
        self.assertEqual(result["status"], "BLOCKED_RECOVERY")
        self.assertEqual(prod_calls, [])

    def test_research_already_present_marks_stale_pending_complete_without_duplicate(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        append_research(cfg.research_tracker_path, 54)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        research_calls: list[int] = []
        run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, research_calls), clock=Clock(1000))
        self.assertEqual(research_calls, [])
        journal = json.loads(cfg.commit_journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["transactions"][-1]["status"], "COMPLETE")

    def test_research_ahead_of_production_blocks(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        write_json(cfg.research_tracker_path, research_store(latest=55))
        self.write_tx(cfg, status="COMPLETE", observation=54, production_hash=sha(cfg.state_path))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_INVALID_OBSERVATION_STATE)

    def test_production_ahead_by_more_than_one_blocks(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        self.write_tx(cfg, status="COMPLETE", observation=55, production_hash=sha(cfg.state_path))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_GAP)

    def test_production_hash_mismatch_from_pending_intent_blocks(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash="not-current", payload=self.payload(cfg))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_GAP)

    def test_status_exposes_pending_recovery(self) -> None:
        root = self.root()
        cfg = self.config(root)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        status = observation_runner_status(cfg.runner_state_path, state_path=cfg.state_path, research_tracker_path=cfg.research_tracker_path, journal_path=cfg.commit_journal_path, clock=Clock(1000))
        self.assertTrue(status["pending_research_recovery"])
        self.assertEqual(status["pending_observation_number"], 54)
        self.assertEqual(status["production_latest_observation"], 54)
        self.assertEqual(status["research_latest_observation"], 53)
        self.assertEqual(status["recovery_status"], PENDING_RESEARCH_RECOVERY)

    def test_latest_production_uses_journal_not_research_only(self) -> None:
        root = self.root()
        cfg = self.config(root)
        self.write_tx(cfg, status="RESEARCH_PENDING", observation=54, production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        journal = json.loads(cfg.commit_journal_path.read_text(encoding="utf-8"))
        self.assertEqual(latest_production_observation(journal, cfg.research_tracker_path), 54)
        self.assertEqual(next_observation_number(cfg.research_tracker_path, cfg.commit_journal_path), 55)


    def test_production_head_bootstrap_from_aligned_53_fixture(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        head = json.loads(cfg.production_head_path.read_text(encoding="utf-8"))
        self.assertEqual(head["schema"], "production-observation-head.v1")
        self.assertEqual(head["observation_number"], 53)

    def test_no_journal_production_ahead_blocks_without_rerun(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=1)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        prod_calls: list[int] = []
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, prod_calls), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_METADATA_MISSING)
        self.assertEqual(prod_calls, [])

    def test_wrong_research_checkpoint_rejected(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        append_research(cfg.research_tracker_path, 54)
        data = json.loads(cfg.research_tracker_path.read_text(encoding="utf-8"))
        for record in data["records"]:
            record["snapshots"][-1]["snapshot_timestamp"] = 9999
        write_json(cfg.research_tracker_path, data)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RESEARCH_IDENTITY_MISMATCH)

    def test_wrong_research_tracker_identity_rejected(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        append_research(cfg.research_tracker_path, 54)
        data = json.loads(cfg.research_tracker_path.read_text(encoding="utf-8"))
        for record in data["records"]:
            record["snapshots"][-1]["snapshot_timestamp"] = 5400
        data["records"][0]["tracker_id"] = "WRONG-BTC"
        write_json(cfg.research_tracker_path, data)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=self.payload(cfg))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RESEARCH_IDENTITY_MISMATCH)

    def test_stale_prepared_production_hash_drift_rejected_without_head(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        old_hash = sha(cfg.state_path)
        self.write_tx(cfg, status="PREPARED", starting_hash=old_hash)
        write_json(cfg.state_path, production_state(timestamp=5400))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_METADATA_MISSING)

    def test_stale_prepared_research_hash_drift_rejected(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        self.write_tx(cfg, status="PREPARED")
        append_research(cfg.research_tracker_path, 52)
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_STALE_PREPARED_BASELINE)

    def test_unchanged_prepared_safely_abandoned(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        self.write_tx(cfg, status="PREPARED")
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["recovered_pending_research"]["status"], "PREPARED_NOT_COMMITTED")

    def test_head_journal_mismatch_blocks(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        write_head(cfg.production_head_path, 53, sha(cfg.state_path), checkpoint=5300)
        self.write_tx(cfg, status="COMPLETE", observation=54, production_hash=sha(cfg.state_path))
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_GAP)

    def test_head_55_research_53_recovery_gap_blocks(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        write_head(cfg.production_head_path, 55, sha(cfg.state_path), checkpoint=5500)
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_GAP)

    def test_missing_recovery_evidence_blocks(self) -> None:
        root = self.root()
        cfg = self.config(root, max_cycles=0)
        write_json(cfg.state_path, production_state(timestamp=5400))
        write_head(cfg.production_head_path, 54, sha(cfg.state_path))
        self.write_tx(cfg, status="RESEARCH_PENDING", production_hash=sha(cfg.state_path), payload=None)
        result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=self.production(cfg, []), research_executor=self.research(cfg, []), clock=Clock(1000))
        self.assertEqual(result["status"], BLOCKED_RECOVERY_EVIDENCE_MISSING)

    def test_status_exposes_production_head(self) -> None:
        root = self.root()
        cfg = self.config(root)
        write_head(cfg.production_head_path, 53, sha(cfg.state_path), checkpoint=5300)
        status = observation_runner_status(cfg.runner_state_path, state_path=cfg.state_path, research_tracker_path=cfg.research_tracker_path, journal_path=cfg.commit_journal_path, production_head_path=cfg.production_head_path, clock=Clock(1000))
        self.assertEqual(status["production_head_observation"], 53)
        self.assertEqual(status["production_head_checkpoint"], 5300)
        self.assertEqual(status["production_head_hash"], sha(cfg.state_path))


if __name__ == "__main__":
    unittest.main()