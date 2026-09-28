import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_reviewer.website_public_snapshot import (
    SCHEMA, FilePublisher, build_public_snapshot, export_public_snapshot,
    publish_public_snapshot, validate_public_snapshot,
)


def fixture():
    return {
        "generated_at": 999999,
        "source": {"review_observation": 251, "review_path": "C:\\private\\journal.json"},
        "runtime": {"running": True, "production_latest_observation": 251,
                    "research_latest_observation": 251, "pid": 123, "path": "/private/runtime"},
        "symbols": {s: {
            "generated_at": 1000, "review_state": "WATCH" if s == "BTC" else "NO_TRADE",
            "direction": "LONG" if s == "BTC" else "NONE", "timeframe": "M5",
            "htf_regime": "RANGE",
            "market_story": f"{s} | NONE | RANGE | INVALIDATED | NO_TRADE; secret evidence /private/path",
            "structure": {"active_draw": {"htf": "UNAVAILABLE", "tactical":
                "Internal Sell-side 123.45 on H1, formed_at=900, distance=0.1000",
                "token": "do-not-export"},
                "liquidity_reaction": "SWEEP_RECLAIMED", "displacement": "VALID",
                "MSS": "CONTEXTUAL", "BOS": "GENERIC", "FVG": "CHAIN_LINKED_FRESH",
                "OB": "ISOLATED_TESTED", "Breaker": "NOT_CONFIRMED"},
            "secret": "do-not-export",
        } for s in ("BTC", "ETH")},
        "evidence": {
            "liquidation": {"BTC": {"status": "CONNECTED", "path": "/private/events"},
                            "ETH": {"status": "DISCONNECTED"}},
            "notification": {"enabled_channels": [], "last_delivery": {
                "channel": "TELEGRAM", "token": "do-not-export", "chat_id": "private-chat"},
                "webhook_url": "https://private.example", "journal": ["raw"]}},
        "research": {"tracker": {"raw": "do-not-export"}, "summary": {
            "historical_origins": 45, "live_origins": 2, "classifiable_live_origins": 0,
            "outcome_complete_live_origins": 2,
            "next_review_ready": {"SAMPLE_READY": False, "OUTCOME_READY": True}}},
        "execution": {"status": "ACTIVE", "connected": True, "credentials": "do-not-export"},
    }


class PublicSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.model = fixture()

    def snapshot(self, **kwargs):
        return build_public_snapshot(self.model, now=kwargs.pop("now", 1100), **kwargs)

    def test_mapping_symbols(self):
        value = self.snapshot()
        self.assertEqual(value["schema"], SCHEMA)
        self.assertEqual(value["symbols"]["BTC"]["review_state"], "WATCH")
        self.assertEqual(value["symbols"]["ETH"]["review_state"], "NO_TRADE")
        self.assertEqual(value["symbols"]["BTC"]["structure"]["MSS"], "CONTEXTUAL")
        self.assertEqual(value["symbols"]["BTC"]["structure"]["OB"], "ISOLATED_TESTED")
        self.assertEqual(value["evidence"]["liquidation"]["BTC"]["status"], "CONNECTED")
        self.assertEqual(value["evidence"]["liquidation"]["ETH"]["status"], "DISCONNECTED")
        self.assertEqual(value["source_observation"], 251)
        self.assertFalse(value["research"]["FIRST_REVIEW"])
        self.assertTrue(value["research"]["CALIBRATION"])

    def test_no_sensitive_keys_or_raw_records(self):
        text = json.dumps(self.snapshot())
        for secret in ("token", "chat_id", "webhook_url", "credentials", "journal",
                       "review_path", "tracker", "do-not-export", "private-chat"):
            self.assertNotIn(secret, text)

    def test_nested_and_scalar_injections_fail_closed(self):
        for bad in ("C:\\private\\data.json", "/home/user/secret",
                    "\\\\server\\share", "https://host/secret", "123456:SECRET_TOKEN",
                    "SECRET", {"token": "secret", "value": "secret"}):
            with self.subTest(bad=bad):
                row = self.model["symbols"]["BTC"]
                for k in ("review_state", "direction", "timeframe", "htf_regime", "market_story"):
                    row[k] = bad
                for k in row["structure"]:
                    row["structure"][k] = bad
                encoded = json.dumps(self.snapshot())
                self.assertNotIn("private", encoded)
                self.assertNotIn("SECRET", encoded)
                self.assertNotIn("host/secret", encoded)
                self.assertNotIn("server", encoded)
                self.assertNotIn("home/user", encoded)

    def test_notification_names_only(self):
        self.assertEqual(self.snapshot()["evidence"]["notification"], {"channels": ["TELEGRAM"]})
        self.model["evidence"]["notification"]["enabled_channels"] = ["discord", "TELEGRAM", "discord", "https://secret"]
        self.assertEqual(self.snapshot()["evidence"]["notification"]["channels"], ["DISCORD", "TELEGRAM"])

    def test_execution_reserved_regardless_of_input(self):
        self.assertEqual(self.snapshot()["execution"], {"status": "RESERVED", "connected": False})

    def test_stale_boundary_and_state_unchanged(self):
        self.assertFalse(self.snapshot(now=1100, stale_after_seconds=100)["stale"])
        value = self.snapshot(now=1101, stale_after_seconds=100)
        self.assertTrue(value["stale"])
        self.assertTrue(value["runtime"]["stale"])
        self.assertEqual(value["symbols"]["BTC"]["review_state"], "WATCH")
        self.assertEqual(value["source_updated_at"], 1000)
        self.assertEqual(value["generated_at"], 1101)

    def test_missing_future_or_lagging_reviewer_is_stale(self):
        self.model["symbols"]["ETH"].pop("generated_at")
        self.assertTrue(self.snapshot()["stale"])
        self.model = fixture()
        self.model["symbols"]["ETH"]["generated_at"] = 1200
        self.assertTrue(self.snapshot()["stale"])
        self.model = fixture()
        self.model["runtime"]["production_latest_observation"] = 252
        value = self.snapshot()
        self.assertTrue(value["stale"])
        self.assertEqual(value["review_observation"], 251)
        self.assertEqual(value["runtime"]["production_latest_observation"], 252)

    def test_unavailable_wrapper_not_guessed(self):
        self.model["symbols"]["BTC"]["structure"]["MSS"] = {
            "availability": "AVAILABLE", "value": "CONTEXTUAL", "reason": "EVIDENCE_NOT_EXPOSED"}
        self.assertEqual(self.snapshot()["symbols"]["BTC"]["structure"]["MSS"], "UNAVAILABLE")

    def test_story_summary_and_source_immutability(self):
        before = copy.deepcopy(self.model)
        value = self.snapshot()
        self.assertEqual(value["symbols"]["BTC"]["market_story"], "BTC | NONE | RANGE | INVALIDATED | NO_TRADE")
        self.assertEqual(before, self.model)

    def test_deterministic_and_validated(self):
        a, b = self.snapshot(), self.snapshot()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        validate_public_snapshot(a)

    def test_empty_model_exports_unavailable_not_negative(self):
        value = build_public_snapshot({}, now=1100)
        self.assertTrue(value["stale"])
        self.assertIsNone(value["research"]["historical_origins"])
        self.assertEqual(value["symbols"]["BTC"]["review_state"], "UNAVAILABLE")
        validate_public_snapshot(value)

    def test_reject_unsafe_publish_input_before_adapter(self):
        from unittest.mock import Mock
        publisher = Mock()
        value = self.snapshot()
        value["secret"] = "sensitive"
        self.assertEqual(publish_public_snapshot(value, publisher)["status"], "PUBLISH_FAILED")
        publisher.publish.assert_not_called()

    def test_failure_leaves_completed_runner_transaction_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = [Path(tmp) / name for name in (
                "observation-runner.json", "observation-commit-journal.json",
                "thesis-baseline.json", "missed-opportunities.json")]
            for path in files:
                path.write_text('{"status":"COMPLETE","observation_number":251}')
            before = {p: p.read_bytes() for p in files}
            from unittest.mock import Mock
            publisher = Mock()
            publisher.publish.side_effect = TimeoutError("private destination")
            result = export_public_snapshot(builder=lambda: self.model, publisher=publisher, now=1100)
            self.assertEqual(result["status"], "PUBLISH_FAILED")
            self.assertEqual(before, {p: p.read_bytes() for p in files})
            self.assertEqual(len(list(Path(tmp).iterdir())), 4)

    def test_current_draw_formats(self):
        self.model["symbols"]["BTC"]["structure"]["active_draw"] = {
            "htf": "Macro Draw: External Buy-side Liquidity 123.45 on D1, distance=0.1000; aligned with BULLISH swing bias",
            "tactical": "Internal Sell-side Liquidity 100.00 on H1, formed_at=900, distance=0.0500"}
        draw = self.snapshot()["symbols"]["BTC"]["structure"]["active_draw"]
        self.assertEqual(draw["htf"], "Macro Draw: External Buy-side Liquidity 123.45 on D1, distance=0.1000")
        self.assertEqual(draw["tactical"], self.model["symbols"]["BTC"]["structure"]["active_draw"]["tactical"])

    def test_publisher_failure_is_fail_open_and_sanitized(self):
        class FailingPublisher:
            def publish(self, snapshot):
                raise TimeoutError("https://secret/token C:\\private\\path")
        value = self.snapshot()
        before = copy.deepcopy(value)
        result = publish_public_snapshot(value, FailingPublisher())
        self.assertEqual(result, {"status": "PUBLISH_FAILED", "error": "PUBLISHER_ERROR"})
        self.assertEqual(before, value)

    def test_publisher_cannot_mutate_snapshot(self):
        class Publisher:
            def publish(self, snapshot):
                snapshot.clear()
        value = self.snapshot()
        before = copy.deepcopy(value)
        self.assertEqual(publish_public_snapshot(value, Publisher())["status"], "PUBLISHED")
        self.assertEqual(before, value)

    def test_atomic_export_and_no_state_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = [root / f for f in ("runner.json", "production.json", "research.json")]
            for p in protected:
                p.write_text('{"observation":251}')
            before = {p: p.read_bytes() for p in protected}
            destination = root / "website-public-snapshot.json"
            destination.write_text('{"old":true}')
            import os
            replace = os.replace
            calls = []
            def checked_replace(source, target):
                self.assertEqual(Path(target).read_text(), '{"old":true}')
                self.assertEqual(Path(source).parent, destination.parent)
                self.assertEqual(json.loads(Path(source).read_text())["schema"], SCHEMA)
                calls.append(True)
                replace(source, target)
            with patch("market_reviewer.persistence.os.replace", side_effect=checked_replace):
                result = export_public_snapshot(output=destination, builder=lambda: self.model, now=1100)
            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual(calls, [True])
            self.assertEqual(json.loads(destination.read_text()), self.snapshot())
            self.assertEqual(before, {p: p.read_bytes() for p in protected})
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_failed_atomic_replace_preserves_previous_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "public.json"
            output.write_text('{"old":true}')
            with patch("market_reviewer.persistence.os.replace", side_effect=OSError("private")):
                result = export_public_snapshot(output=output, builder=lambda: self.model, now=1100)
            self.assertEqual(result["status"], "PUBLISH_FAILED")
            self.assertEqual(output.read_text(), '{"old":true}')
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_build_failure_is_contained(self):
        def failing_builder():
            raise OSError("private-path")
        self.assertEqual(export_public_snapshot(builder=failing_builder),
                         {"status": "PUBLISH_FAILED", "error": "SNAPSHOT_BUILD_FAILED"})

    def test_cli_explicit_export_only(self):
        from market_reviewer.website_cli import main, build_parser
        self.assertEqual(build_parser().parse_args(["export-public"]).output, "artifact/website-public-snapshot.json")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public.json"
            with patch("market_reviewer.website_public_snapshot.build_website_read_model", return_value=self.model), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(["export-public", "--output", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["schema"], SCHEMA)

    def test_invalid_freshness_policy_fails_closed(self):
        for seconds in (0, -1, True, "100"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.snapshot(stale_after_seconds=seconds)


if __name__ == "__main__":
    unittest.main()
