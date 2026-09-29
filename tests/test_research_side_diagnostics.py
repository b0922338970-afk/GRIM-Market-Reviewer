import copy
import io
import json
import unittest
from contextlib import redirect_stdout

from market_reviewer.cli import main
from market_reviewer.research_maturity import research_maturity
from market_reviewer.research_side_diagnostics import research_side_diagnostics, _candidate_rows, _facts
import test_research_maturity as fixtures


class SideDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchMaturityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.fixture.populate(5, 5)
        for r in self.f.records:
            r['snapshots'][0]['opportunity_evidence'] = {
                'STRUCTURE': 'POSITIVE', 'MOMENTUM': 'POSITIVE',
                'POSITIONING': 'NEUTRAL', 'LIQUIDITY': 'NEUTRAL'}
        self.f.save()
        self.journal = self.f.base / 'journal.json'
        self.transactions = [self.tx(r) for r in self.f.records]
        self.save()

    def tx(self, r):
        stamp = r['origin_snapshot_timestamp']
        return {'status': 'COMPLETE', 'observation_number': r['origin_observation'],
            'canonical_checkpoint': stamp + 300, 'recovery_payload': {
                'observation_number': r['origin_observation'],
                'reviews': {r['symbol']: {'Review_Timestamp': str(stamp),
                    'Swing_Bias': 'BEARISH' if r['direction'] == 'SHORT' else 'BULLISH',
                    'Sequence_State': 'INVALIDATED', 'Sequence_Transitions': []}},
                'opportunity_snapshots': {r['symbol']: {'snapshot_timestamp': stamp, 'risk_signatures': []}}}}

    def save(self):
        self.journal.write_text(json.dumps({'transactions': self.transactions}))
        self.f.save()

    def result(self):
        self.save()
        return research_side_diagnostics(self.journal, self.f.live, self.f.root, self.f.outcomes)

    def test_sides_separate(self):
        s = self.result()['segments']['full_window']['sides']
        for side in ['LONG', 'SHORT']:
            self.assertEqual(s[side]['candidate'], 5)
            self.assertEqual(s[side]['origin_created'], 5)
            self.assertEqual(s[side]['outcome_complete'], 5)

    def test_diagnostics_gate_and_canonical_immutable(self):
        production = self.f.base / 'production.json'
        production.write_text('{}')
        runner = self.f.base / 'runner.json'
        runner.write_text('{}')
        gate = research_maturity(self.f.root, self.f.live, self.f.outcomes)
        before = {str(p): p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()}
        research_side_diagnostics(self.journal, self.f.live, self.f.root, self.f.outcomes)
        self.assertEqual(gate, research_maturity(self.f.root, self.f.live, self.f.outcomes))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()})

    def test_unclassifiable_not_complete(self):
        del self.f.records[5]['snapshots'][0]['smc_state']
        s = self.result()['segments']['full_window']['sides']['SHORT']
        self.assertEqual(s['origin_created'], 5)
        self.assertEqual(s['outcome_complete'], 4)

    def test_historical_excluded(self):
        self.f.records[5]['sample_source'] = 'HISTORICAL_REPLAY'
        self.transactions[5]['sample_source'] = 'HISTORICAL_REPLAY'
        self.assertEqual(self.result()['segments']['full_window']['sides']['SHORT']['origin_created'], 4)

    def test_missing_evidence_not_guessed(self):
        extra = copy.deepcopy(self.transactions[-1])
        extra['observation_number'] += 1
        extra['recovery_payload']['observation_number'] += 1
        self.transactions.append(extra)
        s = self.result()['segments']['full_window']['sides']['SHORT']
        self.assertEqual(s['structure_qualified']['count'], 'UNAVAILABLE')
        self.assertEqual(s['score_coverage'], {'known': 5, 'candidates': 6})
        self.assertEqual(s['legal_genesis_eligible'], 'UNAVAILABLE')

    def test_reasons_deterministic_supported_only(self):
        extra = copy.deepcopy(self.transactions[-1])
        extra['observation_number'] += 1
        extra['recovery_payload']['observation_number'] += 1
        review = next(iter(extra['recovery_payload']['reviews'].values()))
        review['Sequence_State'] = 'RETEST_PENDING'
        self.transactions.append(extra)
        first = self.result()
        self.transactions.reverse()
        self.assertEqual(first, self.result())
        s = first['segments']['full_window']['sides']['SHORT']
        self.assertEqual(s['rejections'][0]['reason'], 'PRODUCTION_CONTEXT_ACTIVE')
        self.assertEqual(s['rejections'][0]['percentage'], 100)
        self.assertEqual(s['formal_rejection_report'], 'UNAVAILABLE')

    def test_segmentation_deterministic(self):
        self.f.records = []
        self.transactions = []
        for i, stamp in enumerate([1000200, 1000200+86400, 1000200+259200, 1000200+345600]):
            r = {'origin_snapshot_timestamp': stamp, 'origin_observation': 100+i, 'symbol': 'BTC', 'direction': 'SHORT'}
            self.transactions.append(self.tx(r))
        result = self.result()['segments']
        self.assertEqual(result['latest_24H']['sides']['SHORT']['candidate'], 2)
        self.assertEqual(result['latest_72H']['sides']['SHORT']['candidate'], 3)
        self.assertEqual(result['full_window']['sides']['SHORT']['candidate'], 4)

    def test_counts_alone_do_not_diagnose_causality(self):
        result = self.result()
        self.assertEqual(result['assessment'], 'INSUFFICIENT_EVIDENCE_TO_DIAGNOSE')
        self.assertIn('NO_MATCHED_DIRECTIONAL_PIPELINE_CONTROL', result['assessment_reasons'])

    def test_duplicate_journal_not_counted(self):
        self.transactions.append(copy.deepcopy(self.transactions[-1]))
        self.assertEqual(self.result()['segments']['full_window']['sides']['SHORT']['candidate'], 5)

    def test_future_snapshot_excluded(self):
        r = copy.deepcopy(self.f.records[0])
        r['snapshots'][0]['available_at'] = 9999999999
        rows, conflicts = _candidate_rows({'transactions': [self.tx(r)]}, [r])
        self.assertEqual(rows, [])
        self.assertEqual(conflicts, 1)

    def test_weighted_predicate_matches_existing(self):
        r = {'candidate': {'production_sequence_state': 'INVALIDATED',
             'new_legal_genesis_active': False, 'risk_signatures': []},
             'evidence': {'STRUCTURE': 'POSITIVE', 'MOMENTUM': 'POSITIVE',
                          'LIQUIDITY': 'NEUTRAL', 'POSITIONING': 'NEUTRAL'}}
        self.assertEqual(_facts(r)[1:4], (4, True, True))

    def test_cli_read_only_json(self):
        before = self.f.live.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['research-side-diagnostics', '--journal', str(self.journal),
                '--live-store', str(self.f.live), '--root', str(self.f.root),
                '--historical-outcomes', str(self.f.outcomes), '--json']), 0)
        self.assertTrue(json.loads(output.getvalue())['read_only'])
        self.assertEqual(before, self.f.live.read_bytes())

    def test_missing_archive_unavailable(self):
        self.journal.unlink()
        r = research_side_diagnostics(self.journal, self.f.live, self.f.root, self.f.outcomes)
        self.assertEqual(r['segments'], {})
        self.assertEqual(r['assessment_reasons'], ['SOURCE_UNAVAILABLE_INVALID_OR_CHANGED'])

    def test_none_bias_is_non_candidate_not_short_rejection(self):
        self.f.records = []
        self.transactions = [self.transactions[0]]
        review = next(iter(self.transactions[0]['recovery_payload']['reviews'].values()))
        review['Swing_Bias'] = 'NONE'
        sides = self.result()['segments']['full_window']['sides']
        self.assertEqual(sides['LONG']['candidate'], 0)
        self.assertEqual(sides['SHORT']['candidate'], 0)
        self.assertEqual(sides['SHORT']['rejections'], [])

    def test_legacy_neutral_origin_report_only(self):
        r = self.f.records[0]
        r['snapshots'][0]['opportunity_evidence']['SWING_BIAS'] = 'NONE'
        next(iter(self.transactions[0]['recovery_payload']['reviews'].values()))['Swing_Bias'] = 'NONE'
        before = copy.deepcopy(r)
        result = self.result()
        self.assertEqual(result['view'], 'CORRECTED_DIAGNOSTIC_VIEW')
        row = next(o for o in result['legacy_origin_audit'] if o['origin_id'] == r['tracker_id'])
        self.assertEqual(row['routing_artifact'], 'LEGACY_DIRECTION_ROUTING_ARTIFACT')
        self.assertEqual(row['persisted_direction'], 'LONG')
        self.assertTrue(row['classifiable_complete'])
        self.assertEqual(self.f.records[0], before)
        self.assertEqual(result['segments']['full_window']['no_direction_non_candidates'], 1)
