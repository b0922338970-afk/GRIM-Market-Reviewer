import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from market_reviewer import liquidation_collector as module
from market_reviewer.liquidation_collector import (
    CoverageMetadataCorrupted, CoverageStore, LiquidationEventStore,
    aggregate_liquidations, liquidation_metrics_from_store,
)


class CoverageAtomicPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = CoverageStore(self.root)
        self.store.mark_started("BTC", 1000)
        self.target = self.store.path("BTC")
        self.original = self.target.read_bytes()

    def assert_preserved(self):
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_atomic_write_valid_json(self):
        self.store.mark_transport_alive("BTC", 2000)
        self.assertEqual(json.loads(self.target.read_text())["last_transport_alive_at"], 2000)

    def fail_handle_operation(self, operation):
        factory = tempfile.NamedTemporaryFile

        def failing_file(*args, **kwargs):
            handle = factory(*args, **kwargs)
            proxy = MagicMock(wraps=handle)
            proxy.name = handle.name
            proxy.__enter__.return_value = proxy
            proxy.__exit__.side_effect = handle.__exit__
            getattr(proxy, operation).side_effect = OSError(operation)
            return proxy

        with patch.object(module.tempfile, "NamedTemporaryFile", side_effect=failing_file):
            with self.assertRaisesRegex(OSError, operation):
                self.store.mark_transport_alive("BTC", 2000)
        self.assert_preserved()

    def test_temp_write_failure_preserves_target_and_cleans_temp(self):
        self.fail_handle_operation("write")

    def test_flush_failure_preserves_target_and_cleans_temp(self):
        self.fail_handle_operation("flush")

    def test_fsync_failure_preserves_target_and_cleans_temp(self):
        with patch.object(module.os, "fsync", side_effect=OSError("fsync")):
            with self.assertRaisesRegex(OSError, "fsync"):
                self.store.mark_transport_alive("BTC", 2000)
        self.assert_preserved()

    def test_replace_failure_preserves_target_and_cleans_temp(self):
        with patch.object(module.os, "replace", side_effect=PermissionError("replace")):
            with self.assertRaisesRegex(PermissionError, "replace"):
                self.store.mark_transport_alive("BTC", 2000)
        self.assert_preserved()

    def test_serialization_failure_does_not_touch_target(self):
        with self.assertRaises(TypeError):
            self.store.save("BTC", {"bad": object()})
        self.assert_preserved()

    def test_zero_filled_detected(self):
        self.target.write_bytes(b"\x00" * 512)
        with self.assertRaisesRegex(CoverageMetadataCorrupted, "COVERAGE_METADATA_CORRUPTED"):
            self.store.load("BTC")

    def test_truncated_json_detected(self):
        self.target.write_text('{"coverage_intervals": [')
        with self.assertRaises(CoverageMetadataCorrupted):
            self.store.load("BTC")

    def test_invalid_encoding_and_non_object_detected(self):
        for payload in (b"\xff", b"null", b"[]"):
            with self.subTest(payload=payload):
                self.target.write_bytes(payload)
                with self.assertRaises(CoverageMetadataCorrupted):
                    self.store.load("BTC")

    def test_corruption_blocks_aggregation_instead_of_complete_zero(self):
        self.target.write_bytes(b"\x00" * 10)
        events = LiquidationEventStore(self.root)
        with patch.object(module, "aggregate_liquidations") as aggregate:
            with self.assertRaises(CoverageMetadataCorrupted):
                liquidation_metrics_from_store(events, self.store, "BTC", 5000)
            aggregate.assert_not_called()

    def test_corrupt_restart_fails_without_overwriting_history(self):
        self.target.write_bytes(b"\x00" * 10)
        before = self.target.read_bytes()
        with self.assertRaises(CoverageMetadataCorrupted):
            CoverageStore(self.root).mark_started("BTC", 5000)
        self.assertEqual(self.target.read_bytes(), before)

    def test_missing_history_does_not_produce_available_zero(self):
        result = aggregate_liquidations([], self.store.load("ETH"), 5000)
        for window in result.values():
            self.assertEqual(window["coverage_status"], "UNAVAILABLE")

    def test_valid_restart_preserves_intervals(self):
        self.store.mark_transport_alive("BTC", 2000)
        loaded = CoverageStore(self.root).mark_started("BTC", 3000)
        self.assertEqual(loaded["coverage_intervals"][0]["end"], 2000)
        self.assertEqual(loaded["coverage_intervals"][1]["start"], 3000)

    def test_sequential_atomic_writes(self):
        for timestamp in range(1001, 1021):
            self.store.mark_transport_alive("BTC", timestamp)
            self.assertEqual(CoverageStore(self.root).load("BTC")["last_transport_alive_at"], timestamp)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_replace_uses_same_directory_closed_fsynced_temp(self):
        replace = os.replace
        fsync = os.fsync
        order = []

        def sync(descriptor):
            order.append("fsync")
            return fsync(descriptor)

        def replace_checked(source, target):
            self.assertEqual(Path(source).parent, Path(target).parent)
            self.assertEqual(Path(target).read_bytes(), self.original)
            self.assertEqual(json.loads(Path(source).read_text())["last_transport_alive_at"], 2000)
            self.assertEqual(order, ["fsync"])
            order.append("replace")
            return replace(source, target)

        with patch.object(module.os, "fsync", side_effect=sync), patch.object(module.os, "replace", side_effect=replace_checked):
            self.store.mark_transport_alive("BTC", 2000)
        self.assertEqual(order[:2], ["fsync", "replace"])

    def test_directory_sync_unsupported_is_best_effort(self):
        with patch.object(module.os, "name", "posix"), patch.object(module.os, "open", side_effect=OSError("unsupported")):
            module._fsync_coverage_directory(self.root)

    def test_windows_directory_sync_is_skipped(self):
        with patch.object(module.os, "name", "nt"), patch.object(module.os, "open") as opened:
            module._fsync_coverage_directory(self.root)
            opened.assert_not_called()

