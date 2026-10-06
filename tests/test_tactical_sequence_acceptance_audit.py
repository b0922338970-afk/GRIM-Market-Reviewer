import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_reviewer import tactical_sequence_acceptance_audit as audit
from market_reviewer import tactical_shadow_capture as cap
from market_reviewer.tactical_sequence_shadow import _inputs
from market_reviewer.persistence import atomic_write_json
from test_tactical_shadow_capture import snapshot


class AcceptanceAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'artifact'
        self.current = {}
        self.ledger = cap._empty()
        self.ledger['activation_observation'] = 301
        self.patcher = patch.object(audit, 'LAST_OBSERVATION', 304)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def append(self, obs, cp, *, forming=False, change=None):
        bundle = dict(capture_version=cap.CAPTURE_VERSION, observation_number=obs, checkpoint=cp,
                      status='AVAILABLE', symbols={sy: snapshot(sy, obs, cp, forming=forming) for sy in cap.SYMBOLS})
        if change:
            change(bundle)
        self.current = {sy: cap._fold(self.current.get(sy), s) for sy, s in bundle['symbols'].items()}
        record = dict(observation_number=obs, checkpoint=cp, input_sha256=cap.digest(bundle),
                      states=copy.deepcopy(self.current), transitions=cap._transitions(self.current, obs, cp),
                      production_hash='frozen', production_isolation_verified=True)
        self.ledger['records'].append(record)
        self.ledger.update(current=copy.deepcopy(self.current), latest_observation=obs)
        atomic_write_json(self.root / 'tactical-shadow-inputs' / f'{obs}.json', bundle)
        atomic_write_json(self.root / cap.LEDGER_NAME, self.ledger)

    def cohort(self, forming=False):
        for obs, cp in zip(range(301, 305), (14400, 16200, 16800, 17400)):
            self.append(obs, cp, forming=forming)

    def report(self):
        result = audit.tactical_sequence_acceptance_audit(self.root)
        self.assertEqual(result['status'], 'PASS', result)
        return result

    def probe(self, change=None):
        frozen = snapshot('BTC', 302, 16200)
        target = snapshot('BTC', 301, 14400)['selected_target']
        inputs = _inputs(frozen['review'], frozen['exposure'], 16200)
        if change:
            change(inputs, target)
        return audit._probe(inputs, target, 'LONG')

    def test_durable_only_and_outside_cohort_never_loaded(self):
        self.cohort()
        self.ledger['records'].insert(0, {'observation_number': 300, 'malformed_history': True})
        self.ledger['records'].append({'observation_number': 305, 'future': True})
        atomic_write_json(self.root / cap.LEDGER_NAME, self.ledger)
        self.assertEqual(self.report()['observations'], 4)
        self.assertEqual(self.report()['sequence_count'], 2)

    def test_incomplete_cohort_unavailable(self):
        self.append(301, 14400)
        self.assertEqual(audit.tactical_sequence_acceptance_audit(self.root)['reason'], 'INCOMPLETE_DURABLE_COHORT')

    def test_wrong_activation_rejected(self):
        self.cohort()
        self.ledger['activation_observation'] = 295
        atomic_write_json(self.root / cap.LEDGER_NAME, self.ledger)
        self.assertEqual(audit.tactical_sequence_acceptance_audit(self.root)['status'], 'UNAVAILABLE')

    def test_first_blocker_deterministic_and_forming_not_validated(self):
        self.cohort(forming=True)
        a = self.report()
        self.assertEqual(a, self.report())
        self.assertEqual(a['first_blockers'], {'TARGET_NEVER_SWEPT': 2})
        self.assertEqual(a['persisted_mss_confirmed'], 0)
        self.assertEqual(a['funnel']['POST_SELECTION_SWEEP_SEEN']['symbol'], {'BTC': 0, 'ETH': 0})

    def test_post_selection_timing_strict(self):
        result = self.probe(lambda inputs, target: target.update(selected_at=15000))
        self.assertTrue(result['target_sweeps'])
        self.assertFalse(result['post_selection_sweeps'])
        self.assertIsNone(result['producer_mss'])

    def test_raw_sweep_different_pool_is_not_linked(self):
        def change(inputs, target):
            inputs['liquidity_events'][0]['pool_id'] = 'other'
        result = self.probe(change)
        self.assertTrue(result['raw_sweeps'])
        self.assertFalse(result['post_selection_sweeps'])

    def test_same_pool_wrong_level_not_linked(self):
        result = self.probe(lambda inputs, target: inputs['liquidity_events'][0]['evidence'].update(level_price=999))
        self.assertTrue(result['identity_mismatched_sweeps'])
        self.assertFalse(result['post_selection_sweeps'])

    def test_raw_displacement_wrong_direction_not_linked(self):
        def change(inputs, target):
            for e in inputs['raw_directional_displacement']:
                e['direction'] = e['evidence']['direction'] = 'BEARISH'
        result = self.probe(change)
        self.assertTrue(result['raw_displacements_after_sweep'])
        self.assertIsNone(result['selected_displacement'])

    def test_raw_mss_wrong_direction_not_contextual(self):
        def change(inputs, target):
            for e in inputs['structure_events']:
                e['direction'] = e['evidence']['direction'] = 'BEARISH'
        result = self.probe(change)
        self.assertTrue(result['raw_mss_after_sweep'])
        self.assertIsNone(result['producer_mss'])

    def test_contextual_candidate_not_persisted_acceptance(self):
        self.assertTrue(self.probe()['producer_mss'])
        self.cohort()
        result = self.report()
        self.assertEqual(result['funnel']['CONTEXTUAL_MSS_ACCEPTED']['TOTAL'], 2)
        self.assertEqual(result['persisted_mss_confirmed'], 2)

    def test_target_identity_preserved(self):
        self.cohort()
        self.assertTrue(all(s['target_identity_preserved'] for s in self.report()['sequences']))

    def test_invalidation_attributed_and_post_terminal_excluded(self):
        self.append(301, 14400, forming=True)
        def withdraw(b):
            for s in b['symbols'].values():
                s['review']['Last_MSS'] = {}
                s['review']['Last_BOS'] = {}
                s['review']['FVG'] = []
                s['review']['Order_Blocks'] = []
                s['exposure']['liquidity_events'] = []
                s.update(tactical_direction='NONE', relationship='NEUTRAL', selected_target=None)
                s['review']['Liquidity'] = []
                s['ranking']['levels'] = []
        self.append(302, 16200, change=withdraw)
        self.append(303, 16800)
        self.append(304, 17400)
        result = self.report()
        self.assertEqual(result['invalidations']['reasons'], {'TACTICAL_DIRECTION_WITHDRAWN': 2})
        self.assertEqual(result['funnel']['POST_SELECTION_SWEEP_SEEN']['TOTAL'], 0)
        self.assertEqual(result['post_terminal_diagnostics']['targets_with_post_selection_sweep'], 2)
        self.assertEqual(result['first_blockers'], {'INVALIDATED_BEFORE_PROGRESS': 2})

    def test_no_churn_in_forward_lifecycle(self):
        self.cohort()
        self.assertFalse(self.report()['churn']['SHADOW_SEQUENCE_CHURN'])

    def test_mixed_sequence_causes_not_hidden_by_one_binding_gap(self):
        self.cohort(forming=True)
        sequences = self.report()['sequences']
        sequences[0]['target_audit']['target_sweep_binding_gap'] = True
        self.assertEqual(audit._assessment(sequences, {'SHADOW_SEQUENCE_CHURN': False}), 'MIXED_LIVE_SEQUENCE_GAP')

    def test_raw_mss_during_invalidation_is_not_accepted_or_producer_defect(self):
        self.append(301, 14400, forming=True)
        def withdraw(b):
            for s in b['symbols'].values():
                s['review'].update(Last_MSS={}, Last_BOS={}, FVG=[], Order_Blocks=[], Liquidity=[])
                s.update(tactical_direction='NONE', relationship='NEUTRAL', selected_target=None)
                s['ranking']['levels'] = []
        self.append(302, 16200, change=withdraw)
        self.append(303, 16800)
        self.append(304, 17400)
        result = self.report()
        self.assertEqual(result['funnel']['CONTEXTUAL_MSS_CANDIDATE']['TOTAL'], 2)
        self.assertEqual(result['funnel']['CONTEXTUAL_MSS_ACCEPTED']['TOTAL'], 0)
        self.assertEqual(result['persisted_mss_confirmed'], 0)
        self.assertTrue(all(s['mss_audit']['status'] == 'NOT_ACCEPTED_LIFECYCLE_INVALIDATED'
                            for s in result['sequences']))
        self.assertNotEqual(result['final_assessment'], 'CONTEXTUAL_MSS_PRODUCER_GAP')

    def test_future_availability_rejected_even_with_recomputed_digest(self):
        self.cohort()
        path = self.root / 'tactical-shadow-inputs/302.json'
        bundle = cap._read(path)
        bundle['symbols']['BTC']['exposure']['raw_directional_displacement'][0]['available_at'] = 16500
        atomic_write_json(path, bundle)
        self.ledger['records'][1]['input_sha256'] = cap.digest(bundle)
        atomic_write_json(self.root / cap.LEDGER_NAME, self.ledger)
        self.assertEqual(audit.tactical_sequence_acceptance_audit(self.root)['status'], 'UNAVAILABLE')

    def test_forged_state_target_identity_rejected(self):
        self.cohort()
        self.ledger['records'][1]['states']['BTC']['active_target']['liquidity_id'] = 'OTHER'
        atomic_write_json(self.root / cap.LEDGER_NAME, self.ledger)
        self.assertEqual(audit.tactical_sequence_acceptance_audit(self.root)['reason'], 'DURABLE_STATE_REPLAY_MISMATCH')

    def test_unexplained_restart_detected(self):
        sequences = [dict(symbol='BTC', selected_target_id='target', sequence_id='a', direction='LONG', invalidation=None),
                     dict(symbol='BTC', selected_target_id='target', sequence_id='b', direction='LONG', invalidation=None)]
        self.assertTrue(audit._churn(sequences)['SHADOW_SEQUENCE_CHURN'])
        self.assertEqual(audit._churn(sequences)['same_target_restart_count'], 1)

    def test_counts_isolate_symbol_direction_relationship(self):
        sequences = [dict(symbol='BTC', direction='SHORT', relationship='COUNTER_TREND'),
                     dict(symbol='ETH', direction='LONG', relationship='ALIGNED')]
        counts = audit._counts(sequences, lambda s: s['direction'] == 'SHORT')
        self.assertEqual(counts['symbol'], {'BTC': 1, 'ETH': 0})
        self.assertEqual(counts['direction'], {'LONG': 0, 'SHORT': 1})
        self.assertEqual(counts['relationship'], {'ALIGNED': 0, 'COUNTER_TREND': 1, 'NEUTRAL': 0})

    def test_repeated_event_receipts_not_new_events(self):
        e = self.probe()['target_sweeps'][0]
        later = dict(e, available_at=e['available_at'] + 300)
        self.assertEqual(len(audit._unique([e, later])), 1)

    def test_corrupt_input_rejected_without_mutation(self):
        self.cohort()
        path = self.root / 'tactical-shadow-inputs/302.json'
        path.write_text('{}', encoding='utf-8')
        before = path.read_bytes()
        self.assertEqual(audit.tactical_sequence_acceptance_audit(self.root)['status'], 'UNAVAILABLE')
        self.assertEqual(path.read_bytes(), before)

    def test_canonical_ledger_journal_and_gate_unchanged(self):
        self.cohort()
        for p in ('observation-commit-journal.json', 'observation-runner.json', 'production-observation-head.json'):
            atomic_write_json(self.root / p, {'protected': p})
        atomic_write_json(self.root.parent / 'research/missed-opportunities.json', {'protected': 'research'})
        atomic_write_json(self.root.parent / 'reviews/thesis-baseline.json', {'protected': 'production'})
        hashes = lambda: {str(p): cap._sha(p) for p in self.root.parent.rglob('*') if p.is_file()}
        before = hashes()
        gate = cap.live_status(self.root)
        with patch.object(cap, 'commit_after_complete', side_effect=AssertionError('Read only')):
            report = self.report()
        self.assertEqual(before, hashes())
        self.assertEqual(gate, cap.live_status(self.root))
        self.assertFalse(report['origin_creation_allowed'])

    def test_default_cli_rejects_incomplete_scope_and_creates_nothing(self):
        self.cohort()
        before = {str(p): cap._sha(p) for p in self.root.rglob('*') if p.is_file()}
        command = [sys.executable, '-m', 'market_reviewer.cli', 'tactical-sequence-acceptance-audit',
                   '--output-dir', str(self.root), '--json']
        first = subprocess.run(command, capture_output=True, text=True)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.returncode, 1)
        self.assertEqual(json.loads(first.stdout)['reason'], 'INCOMPLETE_DURABLE_COHORT')
        self.assertEqual(before, {str(p): cap._sha(p) for p in self.root.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
