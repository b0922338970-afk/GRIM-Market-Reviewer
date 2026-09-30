import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from market_reviewer import observation_runner as runner
from market_reviewer import tactical_shadow_capture as cap
from market_reviewer import reviewer as rv
from market_reviewer.persistence import atomic_write_json
from market_reviewer.tactical_direction_shadow import classify_tactical
from market_reviewer.tactical_provenance import build_exposure
import test_tactical_sequence_shadow as fixtures
import test_live_hybrid_smc as live


def snapshot(symbol, obs, cp, *, direction='LONG', forming=False):
    review, exposure, target = fixtures.fixture(cp, direction=direction, symbol=symbol)
    level = {k: target[k] for k in rv.LiquidityLevel.__dataclass_fields__}
    review['Liquidity'] = [level]
    if forming:
        exposure['liquidity_events'] = []
    d = classify_tactical(review, cp)
    s = dict(schema=cap.INPUT_SCHEMA, capture_version=cap.CAPTURE_VERSION, observation_number=obs,
             checkpoint=cp, symbol=symbol, review=review, exposure=exposure,
             primary_direction=d['primary_direction'], tactical_direction=d['tactical_direction'], relationship=d['relationship'],
             activation_status='SHADOW_ONLY', canonical=False, origin_creation_allowed=False,
             ranking=dict(levels=[level], price=105, producer='market_reviewer.reviewer._liquidity_draws'),
             selected_target=target, selection_reason=None, retest_scopes={})
    if cp >= 16800:
        gap = rv.FairValueGap(**exposure['zones'][0]['evidence'])
        candles = [fixtures.Candle(t,106,107,106,106,1) for t in range(15900,cp,300)]
        candles[1] = fixtures.Candle(16200,106,106,104,105,1)
        s['retest_scopes'][cap._zone_key(gap)] = cap._retest_scope({'M5': fixtures.candle_frame(candles)}, gap)
    cap.validate_snapshot(s)
    return s


class DurableShadowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = runner.RunnerConfig(output_dir=self.root/'artifact', state_path=self.root/'production.json',
                                      research_tracker_path=self.root/'research.json')
        atomic_write_json(self.cfg.state_path, {'protected': 'production'})
        atomic_write_json(self.cfg.research_tracker_path, {'protected': 'research'})
        self.transactions = []

    def bundle(self, obs=295, cp=14400, forming=False):
        return dict(capture_version=cap.CAPTURE_VERSION, observation_number=obs, checkpoint=cp, status='AVAILABLE',
                    symbols={s: snapshot(s, obs, cp, forming=forming) for s in cap.SYMBOLS})

    def seed(self, bundle=None, status='COMPLETE'):
        b = bundle or self.bundle()
        tx = dict(observation_number=b['observation_number'], canonical_checkpoint=b['checkpoint'], status=status,
                  production_hash=cap._sha(self.cfg.state_path), research_status='COMPLETE' if status=='COMPLETE' else 'PENDING',
                  recovery_payload=dict(non_canonical_research_evidence={cap.KEY:b}))
        self.transactions = [t for t in self.transactions if t['observation_number'] != b['observation_number']] + [tx]
        atomic_write_json(self.cfg.commit_journal_path, {'transactions': self.transactions})
        return b

    def commit(self, obs=295, cp=14400, forming=False):
        self.seed(self.bundle(obs,cp,forming))
        return cap.commit_after_complete(self.cfg, obs)

    def ledger(self):
        return cap._load_ledger(self.cfg.output_dir/cap.LEDGER_NAME)

    def test_complete_boundary_and_hashes(self):
        self.seed(status='RESEARCH_PENDING')
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'SKIPPED_NOT_COMPLETE')
        self.assertFalse((self.cfg.output_dir/cap.LEDGER_NAME).exists())
        self.seed()
        paths = [self.cfg.state_path,self.cfg.research_tracker_path,self.cfg.commit_journal_path]
        before = [p.read_bytes() for p in paths]
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'PERSISTED')
        self.assertEqual(before,[p.read_bytes() for p in paths])

    def test_idempotence_and_conflicting_retry(self):
        self.commit()
        before = (self.cfg.output_dir/cap.LEDGER_NAME).read_bytes()
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'NOOP')
        self.assertEqual(before,(self.cfg.output_dir/cap.LEDGER_NAME).read_bytes())
        b = self.bundle()
        b['symbols']['BTC']['selection_reason']='changed'
        self.seed(b)
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'CONFLICT')
        self.assertEqual(before,(self.cfg.output_dir/cap.LEDGER_NAME).read_bytes())
        self.assertEqual(cap.live_status(self.cfg.output_dir)['conflicts'],1)

    def test_unavailable_retry_is_conflict_not_overwrite(self):
        self.commit()
        b = self.bundle()
        b['status'] = 'UNAVAILABLE'
        self.seed(b)
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'CONFLICT')
        self.assertEqual(len(self.ledger()['records']),1)

    def test_removed_valid_target_is_detected(self):
        s = snapshot('BTC',295,14400)
        s['selected_target'] = None
        with self.assertRaisesRegex(ValueError,'MISSING_DETERMINISTIC_TARGET'):
            cap.validate_snapshot(s)

    def test_complete_requires_research_receipt(self):
        self.seed()
        self.transactions[0]['research_status'] = 'PENDING'
        atomic_write_json(self.cfg.commit_journal_path, {'transactions': self.transactions})
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'SKIPPED_NOT_COMPLETE')
        self.assertFalse((self.cfg.output_dir/cap.LEDGER_NAME).exists())

    def test_coverage_count_must_match_closed_boundary(self):
        s = snapshot('BTC',297,16800)
        scope = next(iter(s['retest_scopes'].values()))
        scope['expected_count'] = scope['returned_count'] = 0
        with self.assertRaises(ValueError):
            cap.validate_snapshot(s)

    def test_stale_complete_scope_cannot_hide_later_invalidation(self):
        s = snapshot('BTC',298,17400)
        old = snapshot('BTC',297,16800)
        s['retest_scopes'] = old['retest_scopes']
        with self.assertRaisesRegex(ValueError, 'RETEST_SCOPE_MISMATCH'):
            cap.validate_snapshot(s)

    def test_dry_run_never_creates_shadow_files(self):
        from dataclasses import replace
        self.seed()
        before = {str(p):cap._sha(p) for p in self.root.rglob('*') if p.is_file()}
        config = replace(self.cfg, dry_run=True)
        self.assertEqual(cap.commit_after_complete(config,295)['status'],'SKIPPED_DRY_RUN')
        cap.recover_completed(config)
        self.assertEqual(before, {str(p):cap._sha(p) for p in self.root.rglob('*') if p.is_file()})

    def test_pending_earlier_capture_cannot_be_skipped(self):
        self.seed(self.bundle(295,14400))
        self.seed(self.bundle(296,16200))
        self.assertEqual(cap.commit_after_complete(self.cfg,296)['status'],'PERSISTENCE_FAILED')
        self.assertFalse((self.cfg.output_dir/cap.LEDGER_NAME).exists())
        cap.recover_completed(self.cfg)
        self.assertEqual([r['observation_number'] for r in self.ledger()['records']], [295,296])

    def test_recovery_does_not_recommit_already_verified_history(self):
        self.commit()
        with patch.object(cap,'commit_after_complete',side_effect=AssertionError('No repeated append')):
            cap.recover_completed(self.cfg)
        self.assertEqual(self.ledger()['latest_observation'],295)

    def test_locked_target_events_survive_display_eviction(self):
        self.commit()
        previous = self.ledger()['current']['BTC']
        b = self.bundle(296,16200)
        s = b['symbols']['BTC']
        event = rv.LiquidityEvent(**s['exposure']['liquidity_events'][0]['evidence'])
        with patch.object(rv,'find_liquidity_events',return_value=[event]):
            target, events = cap._locked_target_capture({'M5':object()},previous,'BTC',16200)
        s['exposure']['liquidity_events'] = []
        s['locked_target'], s['locked_target_events'] = target, events
        cap.validate_snapshot(s)
        self.seed(b)
        self.assertEqual(cap.commit_after_complete(self.cfg,296)['status'],'PERSISTED')
        state = self.ledger()['current']['BTC']
        self.assertEqual(state['sequence_state'],'SETUP_FORMED')
        self.assertEqual(state['selected_sweep']['pool_id'],previous['active_target']['liquidity_id'])

    def test_locked_target_identity_cannot_change(self):
        self.commit()
        s = snapshot('BTC',296,16200)
        s['locked_target'] = copy.deepcopy(self.ledger()['current']['BTC']['active_target'])
        s['locked_target']['liquidity_id'] = 'DIFFERENT'
        state = cap._fold(self.ledger()['current']['BTC'],s)
        self.assertEqual(state['evaluation_status'],'UNAVAILABLE')
        self.assertIsNone(state['contextual_mss'])

    def test_unactivated_status_is_readonly_and_reports_symbols(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        status = cap.live_status(self.cfg.output_dir)
        self.assertEqual(status['status'],'NOT_ACTIVATED')
        self.assertEqual(set(status['symbols']), {'BTC','ETH'})
        self.assertEqual(status['sequence_started'],0)
        self.assertFalse(status['SHADOW_SEQUENCE_VALIDATED'])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_cross_observation_forward_lifecycle_and_no_origin(self):
        self.commit()
        first = copy.deepcopy(self.ledger()['records'][0])
        identity = self.ledger()['current']['BTC']['sequence_id']
        self.commit(296,16200)
        self.assertEqual(self.ledger()['current']['BTC']['sequence_state'],'SETUP_FORMED')
        self.commit(297,16800)
        state = self.ledger()['current']['BTC']
        self.assertEqual(state['sequence_state'],'RETEST_CONFIRMED')
        self.assertEqual(state['sequence_id'],identity)
        self.assertFalse(state['origin_creation_allowed'])
        self.assertEqual(first,self.ledger()['records'][0])
        self.assertNotEqual(identity,self.ledger()['current']['ETH']['sequence_id'])

    def test_four_forming_capture_ready_not_sequence_validated(self):
        for i,cp in enumerate((14400,16200,16800,17400)):
            self.commit(295+i,cp,forming=True)
        result = cap.live_status(self.cfg.output_dir)
        self.assertTrue(result['SHADOW_SEQUENCE_CAPTURE_READY'])
        self.assertFalse(result['SHADOW_SEQUENCE_VALIDATED'])
        self.assertEqual(result['sequence_started'],2)
        self.assertEqual(result['activation_observation'],295)

    def test_progression_required_for_validation(self):
        for i,cp in enumerate((14400,16200,16800,17400)):
            self.commit(295+i,cp)
        self.assertTrue(cap.live_status(self.cfg.output_dir)['SHADOW_SEQUENCE_VALIDATED'])

    def test_gap_not_consecutive(self):
        for obs,cp in ((295,14400),(297,16200),(298,16800),(299,17400)):
            self.commit(obs,cp,forming=True)
        self.assertFalse(cap.live_status(self.cfg.output_dir)['SHADOW_SEQUENCE_CAPTURE_READY'])

    def test_invalidation_never_rewrites_history(self):
        self.commit()
        self.commit(296,16200)
        before = copy.deepcopy(self.ledger()['records'])
        b = self.bundle(297,16800)
        b['symbols']['BTC']['exposure']['zones'][0]['evidence']['status']='INVALIDATED'
        self.seed(b)
        cap.commit_after_complete(self.cfg,297)
        ledger = self.ledger()
        self.assertEqual(ledger['current']['BTC']['sequence_state'],'INVALIDATED')
        self.assertEqual(ledger['current']['ETH']['sequence_state'],'RETEST_CONFIRMED')
        self.assertEqual(before,ledger['records'][:2])

    def test_atomic_failure_recover_from_frozen_input_only(self):
        self.seed()
        original = cap.atomic_write_json
        def fail_ledger(path, value):
            if path.name == cap.LEDGER_NAME:
                raise OSError('simulated replace failure')
            return original(path,value)
        with patch.object(cap,'atomic_write_json',side_effect=fail_ledger):
            self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'PERSISTENCE_FAILED')
        self.assertFalse((self.cfg.output_dir/cap.LEDGER_NAME).exists())
        self.assertTrue((self.cfg.output_dir/'tactical-shadow-inputs/295.json').exists())
        cap.recover_completed(self.cfg)
        self.assertEqual(self.ledger()['latest_observation'],295)
        self.assertEqual(len(self.ledger()['records']),1)

    def test_corrupt_ledger_fail_closed(self):
        self.commit()
        path = self.cfg.output_dir/cap.LEDGER_NAME
        path.write_text('{broken',encoding='utf-8')
        self.seed(self.bundle(296,16200))
        self.assertEqual(cap.commit_after_complete(self.cfg,296)['status'],'PERSISTENCE_FAILED')
        self.assertEqual(path.read_text(),'{broken')
        self.assertEqual(cap.live_status(self.cfg.output_dir)['status'],'UNAVAILABLE')

    def test_corrupt_transition_log_detected(self):
        self.commit()
        ledger = self.ledger()
        ledger['records'][0]['transitions'][0]['to_state']='MSS_CONFIRMED'
        atomic_write_json(self.cfg.output_dir/cap.LEDGER_NAME,ledger)
        self.assertFalse(cap.live_status(self.cfg.output_dir)['SHADOW_SEQUENCE_VALIDATED'])

    def test_no_backfill_or_capture_without_version_marker(self):
        b=self.bundle()
        b['observation_number']=294
        self.seed(b)
        self.assertEqual(cap.commit_after_complete(self.cfg,294)['status'],'SKIPPED_NO_FORWARD_CAPTURE')
        b=self.bundle()
        b.pop('capture_version')
        self.seed(b)
        self.assertEqual(cap.commit_after_complete(self.cfg,295)['status'],'SKIPPED_NO_FORWARD_CAPTURE')
        self.assertFalse((self.cfg.output_dir/cap.LEDGER_NAME).exists())

    def test_fresh_process_readonly_and_input_source_overwrite(self):
        self.commit()
        (self.root/'market.json').write_text('overwritten',encoding='utf-8')
        before={str(p):cap._sha(p) for p in self.root.rglob('*') if p.is_file()}
        command=[sys.executable,'-m','market_reviewer.cli','tactical-sequence-live-status','--output-dir',str(self.cfg.output_dir),'--json']
        a=subprocess.check_output(command)
        self.assertEqual(a,subprocess.check_output(command))
        self.assertEqual(json.loads(a),cap.live_status(self.cfg.output_dir))
        self.assertEqual(before,{str(p):cap._sha(p) for p in self.root.rglob('*') if p.is_file()})

    def test_real_capture_before_source_disappears_and_no_raw_dump(self):
        live.LiveHybridSMCTests.setUpClass()
        frames=copy.deepcopy(live.LiveHybridSMCTests.frames)
        review=live.LiveHybridSMCTests.review
        cp=int(review['Review_Timestamp'])+300
        exposure=build_exposure(frames,review,cp)
        frozen=cap.build_snapshot(frames,review,exposure,cp,295)
        first=cap._fold(None,frozen)
        frames.clear()
        self.assertEqual(first,cap._fold(None,json.loads(json.dumps(frozen))))
        self.assertNotIn('OHLCV',json.dumps(frozen))
        self.assertNotIn('market_path',json.dumps(frozen))

    def test_future_candle_and_target_ranking_rejected(self):
        s=snapshot('BTC',295,14400)
        s['selected_target']['price']=999
        with self.assertRaises(ValueError):
            cap.validate_snapshot(s)
        s=snapshot('BTC',297,16800)
        scope=next(iter(s['retest_scopes'].values()))
        scope['touch_candles'][0]['timestamp']=16800
        with self.assertRaises(ValueError):
            cap.validate_snapshot(s)

    def test_runner_failure_fail_open_and_website_unchanged(self):
        bundle=self.bundle()
        def production(preparation,obs):
            return dict(observation_number=obs,production_hash=cap._sha(self.cfg.state_path),
                        non_canonical_research_evidence={cap.KEY:bundle},reviews={})
        seen=[]
        def persist(config,obs):
            tx=cap._read(config.commit_journal_path)['transactions'][-1]
            self.assertEqual(tx['status'],'COMPLETE')
            seen.append(obs)
            raise OSError('shadow failure')
        with patch.object(cap,'commit_after_complete',side_effect=persist), \
             patch.object(runner,'_publish_completed_website') as website, \
             patch('market_reviewer.notification_delivery.dispatch_completed_reviews'):
            result=runner._execute_ready_cycle(self.cfg,{'canonical_checkpoint':14400},295,production,
                lambda p:{'research_persistence':'PASS'},lambda:15000,'test')
        self.assertEqual(result['production_result'],'PASS')
        self.assertEqual(result['research_result'],'PASS')
        self.assertEqual(seen,[295])
        website.assert_called_once()

    def test_research_failure_no_shadow_commit(self):
        with patch.object(runner,'_commit_completed_tactical_shadow') as hook:
            with self.assertRaises(ValueError):
                runner._execute_ready_cycle(self.cfg,{'canonical_checkpoint':14400},295,
                    lambda p,n:dict(production_hash=cap._sha(self.cfg.state_path)),
                    lambda p:{'research_persistence':'FAIL'},lambda:15000,'test')
        hook.assert_not_called()

    def test_production_capture_noncanonical_only(self):
        reviews={'BTC':{'State':'WAIT'}}
        protected=self.cfg.state_path.read_bytes()
        with patch.object(runner,'review_snapshot',return_value=reviews), \
             patch.object(runner,'_load_frames',return_value={'BTC':{}}), \
             patch.object(runner,'extract_opportunity_snapshot',return_value={}), \
             patch.object(runner,'_external_by_symbol',return_value={}), \
             patch('market_reviewer.tactical_provenance.capture_noncanonical',return_value={'tactical_provenance':{}}), \
             patch.object(cap,'capture_inputs',return_value=self.bundle()) as capture:
            payload=runner.execute_production_observation({'market_path':'unused','external_path':'unused','canonical_checkpoint':14400},295,self.cfg.state_path)
        self.assertIn(cap.KEY,payload['non_canonical_research_evidence'])
        self.assertEqual(payload['reviews'],reviews)
        self.assertNotIn(cap.KEY,reviews['BTC'])
        self.assertEqual(protected,self.cfg.state_path.read_bytes())
        capture.assert_called_once()


if __name__=='__main__':
    unittest.main()
