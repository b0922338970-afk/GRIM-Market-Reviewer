import copy
import unittest
from unittest.mock import patch

from tests import test_live_hybrid_smc as fixture
from market_reviewer.production_smc_context import enrich_review
from market_reviewer.persistence import build_review_state
from market_reviewer.model import Candle, TIMEFRAME_SECONDS


class ProductionSMCContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.LiveHybridSMCTests.setUpClass()
        cls.review = fixture.LiveHybridSMCTests.review
        cls.frames = fixture.LiveHybridSMCTests.frames
        cls.output = enrich_review(cls.review, cls.frames)

    def test_existing_review_fields_unchanged(self):
        self.assertEqual({k: self.output[k] for k in self.review}, self.review)

    def test_production_persistence_unchanged(self):
        self.assertEqual(build_review_state({'BTC': self.review}, {}),
                         build_review_state({'BTC': self.output}, {}))

    def test_inputs_immutable(self):
        before = copy.deepcopy((self.review, self.frames))
        enrich_review(self.review, self.frames)
        self.assertEqual(before, (self.review, self.frames))

    def test_real_shared_engine(self):
        self.assertEqual(self.output['smc_context']['producer_status'], 'AVAILABLE')
        self.assertFalse(self.output['smc_context']['provenance']['outcomes_used'])

    def test_schemas(self):
        self.assertEqual(self.output['smc_context']['schema'], 'production-smc-context.v1')
        self.assertEqual(self.output['opportunity_alert']['schema'], 'opportunity_alert.v1')

    def test_mapping_all_states(self):
        for state, level in [('NO_TRADE','SILENT'), ('WAIT','SILENT'),
                             ('WATCH','NORMAL_ALERT'), ('ARMED','HIGH_PRIORITY_ALERT')]:
            with self.subTest(state=state):
                out = enrich_review(dict(self.review, State=state), self.frames)
                packet = out['opportunity_alert']
                self.assertEqual(packet['alert_level'], level)
                self.assertEqual(packet['emit_alert'], state in ('WATCH','ARMED'))
                self.assertEqual(packet['execution_ready'], state == 'ARMED')
                self.assertEqual(out['State'], state)

    def test_unavailable_without_gate_change(self):
        out = enrich_review(dict(self.review, State='WATCH'), {})
        self.assertEqual(out['State'], 'WATCH')
        self.assertEqual(out['smc_context']['bpr_state']['availability'], 'UNAVAILABLE')

    def test_no_research_zone_promotion(self):
        self.assertEqual(self.output['opportunity_alert']['entry_zone']['availability'], 'UNAVAILABLE')

    def test_missing_invalidation_and_target(self):
        for key in ('invalidation', 'target'):
            self.assertEqual(self.output['opportunity_alert'][key]['availability'], 'UNAVAILABLE')

    def test_generated_at_is_closed_availability(self):
        self.assertEqual(self.output['opportunity_alert']['generated_at'], int(self.review['Review_Timestamp'])+300)

    def test_future_candles_do_not_change_output(self):
        frames = copy.deepcopy(self.frames)
        cp = int(self.review['Review_Timestamp'])+300
        for tf, frame in frames.items():
            frame.candles.append(Candle(cp+TIMEFRAME_SECONDS[tf], 999, 9999, 1, 888, 10))
            frame.latest_candle_timestamp = frame.candles[-1].timestamp
        self.assertEqual(enrich_review(self.review, frames), self.output)

    def test_deterministic(self):
        self.assertEqual(enrich_review(self.review, self.frames), self.output)

    def test_pipeline_final_output_integration(self):
        from market_reviewer.pipeline import review_snapshot
        with patch('market_reviewer.pipeline.load_snapshot', return_value={'BTC': dict.fromkeys(TIMEFRAME_SECONDS)}), \
             patch('market_reviewer.pipeline.to_market_data_frame', side_effect=list(self.frames.values())), \
             patch('market_reviewer.pipeline.load_review_state', return_value=({}, 'NONE')), \
             patch('market_reviewer.pipeline._review_symbol_with_native_replay', return_value=(self.review, {'unchanged': True})), \
             patch('market_reviewer.pipeline.atomic_write_json') as write:
            out = review_snapshot(None, 'temporary.json')
        self.assertEqual(out['BTC'], self.output)
        self.assertEqual(write.call_args.args[1]['symbols'], {'BTC': {'unchanged': True}})

    def test_eth_output(self):
        frames = copy.deepcopy(self.frames)
        for frame in frames.values():
            frame.symbol = 'ETH'
        review = dict(self.review, Symbol='ETH', State='WATCH')
        out = enrich_review(review, frames)
        self.assertEqual(out['opportunity_alert']['symbol'], 'ETH')
        self.assertEqual(out['opportunity_alert']['alert_level'], 'NORMAL_ALERT')
        self.assertEqual(out['smc_context']['producer_status'], 'AVAILABLE')

    def test_runner_packet_passthrough(self):
        from pathlib import Path
        from market_reviewer.observation_runner import execute_production_observation
        with patch('market_reviewer.observation_runner.review_snapshot', return_value={'BTC': self.output}), \
             patch('market_reviewer.observation_runner._load_frames', return_value={'BTC': self.frames}), \
             patch('market_reviewer.observation_runner._external_by_symbol', return_value={}), \
             patch('market_reviewer.observation_runner._sha256_or_none', return_value='unchanged'):
            payload = execute_production_observation({'market_path': 'unused', 'external_path': 'unused'},
                                                     1, Path('unused'))
        self.assertEqual(payload['reviews']['BTC']['opportunity_alert'], self.output['opportunity_alert'])
        self.assertEqual(payload['production_hash'], 'unchanged')

    def test_opportunity_snapshot_unchanged_by_output(self):
        from market_reviewer.opportunity import extract_opportunity_snapshot
        self.assertEqual(extract_opportunity_snapshot(self.review, self.frames),
                         extract_opportunity_snapshot(self.output, self.frames))
