import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from market_reviewer.cli import main
from market_reviewer.tactical_chain_audit import (
    PRODUCERS, tactical_chain_audit, trace_components_safely, probe_producers)
from test_tactical_provenance import chain


class ChainAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'journal.json'
        self.review, self.exposure = chain()
        self.review.update(Contextual_MSS='NONE', Active_Setup_ID='NONE')
        self.tx = dict(status='COMPLETE', observation_number=282, canonical_checkpoint=12000,
                       recovery_payload=dict(observation_number=282, canonical_checkpoint=12000,
                           reviews={'BTC': self.review},
                           non_canonical_research_evidence={'tactical_provenance': {'BTC': self.exposure}}))

    def run_audit(self, txs=None):
        self.path.write_text(json.dumps({'transactions': txs or [self.tx]}), encoding='utf-8')
        return tactical_chain_audit(self.path)

    def test_primary_and_raw_separate_no_automatic_chain(self):
        result = self.run_audit()
        row = result['baseline_282'][0]
        self.assertEqual(row['trace']['production']['Contextual_MSS'], 'NONE')
        self.assertEqual(row['trace']['counts']['matching_raw_displacement'], 1)
        self.assertFalse(row['trace']['component_presence_is_linkage'])
        self.assertEqual(row['Contextual_MSS_classification'], 'PRIMARY_BIAS_FILTERED')
        self.assertNotEqual(row['binding']['status'], 'BOUND')
        self.assertFalse(result['origin_creation_allowed'])

    def test_matching_primary_missing_not_market_absence(self):
        self.review['Swing_Bias'] = 'BEARISH'
        row = self.run_audit()['baseline_282'][0]
        self.assertEqual(row['Contextual_MSS_classification'], 'UNKNOWN')
        self.assertEqual(row['Active_Setup_classification'], 'UNKNOWN')

    def test_future_component_rejected(self):
        self.exposure['raw_directional_displacement'][0]['available_at'] = 12001
        row = self.run_audit()['baseline_282'][0]
        self.assertNotIn('trace', row)
        self.assertFalse(row['validation']['valid'])

    def test_future_zone_excluded_not_used_for_binding(self):
        self.exposure['zones'][0]['available_at'] = 12001
        row = self.run_audit()['baseline_282'][0]
        self.assertEqual(row['component_validation']['excluded_invalid_zone_records'], 1)
        self.assertEqual(row['trace']['counts']['matching_setup_FVG'], 0)
        self.assertEqual(row['binding']['reason'], 'FULL_EXPOSURE_INVALID')

    def test_nested_future_timestamp_cannot_enter_probe(self):
        self.exposure['raw_directional_displacement'][0]['evidence']['timestamp'] = 12001
        result = probe_producers(None, self.review, self.exposure, 12000)
        self.assertEqual(result['reason'], 'FROZEN_PAYLOAD_IDENTITY_MISMATCH')

    def test_frozen_helpers_do_not_run_detectors_or_create_authority(self):
        from market_reviewer import reviewer as rv
        from dataclasses import asdict
        for event in self.exposure['structure_events']:
            event['evidence'] = asdict(rv.StructureEvent('BEARISH', 100, event['timestamp'], 'MSS', 'BULLISH', 'BEARISH', 'test'))
        for event in self.exposure['liquidity_events']:
            event['evidence'].update(sweep_price=101, penetration=1, close_location='INSIDE')
        # No FVG input: successful conditional MSS is not an active setup.
        self.exposure['zones'] = []
        with patch.object(rv, 'analyze_structure', side_effect=AssertionError('No redetection')):
            result = probe_producers(None, self.review, self.exposure, 12000)
        self.assertEqual(result['mss_returned'], 1)
        self.assertEqual(result['setup_returned'], 0)
        self.assertFalse(result['trials'][0]['active_setup_authorized'])
        self.assertFalse(result['trials'][0]['production_eligible_target_proven'])

    def test_collision_does_not_authorize_ancestry(self):
        zone = copy.deepcopy(self.exposure['zones'][0])
        zone['evidence']['status'] = 'MITIGATED'
        self.exposure['zones'].append(zone)
        row = self.run_audit()['baseline_282'][0]
        self.assertFalse(row['validation']['valid'])
        self.assertEqual(row['trace']['counts']['matching_raw_displacement'], 1)
        self.assertFalse(row['component_validation']['identity_binding_authorized'])
        self.assertEqual(row['probe']['status'], 'UNAVAILABLE')

    def test_forward_cohort_and_symbols_separate(self):
        eth = copy.deepcopy(self.exposure)
        eth['symbol'] = 'ETH'
        for key in ('zones', 'structure_events', 'liquidity_events', 'raw_directional_displacement'):
            for event in eth[key]:
                event['symbol'] = 'ETH'
        self.tx['recovery_payload']['reviews']['ETH'] = dict(self.review, Symbol='ETH')
        self.tx['recovery_payload']['non_canonical_research_evidence']['tactical_provenance']['ETH'] = eth
        historical = dict(copy.deepcopy(self.tx), sample_source='HISTORICAL_REPLAY')
        pending = dict(copy.deepcopy(self.tx), status='PREPARED')
        unavailable = copy.deepcopy(self.tx)
        unavailable['recovery_payload'].pop('non_canonical_research_evidence')
        rows = self.run_audit([historical, pending, unavailable, self.tx, self.tx])['forward_observations']
        self.assertEqual([(r['observation'], r['symbol']) for r in rows], [(282, 'BTC'), (282, 'ETH')])

    def test_identity_mismatch_fails_closed(self):
        self.tx['recovery_payload']['observation_number'] = 281
        self.assertEqual(self.run_audit()['status'], 'UNAVAILABLE')

    def test_no_origin_or_state_mutation(self):
        self.run_audit()
        for name in ('production.json', 'research.json', 'runner.json'):
            (self.root / name).write_text('{"sentinel": true}', encoding='utf-8')
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        inputs = copy.deepcopy((self.review, self.exposure))
        with patch('market_reviewer.reviewer.review_symbol', side_effect=AssertionError('No production review')):
            result = tactical_chain_audit(self.path)
            trace_components_safely(self.review, self.exposure, 12000)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(inputs, (self.review, self.exposure))
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()})

    def test_cli_and_fresh_process_determinism(self):
        expected = self.run_audit()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['tactical-chain-audit', '--journal', str(self.path), '--json']), 0)
        self.assertEqual(json.loads(out.getvalue()), expected)
        command = [sys.executable, '-m', 'market_reviewer.cli', 'tactical-chain-audit', '--journal', str(self.path), '--json']
        self.assertEqual(json.loads(subprocess.check_output(command, text=True)), expected)

    def test_producer_contract_and_conditional_probe_not_authority(self):
        for event in self.exposure['liquidity_events']:
            event['evidence'].update(sweep_price=101, penetration=1, close_location='INSIDE')
        self.assertTrue(PRODUCERS['contextual_mss']['CONTEXTUAL_MSS_PRIMARY_BIAS_FILTERED'])
        self.assertTrue(PRODUCERS['active_setup']['ACTIVE_SETUP_PRIMARY_SEQUENCE_ONLY'])
        self.assertEqual(PRODUCERS['active_setup']['zone_types'], ['SETUP_FVG'])
        from types import SimpleNamespace
        with patch('market_reviewer.tactical_chain_audit.legal_prefix', return_value={tf: object() for tf in ('D1','H4','H1','M15','M5')}), \
             patch('market_reviewer.reviewer.analyze_structure', return_value=SimpleNamespace(events=[])), \
             patch('market_reviewer.reviewer.find_displacements', return_value=[]), \
             patch('market_reviewer.reviewer.find_fvgs', return_value=[]):
            probe = probe_producers({}, self.review, self.exposure, 12000)
        self.assertTrue(probe['not_market_absence_proof'])
        self.assertEqual(probe['mss_returned'], 0)
        self.assertEqual(len(probe['trials']), 1)
        self.assertFalse(probe['trials'][0]['active_setup_authorized'])


if __name__ == '__main__':
    unittest.main()
