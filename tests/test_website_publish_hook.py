import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from market_reviewer import observation_runner as runner
from market_reviewer import website_publish_hook as hook
from market_reviewer.persistence import atomic_write_json
from tests.test_website_public_snapshot import fixture

SECRET = "synthetic-secret-never-log"
ENV = {"GRIM_WEBSITE_SUPABASE_URL": "https://example.supabase.co",
       "GRIM_WEBSITE_SUPABASE_SERVICE_ROLE_KEY": SECRET}


class WebsitePublishHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = runner.RunnerConfig(output_dir=self.root / "artifact",
            state_path=self.root / "reviews/state.json", research_tracker_path=self.root / "research/tracker.json",
            liquidation_root=self.root / "artifact/liquidations")
        atomic_write_json(self.config.state_path, {"symbols": {s: {"previous_review_timestamp": 400} for s in ("BTC", "ETH")}})
        atomic_write_json(self.config.research_tracker_path, {"latest": 257})
        self.model = fixture()
        self.model["source"]["review_observation"] = 258
        self.model["runtime"].update(production_latest_observation=258, research_latest_observation=258)
        for row in self.model["symbols"].values():
            row["generated_at"] = 1000
        self.model_before = copy.deepcopy(self.model)
        self.env = patch.dict("os.environ", ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.builder_patch = patch.object(hook, "build_website_read_model", return_value=self.model)
        self.builder = self.builder_patch.start()
        self.addCleanup(self.builder_patch.stop)
        self.network_patch = patch("market_reviewer.website_supabase_publisher.build_opener")
        self.network = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.transport = self.network.return_value.open
        self.transport.return_value.__enter__.return_value.status = 201
        notification = patch("market_reviewer.notification_delivery.dispatch_completed_reviews")
        notification.start()
        self.addCleanup(notification.stop)

    def protected(self):
        return {p: p.read_bytes() for p in (self.config.state_path, self.config.research_tracker_path,
            self.config.commit_journal_path, self.config.production_head_path)}

    def production(self, preparation, number):
        self.transport.assert_not_called()
        atomic_write_json(self.config.state_path, {"symbols": {s: {"previous_review_timestamp": 700} for s in ("BTC", "ETH")}})
        return {"observation_number": number,
                "production_hash": hashlib.sha256(self.config.state_path.read_bytes()).hexdigest(),
                "reviews": {"BTC": {"State": "WATCH"}, "ETH": {"State": "NO_TRADE"}}}

    def research(self, payload):
        self.transport.assert_not_called()
        atomic_write_json(self.config.research_tracker_path, {"latest": payload["observation_number"]})
        return {"research_persistence": "PASS"}

    def complete(self, research=None):
        return runner._execute_ready_cycle(self.config, {"canonical_checkpoint": 1000},
            258, self.production, research or self.research, lambda: 1100, "test-cycle")

    def seed_complete(self):
        with patch.object(runner, "_publish_completed_website"):
            self.complete()

    def call(self):
        return hook.publish_latest_after_complete(self.config, 258, 1000, clock=lambda: 1100)

    def logs(self):
        return [json.loads(line) for line in self.config.runner_log_path.read_text().splitlines()]

    def test_only_after_complete_persisted_success_matches_observation(self):
        captured = []
        response = self.transport.return_value
        def at_publish(request, **kwargs):
            tx = json.loads(self.config.commit_journal_path.read_text())["transactions"][-1]
            self.assertEqual(tx["status"], "COMPLETE")
            self.assertEqual(tx["research_status"], "COMPLETE")
            self.assertEqual(tx["canonical_checkpoint"], 1000)
            body = json.loads(request.data)
            self.assertEqual(body["source_observation"], 258)
            self.assertEqual(body["snapshot"]["review_observation"], 258)
            self.assertEqual(body["generated_at"], 1100)
            self.assertEqual(body["stale_after_seconds"], 1800)
            captured.append(self.protected())
            return response
        self.transport.side_effect = at_publish
        result = self.complete()
        self.assertEqual(result["production_result"], "PASS")
        self.assertEqual(result["research_result"], "PASS")
        self.assertEqual(set(result), {"observation_number", "production_result", "research_result", "blocker", "next_scheduled_run"})
        self.assertEqual(captured[0], self.protected())
        self.assertEqual(self.model, self.model_before)
        self.transport.assert_called_once()
        self.assertEqual(self.logs()[-1], {"website_snapshot_publish": "PUBLISHED", "observation": 258, "reason": None})
        self.assertEqual(self.builder.call_args.kwargs["commit_journal_path"], self.config.commit_journal_path)

    def test_non_complete_never_publishes(self):
        self.seed_complete()
        for status in ("PREPARED", "PRODUCTION_COMMITTING", "RESEARCH_PENDING", "ABANDONED"):
            journal = json.loads(self.config.commit_journal_path.read_text())
            journal["transactions"][-1]["status"] = status
            atomic_write_json(self.config.commit_journal_path, journal)
            self.assertEqual(self.call()["status"], "SKIPPED_NOT_COMPLETE")
        self.network.assert_not_called()
        self.builder.assert_not_called()

    def test_research_failure_does_not_publish(self):
        with self.assertRaises(ValueError):
            self.complete(research=lambda payload: {"research_persistence": "FAIL", "message": "failed"})
        self.network.assert_not_called()
        self.assertEqual(json.loads(self.config.commit_journal_path.read_text())["transactions"][-1]["status"], "RESEARCH_PENDING")

    def test_missing_config_runner_pass(self):
        with patch.dict("os.environ", {}, clear=True):
            result = self.complete()
        self.assertEqual(result["production_result"], "PASS")
        self.assertEqual(self.logs()[-1]["website_snapshot_publish"], "NOT_CONFIGURED")
        self.builder.assert_not_called()
        self.network.assert_not_called()

    def test_http_failure_runner_pass(self):
        self.transport.side_effect = HTTPError("https://private", 503, SECRET, {}, None)
        result = self.complete()
        self.assertEqual(result["production_result"], "PASS")
        self.assertEqual(self.logs()[-1]["reason"], "HTTP_ERROR")
        self.transport.assert_called_once()
        self.assertNotIn(SECRET, self.config.runner_log_path.read_text())

    def test_timeout_runner_pass(self):
        self.transport.side_effect = TimeoutError(SECRET)
        result = self.complete()
        self.assertEqual(result["research_result"], "PASS")
        self.assertEqual(self.logs()[-1]["reason"], "TIMEOUT")
        self.transport.assert_called_once()

    def test_source_n_minus_one_or_n_plus_one_is_skipped(self):
        self.seed_complete()
        for number in (257, 259):
            self.model["source"]["review_observation"] = number
            self.assertEqual(self.call()["status"], "SKIPPED_SOURCE_MISMATCH")
        self.network.assert_not_called()

    def test_checkpoint_and_runtime_mismatch_is_skipped(self):
        self.seed_complete()
        self.model["symbols"]["BTC"]["generated_at"] = 999
        self.assertEqual(self.call()["status"], "SKIPPED_SOURCE_MISMATCH")
        self.model["symbols"]["BTC"]["generated_at"] = 1000
        self.model["runtime"]["research_latest_observation"] = 259
        self.assertEqual(self.call()["status"], "SKIPPED_SOURCE_MISMATCH")
        self.network.assert_not_called()

    def test_state_drift_during_build_never_publishes(self):
        self.seed_complete()
        def drift(**kwargs):
            atomic_write_json(self.config.research_tracker_path, {"latest": 259})
            return self.model
        self.builder.side_effect = drift
        self.assertEqual(self.call()["status"], "SKIPPED_SOURCE_MISMATCH")
        self.network.assert_not_called()

    def test_hook_exception_cannot_escape_or_change_payload(self):
        with patch.object(hook, "publish_latest_after_complete", side_effect=RuntimeError(SECRET)):
            result = self.complete()
        self.assertEqual(result["production_result"], "PASS")
        self.assertEqual(self.logs()[-1]["reason"], "HOOK_ERROR")
        journal = self.config.commit_journal_path.read_text()
        self.assertNotIn("website_snapshot_publish", journal)
        self.assertNotIn(SECRET, journal + self.config.runner_log_path.read_text() + json.dumps(result))

    def test_logging_failure_does_not_escape(self):
        with patch.object(runner, "_append_runner_log", side_effect=OSError(SECRET)):
            self.assertEqual(self.complete()["production_result"], "PASS")

    def test_unknown_hook_status_and_reason_redacted(self):
        with patch.object(hook, "publish_latest_after_complete", return_value={"status": SECRET, "reason": SECRET}):
            self.complete()
        self.assertNotIn(SECRET, self.config.runner_log_path.read_text())

    def test_completed_history_not_scanned_for_backfill(self):
        self.seed_complete()
        with patch.object(runner, "_active_transaction", return_value=None), \
             patch.object(runner, "_bootstrap_production_head_if_aligned", return_value={}), \
             patch.object(runner, "latest_research_observation", return_value=258), \
             patch.object(runner, "latest_production_observation", return_value=258), \
             patch.object(runner, "_repair_completed_research_watermark", return_value=None), \
             patch.object(runner, "_publish_completed_website") as publish:
            runner._reconcile_observation_transactions(self.config, self.research, lambda: 1100)
        publish.assert_not_called()

    def test_recovery_new_complete_already_present_publishes_once(self):
        self.recovery_case("MATCH")

    def test_recovery_new_complete_after_research_publishes_once(self):
        self.recovery_case("MISSING")

    def recovery_case(self, identity):
        self.seed_complete()
        journal = json.loads(self.config.commit_journal_path.read_text())
        journal["transactions"][-1].update(status="RESEARCH_PENDING", research_status="PENDING")
        atomic_write_json(self.config.commit_journal_path, journal)
        payload = journal["transactions"][-1]["recovery_payload"]
        with patch.object(runner, "_research_identity_status", return_value=identity), \
             patch.object(runner, "latest_research_observation", return_value=258), \
             patch.object(runner, "_transaction_has_recovery_evidence", return_value=True), \
             patch.object(runner, "_recovery_payload_from_transaction", return_value=payload):
            result = runner._reconcile_observation_transactions(self.config, self.research, lambda: 1100)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(json.loads(self.config.commit_journal_path.read_text())["transactions"][-1]["status"], "COMPLETE")
            self.transport.assert_called_once()
            runner._reconcile_observation_transactions(self.config, self.research, lambda: 1100)
            self.transport.assert_called_once()

    def test_waiting_blocked_and_dry_run_do_not_publish(self):
        from dataclasses import replace
        for status in ("WAITING_FOR_M5_CLOSE", "BLOCKED", "READY"):
            with self.subTest(status=status), \
                 patch.object(runner, "_reconcile_observation_transactions", return_value=None), \
                 patch.object(runner, "next_observation_number", return_value=258), \
                 patch.object(runner, "_publish_completed_website") as publish:
                cfg = replace(self.config, dry_run=status == "READY")
                runner.run_observation_cycle(cfg, coordinator=lambda **kw: {"status": status},
                    production_executor=self.production, research_executor=self.research, clock=lambda: 1100)
                publish.assert_not_called()
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
