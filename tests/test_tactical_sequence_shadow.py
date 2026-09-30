import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from market_reviewer import reviewer as rv
from market_reviewer.cli import main
from market_reviewer.model import Candle, MarketDataFrame
from market_reviewer.tactical_provenance import SCHEMA as EXPOSURE_SCHEMA
from market_reviewer.tactical_sequence_shadow import advance, select_target, tactical_sequence_shadow


def fixture(cp, direction='LONG', primary='BULLISH', symbol='BTC'):
    bias = 'BULLISH' if direction == 'LONG' else 'BEARISH'
    side = 'SELLSIDE' if direction == 'LONG' else 'BUYSIDE'
    level_type = 'Internal Sell-side Liquidity' if direction == 'LONG' else 'Internal Buy-side Liquidity'
    review = dict(Symbol=symbol, Review_Timestamp=str(cp-300), Swing_Bias=primary, Displacement='NONE',
                  Contextual_MSS='NONE', Active_Setup_ID='NONE', Current_Phase='PULLBACK',
                  Last_MSS={'H1': f'{bias} 100 @ 3600', 'M15': f'{bias} 100 @ 7200'})
    def event(eid, tf, ts, kind, data, **extra):
        return dict(event_id=eid, symbol=symbol, timeframe=tf, timestamp=ts, available_at=cp,
                    type=kind, evidence=data, **extra)
    structures = [event(tf, tf, ts, 'MSS', asdict(rv.StructureEvent(bias, 100, ts, 'MSS', 'RANGE', bias, 'test')),
                        direction=bias) for tf, ts in (('H1', 3600), ('M15', 7200))]
    exposure = dict(schema=EXPOSURE_SCHEMA, status='AVAILABLE', symbol=symbol, checkpoint=cp,
                    production_selected_displacement='NONE', structure_events=structures,
                    raw_directional_displacement=[], liquidity_events=[], zones=[])
    level = rv.LiquidityLevel(100 if direction == 'LONG' else 110, level_type, 'H1', 3600, 'UNSWEPT', 'POOL')
    target = {**asdict(level), 'side': side, 'timestamp': 3600, 'available_at': cp,
              'selected_at': cp, 'source_module': 'market_reviewer.reviewer._liquidity_draws'}
    review['Liquidity'] = [asdict(level)]
    if cp >= 16200:
        # H1 sweep must close before cp: use M5 target in lifecycle fixtures.
        target.update(timeframe='M5')
        sweep = rv.LiquidityEvent(level.price, level.type, 'M5', 'SWEPT', 15000, level.price, 1, 'INSIDE')
        exposure['liquidity_events'] = [event('S', 'M5', 15000, 'SWEPT', asdict(sweep), side=side, pool_id='POOL')]
        ds = rv.DisplacementEvent(bias, 'VALID', 15600, 'MSS 100', True, 2, 2, True, False)
        exposure['raw_directional_displacement'] = [event('D', 'M5', 15600, 'DISPLACEMENT', asdict(ds), direction=bias)]
        exposure['structure_events'].append(event('M', 'M5', 15600, 'MSS',
            asdict(rv.StructureEvent(bias, 100, 15600, 'MSS', 'RANGE', bias, 'test')), direction=bias))
        gap = rv.FairValueGap(105, 103, 104, bias, 'M5', 15600, 'FRESH', 0, 2, 'SETUP_FVG', 'M5:15600')
        exposure['zones'] = [event('F', 'M5', 15600, 'FVG', asdict(gap), direction=bias, setup_id=rv._setup_id(gap))]
    target['timeframe'] = 'M5'
    return review, exposure, target


def candle_frame(candles):
    return MarketDataFrame('BTC', 'M5', 'test', 'test', 'spot', 'UTC', 'test', 'test', 'test', 'test',
        'DATA_READY', candles[-1].timestamp+300, candles[-1].timestamp, candles[-1].timestamp, None, candles, 'DATA_READY')


class TacticalSequenceTests(unittest.TestCase):
    def step(self, previous=None, cp=14400, obs=282, direction='LONG', primary='BULLISH', mutate=None, prefix=None):
        r, e, target = fixture(cp, direction, primary)
        if mutate:
            mutate(r, e)
        before = copy.deepcopy((previous, r, e))
        with patch('market_reviewer.tactical_sequence_shadow._verified_prefix', return_value=prefix), \
             patch('market_reviewer.tactical_sequence_shadow.select_target', return_value=(target, None)):
            result = advance(previous, r, e, cp, obs)
        self.assertEqual(before, (previous, r, e))
        self.assertFalse(result['origin_creation_allowed'])
        self.assertFalse(result['canonical'])
        return result

    def setup_state(self):
        first = self.step()
        return self.step(first, 16200, 283)

    def test_countertrend_short_independent_from_primary(self):
        s = self.step(direction='SHORT', primary='BULLISH')
        self.assertEqual(s['tactical_direction'], 'SHORT')
        self.assertEqual(s['primary_direction'], 'LONG')
        self.assertEqual(s['relationship'], 'COUNTER_TREND')
        self.assertEqual(s['sequence_state'], 'FORMING')

    def test_no_swing_bias_fallback(self):
        s = self.step(mutate=lambda r,e: r.update(Last_MSS={}, Last_BOS={}))
        self.assertEqual(s['tactical_direction'], 'NONE')
        self.assertIsNone(s['sequence_id'])

    def test_missing_target_blocks_even_complete_components(self):
        r, e, _ = fixture(16200)
        s = advance(None, r, e, 16200, 282)
        self.assertIsNone(s['contextual_mss'])
        self.assertIsNone(s['active_setup_id'])
        self.assertTrue(s['blockers'][0].startswith('TACTICAL_ACTIVE_TARGET_UNAVAILABLE'))

    def test_linked_mss_uses_existing_producer(self):
        s = self.setup_state()
        self.assertEqual(s['contextual_mss']['related_sweep_id'], 'S')
        self.assertEqual(s['contextual_mss']['related_displacement_id'], 'D')
        self.assertEqual(s['contextual_mss']['trigger_timeframe'], 'M5')
        self.assertEqual(s['sequence_state'], 'SETUP_FORMED')

    def test_missing_mss_blocks_setup(self):
        s = self.step(self.step(), 16200, 283, mutate=lambda r,e: e['structure_events'].pop())
        self.assertEqual(s['blockers'], ['CONTEXTUAL_MSS_MISSING'])
        self.assertIsNone(s['active_setup_id'])

    def test_deterministic_namespaced_setup(self):
        a, b = self.setup_state(), self.setup_state()
        self.assertEqual(a, b)
        self.assertTrue(a['active_setup_id'].startswith('TACTICAL-SETUP-'))
        self.assertNotEqual(a['active_setup_id'], a['detector_setup_id'])

    def test_forward_lifecycle_separate_stages(self):
        s = self.setup_state()
        self.assertEqual([t['new_state'] for t in s['transitions']], ['FORMING','MSS_CONFIRMED','SETUP_FORMED'])
        later = self.step(s, 16800, 284)
        self.assertEqual(later['sequence_state'], 'RETEST_PENDING')
        self.assertEqual(later['active_setup_id'], s['active_setup_id'])
        with self.assertRaisesRegex(ValueError, 'BACKWARD_CHECKPOINT'):
            self.step(later, 16200, 285)

    def test_same_checkpoint_idempotent(self):
        self.assertEqual(self.step(), self.step(self.step()))
        with self.assertRaisesRegex(ValueError, 'CONFLICTING_CHECKPOINT'):
            self.step(self.step(), primary='NONE')

    def test_old_sweep_not_backfilled(self):
        first = self.step(cp=16200)
        s = self.step(first, 16800, 283)
        self.assertEqual(s['sequence_state'], 'FORMING')
        self.assertEqual(s['blockers'], ['MATCHING_POST_SELECTION_SWEEP_MISSING'])

    def test_direction_reversal_invalidates(self):
        s = self.step(self.setup_state(), 16800, 284, direction='SHORT')
        self.assertEqual(s['sequence_state'], 'INVALIDATED')
        self.assertIn('POOL', s['retired_target_ids'])
        self.assertEqual(s['tactical_direction'], 'LONG')
        self.assertEqual(s['observed_tactical_direction'], 'SHORT')
        self.assertEqual(s['relationship'], 'ALIGNED')

    def test_nested_event_type_mismatch_blocks_advancement(self):
        s = self.step(self.step(), 16200, 283,
                      mutate=lambda r,e: e['liquidity_events'][0]['evidence'].update(event_type='RECLAIMED'))
        self.assertEqual(s['evaluation_status'], 'UNAVAILABLE')
        self.assertIsNone(s['contextual_mss'])

    def test_incomplete_detector_payload_suspends_not_crashes(self):
        s = self.step(self.step(), 16200, 283,
                      mutate=lambda r,e: e['raw_directional_displacement'][0]['evidence'].pop('body_ratio'))
        self.assertEqual(s['evaluation_status'], 'UNAVAILABLE')
        self.assertEqual(s['sequence_state'], 'FORMING')

    def test_distinct_pool_cannot_supply_sweep(self):
        s = self.step(self.step(), 16200, 283,
                      mutate=lambda r,e: e['liquidity_events'][0].update(pool_id='OTHER_POOL'))
        self.assertEqual(s['blockers'], ['MATCHING_POST_SELECTION_SWEEP_MISSING'])
        self.assertIsNone(s['contextual_mss'])

    def test_ambiguous_displacement_identity_blocks_mss(self):
        def collision(r,e):
            duplicate = copy.deepcopy(e['raw_directional_displacement'][0])
            duplicate['event_id'] = 'OTHER_DISPLACEMENT'
            e['raw_directional_displacement'].append(duplicate)
        s = self.step(self.step(), 16200, 283, mutate=collision)
        self.assertEqual(s['blockers'], ['DISPLACEMENT_LINKAGE_MISSING_OR_AMBIGUOUS'])
        self.assertIsNone(s['contextual_mss'])

    def test_locked_zone_failure_invalidates(self):
        s = self.step(self.setup_state(), 16800, 284,
                      mutate=lambda r,e: e['zones'][0]['evidence'].update(status='INVALIDATED'))
        self.assertEqual(s['sequence_state'], 'INVALIDATED')

    def test_new_zone_cannot_replace_locked_setup(self):
        old = self.setup_state()
        def replace_zone(r,e):
            e['zones'][0]['event_id'] = 'OTHER'
            e['zones'][0]['setup_id'] = 'OTHER'
            e['zones'][0]['evidence']['upper'] = 106
        s = self.step(old, 16800, 284, mutate=replace_zone)
        self.assertEqual(s['active_setup_id'], old['active_setup_id'])
        self.assertEqual(s['active_setup'], old['active_setup'])

    def test_different_zone_identity_cannot_invalidate_locked_setup(self):
        def unrelated_failure(r,e):
            e['zones'][0]['event_id'] = 'OTHER_ZONE'
            e['zones'][0]['evidence']['status'] = 'INVALIDATED'
        s = self.step(self.setup_state(), 16800, 284, mutate=unrelated_failure)
        self.assertEqual(s['sequence_state'], 'RETEST_PENDING')
        self.assertEqual(s['evaluation_status'], 'UNAVAILABLE')

    def test_confirmed_retest_does_not_create_origin_and_later_failure_terminates(self):
        candles = [Candle(15900,106,107,106,106,1), Candle(16200,106,106,104,105,1),
                   Candle(16500,106,107,106,106,1)]
        s = self.step(self.setup_state(), 16800, 284, prefix={'M5': candle_frame(candles)})
        self.assertEqual(s['sequence_state'], 'RETEST_CONFIRMED')
        self.assertFalse(s['origin_creation_allowed'])
        failed = self.step(s, 17100, 285,
            mutate=lambda r,e: e['zones'][0]['evidence'].update(status='INVALIDATED'))
        self.assertEqual(failed['sequence_state'], 'INVALIDATED')
        self.assertEqual(failed['active_setup_id'], s['active_setup_id'])

    def test_cross_symbol_state_rejected(self):
        r, e, _ = fixture(16200, symbol='ETH')
        with self.assertRaisesRegex(ValueError, 'SHADOW_STATE_IDENTITY_MISMATCH'):
            advance(self.step(), r, e, 16200, 283)

    def test_forward_retest_uses_closed_candles(self):
        candles = [Candle(t, 106, 107, 106, 106, 1) for t in (15900,16200,16500)]
        candles[1] = Candle(16200, 106, 106, 104, 105, 1)
        s = self.step(self.setup_state(), 16800, 284, prefix={'M5': candle_frame(candles)})
        self.assertEqual(s['sequence_state'], 'RETEST_CONFIRMED')
        self.assertEqual(s['eligible_retest']['timestamp'], 16200)
        self.assertEqual(s['eligible_retest']['setup_id'], s['active_setup_id'])

    def test_preselection_touch_not_retest(self):
        candles = [Candle(15900, 106, 106, 104, 105, 1), Candle(16200,106,107,106,106,1), Candle(16500,106,107,106,106,1)]
        s = self.step(self.setup_state(), 16800, 284, prefix={'M5': candle_frame(candles)})
        self.assertFalse(s['eligible_retest']['confirmed'])

    def test_retest_history_gap_not_confirmed(self):
        s = self.step(self.setup_state(), 16800, 284, prefix={'M5': candle_frame([Candle(16500,104,106,104,105,1)])})
        self.assertEqual(s['blockers'], ['RETEST_HISTORY_GAP'])

    def test_future_and_wrong_lineage_rejected(self):
        s = self.step(self.step(), 16200, 283, mutate=lambda r,e: e['raw_directional_displacement'][0].update(available_at=16201))
        self.assertEqual(s['evaluation_status'], 'UNAVAILABLE')
        s = self.step(self.step(), 16200, 283,
                      mutate=lambda r,e: e['zones'][0]['evidence'].update(related_displacement_id='M15:15600'))
        self.assertEqual(s['blockers'], ['FVG_DISPLACEMENT_LINK_MISMATCH'])

    def test_frozen_nested_future_timestamp_rejected(self):
        s = self.step(self.step(), 16200, 283,
                      mutate=lambda r,e: e['structure_events'][-1]['evidence'].update(timestamp=17000))
        self.assertEqual(s['evaluation_status'], 'UNAVAILABLE')

    def test_real_prefix_excludes_future_candle_and_rejects_wrong_boundary(self):
        import test_live_hybrid_smc as fixtures
        from market_reviewer.tactical_provenance import build_exposure
        from market_reviewer.tactical_sequence_shadow import _verified_prefix
        fixtures.LiveHybridSMCTests.setUpClass()
        frames=copy.deepcopy(fixtures.LiveHybridSMCTests.frames)
        review=fixtures.LiveHybridSMCTests.review
        cp=int(review['Review_Timestamp'])+300
        exposure=build_exposure(frames,review,cp)
        frames['M5'].candles.append(Candle(cp+300,100,1000,1,900,1))
        prefix=_verified_prefix(frames,review,exposure,cp)
        self.assertTrue(all(c.timestamp+300<=cp for c in prefix['M5'].candles))
        with self.assertRaisesRegex(ValueError,'SOURCE_BOUNDARY_MISMATCH'):
            _verified_prefix(frames,review,exposure,cp+300)

    def test_no_production_fallback_fields(self):
        first=self.step()
        def change_selected(r,e):
            r['Contextual_MSS']='FAKE PRODUCTION ANCHOR'
            r['Active_Setup_ID']='FAKE PRODUCTION SETUP'
            r['Active_Tactical_Draw']='FAKE TARGET'
        a=self.step(first,16200,283)
        b=self.step(first,16200,283,mutate=change_selected)
        self.assertEqual(a['contextual_mss'],b['contextual_mss'])
        self.assertEqual(a['active_setup_id'],b['active_setup_id'])

    def test_unavailable_checkpoint_preserves_locked_state(self):
        old=self.setup_state()
        s=self.step(old,16800,284,mutate=lambda r,e:e.update(status='UNAVAILABLE'))
        self.assertEqual(s['active_setup_id'],old['active_setup_id'])
        self.assertEqual(s['sequence_state'],old['sequence_state'])

    def test_exact_target_ranking_reused_not_nearest_guess(self):
        r, _, _ = fixture(14400)
        h1 = rv.LiquidityLevel(100,'Internal Sell-side Liquidity','H1',3600,'UNSWEPT','H1-POOL')
        m5 = rv.LiquidityLevel(104,'Internal Sell-side Liquidity','M5',3600,'UNSWEPT','M5-POOL')
        r['Liquidity'] = [asdict(h1), asdict(m5)]
        frames = {tf: tf for tf in ('D1','H4','H1','M15','M5')}
        with patch.object(rv, 'analyze_structure', return_value=None), \
             patch.object(rv, 'find_liquidity', side_effect=lambda f,s: [h1] if f=='H1' else [m5] if f=='M5' else []), \
             patch.object(rv, '_current_price', return_value=105):
            target, reason = select_target(r, frames, 'LONG', 14400, [])
            self.assertIsNone(reason)
            self.assertEqual(target['liquidity_id'], 'H1-POOL')
            self.assertIsNone(select_target(r, frames, 'LONG', 14400, ['H1-POOL'])[0])
            r['Liquidity'].pop(0)
            self.assertIsNone(select_target(r, frames, 'LONG', 14400, [])[0])

    def test_cli_readonly_forward_scope_and_fresh_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r, e, _ = fixture(14400)
            payload = dict(observation_number=282, canonical_checkpoint=14400, reviews={'BTC':r},
                           non_canonical_research_evidence={'tactical_provenance':{'BTC':e}})
            tx = dict(status='COMPLETE',observation_number=282,canonical_checkpoint=14400,recovery_payload=payload)
            old = copy.deepcopy(tx)
            old.update(observation_number=281)
            old['recovery_payload']['observation_number']=281
            historical = dict(copy.deepcopy(tx),sample_source='HISTORICAL_REPLAY')
            journal = root/'journal.json'
            journal.write_text(json.dumps({'transactions':[old,historical,tx,tx]}),encoding='utf-8')
            for name in ('production.json','research.json','runner.json'):
                (root/name).write_text('{"sentinel": 1}',encoding='utf-8')
            digest=lambda:{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
            before=digest()
            result=tactical_sequence_shadow(journal)
            self.assertEqual(result['status'],'PASS')
            self.assertEqual(len(result['forward_observations']),1)
            self.assertEqual(result['metrics']['sequence_started']['count'],0)
            out=io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(['tactical-sequence-shadow','--journal',str(journal),'--json']),0)
            self.assertEqual(json.loads(out.getvalue()),result)
            actual=subprocess.check_output([sys.executable,'-m','market_reviewer.cli','tactical-sequence-shadow','--journal',str(journal),'--json'],text=True)
            self.assertEqual(json.loads(actual),result)
            self.assertEqual(before,digest())

    def test_288_frozen_probe_deterministic_no_persistence(self):
        path=Path('artifact/observation-commit-journal.json')
        if not path.exists():
            self.skipTest('Optional forward archive unavailable')
        before=path.read_bytes()
        tx=next((t for t in json.loads(before)['transactions'] if t.get('observation_number')==288 and t.get('status')=='COMPLETE'),None)
        if tx is None:
            self.skipTest('Optional #288 unavailable')
        from market_reviewer.tactical_sequence_shadow import trace_288
        p=tx['recovery_payload']
        r=p['reviews']['ETH']
        e=p['non_canonical_research_evidence']['tactical_provenance']['ETH']
        s=advance(None,r,e,p['canonical_checkpoint'],288)
        a=trace_288(r,e,p['canonical_checkpoint'],s)
        self.assertEqual(a,trace_288(r,e,p['canonical_checkpoint'],s))
        self.assertEqual(a['conditional_mss_count'],4)
        self.assertEqual(a['conditional_setup_count'],0)
        self.assertFalse(a['can_establish_SETUP_FORMED'])
        self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
