import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from market_reviewer.cli import main
from market_reviewer.research_maturity import research_maturity
from market_reviewer.website_read_model import build_website_read_model
import test_smc_live_sample_status as fixtures


class ResearchMaturityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SMCLiveSampleStatusTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def populate(self, long=5, short=5):
        self.f.records = []
        for i in range(long + short):
            r = self.f.record(i)
            direction = "LONG" if i < long else "SHORT"
            r["direction"] = direction
            state = r["snapshots"][0]["smc_state"]
            state["direction"] = state["matching_context"]["direction"] = direction
            r["schema"] = "missed-opportunity-tracker.v1"
            r["episode_status"] = "OPEN"
            r["status"] = "DETERIORATING"
            for key, value in (("production_sequence_id", "BTC-seq-0009"),
                               ("production_sequence_state", "INVALIDATED"),
                               ("production_review_state", "NO_TRADE")):
                r[key + "_at_origin"] = value
                r["snapshots"][0][key] = value
            self.f.records.append(r)

    def gate(self):
        self.f.save()
        return research_maturity(self.f.root, self.f.live, self.f.outcomes)

    def test_long_shortfall(self):
        self.populate(4, 5)
        g = self.gate()
        self.assertFalse(g["FIRST_REVIEW_READY"])
        self.assertIn("LONG_SAMPLE_SHORTFALL", g["first_review_reasons"])

    def test_short_shortfall(self):
        self.populate(5, 4)
        self.assertIn("SHORT_SAMPLE_SHORTFALL", self.gate()["first_review_reasons"])

    def test_five_each_complete(self):
        self.populate()
        g = self.gate()
        self.assertTrue(g["FIRST_REVIEW_READY"])
        self.assertFalse(g["CALIBRATION_READY"])

    def test_pending_not_complete(self):
        self.populate()
        self.f.records[0]["outcomes"]["24H"]["horizon_status"] = "PENDING"
        g = self.gate()
        self.assertEqual(g["sides"]["LONG"]["outcome_complete"], 4)
        self.assertEqual(g["sides"]["LONG"]["pending"], 1)
        self.assertIn("OUTCOME_PENDING", g["first_review_reasons"])

    def test_legacy_excluded_without_backfill(self):
        self.populate(7, 5)
        for r in self.f.records[:2]:
            del r["snapshots"][0]["smc_state"]
        g = self.gate()
        self.assertEqual(g["sides"]["LONG"]["unclassifiable"], 2)
        self.assertEqual(g["sides"]["LONG"]["outcome_complete"], 5)
        self.assertTrue(g["FIRST_REVIEW_READY"])

    def test_duplicate_not_counted(self):
        self.populate(4, 5)
        self.f.records.append(copy.deepcopy(self.f.records[0]))
        self.assertEqual(self.gate()["sides"]["LONG"]["outcome_complete"], 4)

    def test_conflicting_duplicate_blocks(self):
        self.populate(8, 8)
        dupe = copy.deepcopy(self.f.records[0])
        dupe["origin_price"] = 123
        self.f.records.append(dupe)
        self.assertIn("FROZEN_CONTEXT_INTEGRITY_VIOLATION", self.gate()["integrity_reasons"])

    def test_historical_not_live(self):
        self.populate(1, 0)
        self.f.records[0]["sample_source"] = "HISTORICAL_REPLAY"
        g = self.gate()
        self.assertEqual(g["sides"]["LONG"]["classifiable_live"], 0)
        self.assertFalse(g["FIRST_REVIEW_READY"])

    def test_eight_each_calibration(self):
        self.populate(8, 8)
        g = self.gate()
        self.assertTrue(g["CALIBRATION_READY"])
        self.assertEqual(g["sides"]["SHORT"]["outcome_summaries"]["4H"],
                         {"n": 8, "median_MFE_pct": 2, "median_signed_MAE_pct": -1})

    def test_eight_seven_not_calibration(self):
        self.populate(8, 7)
        g = self.gate()
        self.assertTrue(g["FIRST_REVIEW_READY"])
        self.assertFalse(g["CALIBRATION_READY"])
        self.assertIn("SHORT_CALIBRATION_SAMPLE_SHORTFALL", g["calibration_reasons"])

    def test_deterministic(self):
        self.populate()
        first = self.gate()
        self.f.records.reverse()
        self.assertEqual(first, self.gate())

    def test_immutable_and_shared_cli_website(self):
        self.populate()
        self.f.save()
        production = self.f.base / "production.json"
        production.write_text('{"schema":"review-state.v2"}')
        runner = self.f.base / "runner.json"
        runner.write_text('{}')
        before = {p: p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()}
        args = ["research-maturity", "--root", str(self.f.root), "--live-store", str(self.f.live),
                "--historical-outcomes", str(self.f.outcomes), "--json"]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(args), 0)
        g = json.loads(output.getvalue())
        with patch('market_reviewer.website_read_model.observation_runner_status', return_value={}), \
             patch('market_reviewer.website_read_model.notification_status', return_value={}):
            model = build_website_read_model(review_state_path=production, runner_state_path=runner,
                research_store_path=self.f.live, smc_root=self.f.root, historical_outcomes_path=self.f.outcomes,
                liquidation_root=self.f.base / 'absent')
        self.assertEqual(g, model['research']['acceptance'])
        self.assertEqual(model['research']['summary']['next_review_ready'],
                         {'SAMPLE_READY': True, 'OUTCOME_READY': False})
        self.assertEqual(before, {p: p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()})

    def test_future_context_blocks(self):
        self.populate(8, 8)
        self.f.records[0]['snapshots'][0]['smc_state']['available_at'] = 9999999999
        self.assertFalse(self.gate()['FIRST_REVIEW_READY'])

    def test_lineage_mismatch_blocks(self):
        self.populate(8, 8)
        self.f.records[0]['production_sequence_id_at_origin'] = 'wrong'
        self.assertIn('PRODUCTION_RESEARCH_LINEAGE_MISMATCH', self.gate()['integrity_reasons'])

    def test_invalid_outcome_blocks_calibration(self):
        self.populate(9, 8)
        self.f.records[0]['outcomes']['1H']['MFE_pct'] = None
        self.assertIn('OUTCOME_LIFECYCLE_INVALID', self.gate()['calibration_reasons'])

    def test_corrupt_episode_blocks_calibration(self):
        self.populate(9, 8)
        self.f.records[0]['episode_status'] = 'CORRUPT'
        self.assertIn('EPISODE_LIFECYCLE_INVALID', self.gate()['calibration_reasons'])

    def test_frozen_schema_drift_blocks(self):
        self.populate(9, 8)
        self.f.records[0]['snapshots'][0]['live_smc_frozen_context'] = {'schema': 'unsupported'}
        self.assertIn('FROZEN_CONTEXT_INTEGRITY_VIOLATION', self.gate()['calibration_reasons'])

    def test_pending_surplus_does_not_block_complete_cohort(self):
        self.populate(9, 8)
        self.f.records[0]['outcomes']['1H']['horizon_status'] = 'PENDING'
        self.assertTrue(self.gate()['CALIBRATION_READY'])

    def test_missing_store_unknown_not_zero(self):
        self.f.live.unlink()
        g = research_maturity(self.f.root, self.f.live, self.f.outcomes)
        self.assertIsNone(g['sides']['LONG']['outcome_complete'])
        self.assertFalse(g['FIRST_REVIEW_READY'])

    def test_concurrent_source_replacement_fail_closed(self):
        self.populate()
        self.f.save()
        def replace(*args):
            self.f.live.write_text('{}')
            return {}
        with patch('market_reviewer.research_maturity.smc_live_sample_status', side_effect=replace):
            g = research_maturity(self.f.root, self.f.live, self.f.outcomes)
        self.assertIn('SOURCE_CHANGED_DURING_READ', g['integrity_reasons'])
