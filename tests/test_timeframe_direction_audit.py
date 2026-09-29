import copy
import io
import json
import unittest
from contextlib import redirect_stdout

from market_reviewer.cli import main
from market_reviewer.research_maturity import research_maturity
from market_reviewer.timeframe_direction_audit import (
    bias_bucket, routing_contract, timeframe_roles, tactical_evidence, timeframe_direction_audit,
)
import test_research_side_diagnostics as fixtures


class TimeframeDirectionAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SideDiagnosticsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f

    def audit(self):
        self.fixture.save()
        return timeframe_direction_audit(self.fixture.journal, self.f.live, self.f.root, self.f.outcomes)

    def test_none_distinct_not_bullish(self):
        self.assertEqual(bias_bucket('NONE'), 'NONE')
        self.assertEqual(bias_bucket(None), 'UNAVAILABLE')
        self.assertEqual(bias_bucket('ODD'), 'OTHER')

    def test_mapping_reproduces_default(self):
        c = routing_contract()
        self.assertEqual(c['research']['mapping']['NONE'], 'LONG')
        self.assertEqual(c['research']['mapping']['BEARISH'], 'SHORT')
        self.assertIn('else "NONE"', c['reviewer']['branches'][0][1])

    def test_roles_deterministic(self):
        self.assertEqual(timeframe_roles(), timeframe_roles())
        self.assertEqual(timeframe_roles()['implemented_groups']['HTF'], ['D1', 'H4'])
        self.assertFalse(timeframe_roles()['tactical_short_supported'])

    def test_symbol_distribution(self):
        self.f.records = []
        payload = self.fixture.transactions[0]['recovery_payload']
        old = next(iter(payload['reviews']))
        review = payload['reviews'][old]
        opportunity = payload['opportunity_snapshots'][old]
        payload['reviews'] = {'BTC': review}
        payload['opportunity_snapshots'] = {'BTC': opportunity}
        self.fixture.transactions = [self.fixture.transactions[0], copy.deepcopy(self.fixture.transactions[0])]
        tx = self.fixture.transactions[1]
        payload = tx['recovery_payload']
        symbol = next(iter(payload['reviews']))
        payload['reviews']['ETH'] = payload['reviews'].pop(symbol)
        payload['opportunity_snapshots']['ETH'] = payload['opportunity_snapshots'].pop(symbol)
        payload['reviews']['ETH']['Swing_Bias'] = 'NONE'
        r = self.audit()['segments']['full_window']['bias']
        self.assertEqual(r['BTC']['BULLISH'], 1)
        self.assertEqual(r['ETH']['NONE'], 1)
        self.assertEqual(r['ETH']['BULLISH'], 0)

    def test_tactical_evidence_does_not_promote(self):
        self.f.records = []
        self.fixture.transactions = [self.fixture.transactions[0]]
        review = next(iter(self.fixture.transactions[0]['recovery_payload']['reviews'].values()))
        stamp = int(review['Review_Timestamp'])
        review.update(Swing_Bias='NONE', Preferred_Direction='NONE',
            Last_MSS={'H1': f'BEARISH 100.00 @ {stamp-3600}'})
        r = self.audit()['segments']['full_window']
        symbol = next(iter(self.fixture.transactions[0]['recovery_payload']['reviews']))
        example = r['tactical_short_context'][symbol]['examples'][0]
        self.assertTrue(example['flags']['H1_BEARISH_MSS'])
        self.assertEqual(example['reviewer_direction'], 'NONE')
        self.assertEqual(example['research_direction'], 'LONG')
        self.assertEqual(r['bias'][symbol]['NONE'], 1)

    def test_open_candle_excluded(self):
        r = {'Last_MSS': {'H1': 'BEARISH 100 @ 1000'},
             'FVG': [{'direction': 'BEARISH','timeframe':'M5','formed_at':4500}]}
        flags = tactical_evidence(r, 4600)
        self.assertTrue(flags['H1_BEARISH_MSS'])
        self.assertFalse(flags['BEARISH_FVG'])
        self.assertIsNone(flags['BEARISH_OB'])

    def test_readonly_cli_and_gate(self):
        production = self.f.base / 'production.json'
        production.write_text('{}')
        gate = research_maturity(self.f.root, self.f.live, self.f.outcomes)
        before = {p:p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()}
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['timeframe-direction-audit', '--journal', str(self.fixture.journal),
                '--live-store',str(self.f.live),'--root',str(self.f.root),
                '--historical-outcomes',str(self.f.outcomes),'--json']),0)
        self.assertTrue(json.loads(out.getvalue())['read_only'])
        self.assertEqual(gate,research_maturity(self.f.root,self.f.live,self.f.outcomes))
        self.assertEqual(before,{p:p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()})

    def test_segmentation_order_determinism(self):
        first = self.audit()
        self.fixture.transactions.reverse()
        self.assertEqual(first,self.audit())
        self.assertEqual(first['segments']['latest_24H']['end']-first['segments']['latest_24H']['start'],86400)
        self.assertEqual(first['segments']['latest_72H']['end']-first['segments']['latest_72H']['start'],259200)

    def test_conflicting_exposed_evidence_fail_closed(self):
        tx = copy.deepcopy(self.fixture.transactions[0])
        next(iter(tx['recovery_payload']['reviews'].values()))['Last_MSS'] = {'H1':'NONE'}
        self.fixture.transactions.append(tx)
        self.assertEqual(self.audit()['assessment'],'INSUFFICIENT_EVIDENCE')
