import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from market_reviewer.website_read_model import build_website_read_model, _latest_reviews, _liquidation_status_read_only


class WebsiteReadModelV45Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'observation-commit-journal.json'
        self.review = self.root / 'production.json'
        self.research = self.root / 'research.json'
        self.review.write_text(json.dumps({'symbols': {'BTC': {'review_state': 'ARMED'}}}))
        self.research.write_text('{}')
        self.row = {'Market_Regime': 'TREND_CONTINUATION', 'opportunity_alert': {
            'review_state': 'WATCH', 'direction': 'LONG', 'timeframe': 'M5', 'market_story': 'Frozen review',
            'quality_context': {'contextual_mss': {'availability': 'AVAILABLE', 'value': 'CONTEXTUAL'},
                                'active_draw': {'availability': 'AVAILABLE', 'value': {'htf': {'availability': 'AVAILABLE', 'value': 123}}},
                                'ob_state': {'availability': 'UNAVAILABLE', 'value': 'do not expose', 'reason': 'EVIDENCE_NOT_EXPOSED'}}}}
        self.write([self.tx(250), self.tx(251, reviews=None)])

    def tx(self, number, reviews=True, status='COMPLETE'):
        return {'observation_number': number, 'status': status, 'recovery_payload': {
            'reviews': {'BTC': self.row, 'ETH': self.row} if reviews is True else reviews}}

    def write(self, transactions):
        self.journal.write_text(json.dumps({'transactions': transactions}))

    def model(self):
        with ExitStack() as stack:
            values = {'observation_runner_status': {'production_latest_observation': 251, 'research_latest_observation': 251},
                      'missed_opportunity_status': {}, 'smc_live_sample_status': {},
                      'notification_status': {'enabled_channels': [], 'last_delivery': {'channel': 'TELEGRAM'}},
                      '_liquidation_status_read_only': [
                          {'symbol': 'BTC', 'connected': True, 'disconnect_count': 2, 'stored_event_count': 0, 'last_transport_alive_at': 100},
                          {'symbol': 'ETHUSDT', 'connected': False, 'disconnect_count': 3, 'stored_event_count': 9, 'last_transport_alive_at': 90}]}
            for name, value in values.items():
                stack.enter_context(patch('market_reviewer.website_read_model.'+name, return_value=value))
            return build_website_read_model(review_state_path=self.review, research_store_path=self.research,
                                            runner_state_path=self.root/'runner.json', clock=lambda: 123)

    def test_latest_missing_reviews_falls_back(self):
        result = self.model()
        self.assertEqual(result['source']['review_observation'], 250)
        self.assertEqual(result['runtime']['production_latest_observation'], 251)
        self.assertEqual(result['symbols']['BTC']['review_state'], 'WATCH')

    def test_newest_valid_selected_regardless_list_order(self):
        self.write([self.tx(251), self.tx(250)])
        self.assertEqual(self.model()['source']['review_observation'], 251)

    def test_invalid_status_and_malformed_reviews_skipped(self):
        self.write([self.tx(250), self.tx(251, status='PREPARED'), self.tx(252, reviews=[]), self.tx(253, reviews={'SOL': {}})])
        self.assertEqual(self.model()['source']['review_observation'], 250)

    def test_committed_statuses_accepted(self):
        for status in ('PRODUCTION_COMMITTED', 'RESEARCH_PENDING', 'COMPLETE'):
            self.assertEqual(_latest_reviews({'transactions': [self.tx(250, status=status)]})[0], 250)

    def test_no_journal_does_not_fallback_to_thesis(self):
        self.journal.unlink()
        result = self.model()
        self.assertIsNone(result['source']['review_observation'])
        self.assertEqual(result['symbols']['BTC']['review_state'], 'UNAVAILABLE')

    def test_all_alert_mappings_and_unwrap(self):
        result = self.model()['symbols']['BTC']
        self.assertEqual(result['direction'], 'LONG')
        self.assertEqual(result['timeframe'], 'M5')
        self.assertEqual(result['htf_regime'], 'TREND_CONTINUATION')
        self.assertEqual(result['market_story'], 'Frozen review')
        self.assertEqual(result['structure']['MSS'], 'CONTEXTUAL')
        self.assertEqual(result['structure']['active_draw'], {'htf': 123})
        self.assertEqual(result['structure']['OB'], 'UNAVAILABLE')
        self.assertEqual(result['structure']['FVG'], 'UNAVAILABLE')

    def test_liquidation_list_per_symbol(self):
        values = self.model()['evidence']['liquidation']
        self.assertEqual(values['BTC']['status'], 'CONNECTED')
        self.assertEqual(values['ETH']['status'], 'DISCONNECTED')
        self.assertEqual(values['BTC']['stored_event_count'], 0)
        self.assertEqual(values['ETH']['stored_event_count'], 9)
        self.assertEqual(values['BTC']['disconnect_count'], 2)
        self.assertEqual(values['ETH']['last_transport_alive_at'], 90)

    def test_execution_reserved(self):
        result = self.model()['execution']
        self.assertEqual(result['status'], 'RESERVED')
        self.assertEqual(result['mode'], 'NOT_CONNECTED_YET')
        self.assertFalse(result['connected'])

    def test_no_files_created_or_modified(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.model()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_liquidation_read_does_not_repair_dead_process(self):
        coverage = {'collector_process_id': 123, 'collector_stopped_at': None,
                    'coverage_intervals': [{'start': 10, 'end': None, 'status': 'CONNECTED'}],
                    'last_transport_alive_at': 20}
        with patch('market_reviewer.website_read_model.CoverageStore') as store, \
             patch('market_reviewer.website_read_model.LiquidationEventStore') as events, \
             patch('market_reviewer.website_read_model._has_open_connected_interval', return_value=True), \
             patch('market_reviewer.website_read_model._process_is_alive', return_value=False):
            store.return_value.load.return_value = coverage
            events.return_value.event_count.return_value = 0
            rows = _liquidation_status_read_only(self.root)
            self.assertFalse(rows[0]['connected'])
            store.return_value.save.assert_not_called()
            store.return_value.mark_stopped.assert_not_called()

    def test_missing_runtime_read_does_not_create_files(self):
        empty = self.root/'missing'
        build_website_read_model(review_state_path=empty/'production.json', runner_state_path=empty/'runner.json',
            research_store_path=empty/'research.json', liquidation_root=empty/'liquidations',
            notification_journal_path=empty/'notification.json', smc_root=empty/'smc', historical_outcomes_path=empty/'outcomes.json')
        self.assertFalse(empty.exists())


if __name__ == '__main__':
    unittest.main()
