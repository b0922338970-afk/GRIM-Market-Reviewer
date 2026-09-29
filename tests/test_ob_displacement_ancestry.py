import copy
import json
import unittest
from dataclasses import replace, asdict
from pathlib import Path
from unittest.mock import patch

from market_reviewer import reviewer as rv
from market_reviewer.model import Candle
from market_reviewer.tactical_provenance import (build_exposure, identity, valid_exposure,
    deduplicate_zones, order_blocks_with_ancestry, corrected_ob_provenance_view)
import test_live_hybrid_smc as live


class OBAncestryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        live.LiveHybridSMCTests.setUpClass()
        cls.frames = live.LiveHybridSMCTests.frames
        cls.review = live.LiveHybridSMCTests.review
        cls.cp = int(cls.review['Review_Timestamp']) + 300

    def fixture(self):
        frames = copy.deepcopy(self.frames)
        cs = frames['M5'].candles
        # Exactly one bearish source for the following two bullish displacements.
        cs[-3] = Candle(cs[-3].timestamp, 102, 103, 99, 100, 10)
        cs[-2] = Candle(cs[-2].timestamp, 100, 106, 100, 105, 10)
        cs[-1] = Candle(cs[-1].timestamp, 105, 112, 105, 111, 10)
        ds = [rv.DisplacementEvent('BULLISH', 'VALID', c.timestamp, 'BOS 103', False, ratio, 2, True, False)
              for c, ratio in [(cs[-2], 2), (cs[-1], 3)]]
        return frames, ds

    def exposure(self, frames, ds):
        def detector(frame, structure):
            return ds if frame.timeframe == 'M5' else []
        with patch.object(rv, 'find_displacements', side_effect=detector):
            return build_exposure(frames, self.review, self.cp)

    def test_two_causes_same_source_distinct_ids(self):
        frames, ds = self.fixture()
        e = self.exposure(frames, ds)
        obs = [z for z in e['zones'] if z['type'] == 'OB']
        self.assertEqual(len(obs), 2)
        self.assertEqual(len({z['timestamp'] for z in obs}), 1)
        self.assertEqual(len({z['event_id'] for z in obs}), 2)
        self.assertEqual(len({z['related_displacement_event_id'] for z in obs}), 2)
        self.assertTrue(valid_exposure(e, self.review, self.cp))

    def test_ids_deterministic_not_index_or_strength(self):
        frames, ds = self.fixture()
        a = self.exposure(frames, ds)
        b = self.exposure(frames, list(reversed(ds)))
        self.assertEqual(a, b)
        c = self.exposure(frames, [replace(d, body_ratio=9) for d in ds])
        self.assertEqual([z['event_id'] for z in a['zones'] if z['type'] == 'OB'],
                         [z['event_id'] for z in c['zones'] if z['type'] == 'OB'])

    def test_parent_fields_resolve(self):
        frames, ds = self.fixture()
        e = self.exposure(frames, ds)
        parents = {d['event_id']: d for d in e['raw_directional_displacement']}
        for z in e['zones']:
            if z['type'] != 'OB':
                continue
            p = parents[z['related_displacement_event_id']]
            self.assertEqual(z['timeframe'], p['timeframe'])
            self.assertEqual(z['direction'], p['direction'])
            self.assertEqual(z['related_displacement_timestamp'], p['timestamp'])
            self.assertEqual(z['related_displacement_body_ratio'], p['evidence']['body_ratio'])

    def test_identical_lineage_dedup_and_true_collision(self):
        frames, ds = self.fixture()
        e = self.exposure(frames, ds)
        z = next(z for z in e['zones'] if z['type'] == 'OB')
        e['zones'].append(copy.deepcopy(z))
        fixed = deduplicate_zones(e)
        self.assertTrue(valid_exposure(fixed, self.review, self.cp))
        different = copy.deepcopy(z)
        different['evidence']['displacement_strength'] = 99
        fixed['zones'].append(different)
        self.assertFalse(valid_exposure(deduplicate_zones(fixed), self.review, self.cp))

    def test_bad_parent_rejected(self):
        frames, ds = self.fixture()
        e = self.exposure(frames, ds)
        next(z for z in e['zones'] if z['type'] == 'OB')['related_displacement_event_id'] = 'missing'
        self.assertFalse(valid_exposure(e, self.review, self.cp))

    def test_production_ob_unchanged(self):
        frames, ds = self.fixture()
        frame = frames['M5']
        structure = rv.analyze_structure(frame)
        before = rv.find_order_blocks(frame, structure, ds)
        self.assertEqual([b for b, d in order_blocks_with_ancestry(frame, structure, ds)], before)
        self.exposure(frames, ds)
        self.assertEqual(rv.find_order_blocks(frame, structure, ds), before)
        self.assertNotIn('related_displacement_id', asdict(before[0]))

    def test_fvg_ids_unchanged(self):
        frames, ds = self.fixture()
        e = self.exposure(frames, ds)
        for z in e['zones']:
            if z['type'] == 'FVG':
                data = z['evidence']
                anchor = {k: data[k] for k in ('direction', 'upper', 'lower', 'high', 'low') if k in data}
                self.assertEqual(z['event_id'], identity('FVG', z['symbol'], z['timeframe'], z['timestamp'], z['direction'], anchor))
                self.assertNotIn('ancestry_version', z)

    def test_282_corrected_replay_readonly(self):
        from market_reviewer.observation_runner import _load_frames
        path = Path('artifact/observation-commit-journal.json')
        if not path.exists():
            self.skipTest('Optional #282 archive unavailable')
        before = path.read_bytes()
        tx = next((t for t in json.loads(before)['transactions'] if t.get('observation_number') == 282 and t.get('status') == 'COMPLETE'), None)
        if tx is None or not Path(tx['recovery_payload']['market_path']).exists():
            self.skipTest('Optional #282 source unavailable')
        p = tx['recovery_payload']
        frames = _load_frames(Path(p['market_path']))
        original = copy.deepcopy(p)
        for symbol in ('BTC', 'ETH'):
            with self.subTest(symbol=symbol):
                old = p['non_canonical_research_evidence']['tactical_provenance'][symbol]
                r = corrected_ob_provenance_view(frames[symbol], p['reviews'][symbol], p['canonical_checkpoint'], old)
                obs = [z for z in r['exposure']['zones'] if z['type'] == 'OB']
                self.assertEqual(len(obs), 40)
                self.assertEqual(len({z['event_id'] for z in obs}), 40)
                self.assertTrue(r['validation']['valid'])
                self.assertNotIn(r['binding']['reason'], ('RAW_EVIDENCE_UNAVAILABLE', 'EXPOSURE_VALIDATION_FAILED'))
                self.assertFalse(r['binding']['origin_creation_allowed'])
                self.assertEqual([z for z in old['zones'] if z['type'] == 'FVG'],
                                 [z for z in r['exposure']['zones'] if z['type'] == 'FVG'])
                bad = copy.deepcopy(old)
                bad['raw_directional_displacement'][0]['timestamp'] += 1
                with self.assertRaisesRegex(ValueError, 'PERSISTED_SOURCE_REPLAY_MISMATCH'):
                    corrected_ob_provenance_view(frames[symbol], p['reviews'][symbol], p['canonical_checkpoint'], bad)
        self.assertEqual(p, original)
        self.assertEqual(before, path.read_bytes())
