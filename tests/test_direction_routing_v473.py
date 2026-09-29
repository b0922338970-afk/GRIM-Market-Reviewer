import copy
import unittest
from unittest.mock import patch

from market_reviewer.missed_opportunity_live import _direction_from_review, _apply_symbol_observation


class DirectionRoutingTests(unittest.TestCase):
    def test_explicit_directions(self):
        self.assertEqual(_direction_from_review({'Swing_Bias': 'BULLISH'}), 'LONG')
        self.assertEqual(_direction_from_review({'Swing_Bias': 'BEARISH'}), 'SHORT')

    def test_neutral_missing_and_unknown(self):
        for review in ({}, {'Swing_Bias': None}, {'Swing_Bias':'NONE'},
                       {'Swing_Bias':'UNAVAILABLE'}, {'Swing_Bias':'OTHER'}):
            self.assertEqual(_direction_from_review(review),'NONE')

    def test_none_skips_eligibility_identity_and_freeze(self):
        store = {'records':[{'tracker_id':'legacy-LONG','direction':'LONG','snapshots':[{'old':True}]}]}
        before = copy.deepcopy(store)
        review = {'Swing_Bias':'NONE','Preferred_Direction':'NONE'}
        with patch('market_reviewer.missed_opportunity_live.is_eligible_origin') as eligible, \
             patch('market_reviewer.missed_opportunity_live.build_tracker_candidate_from_observation') as builder, \
             patch('market_reviewer.missed_opportunity_live.upsert_tracker') as upsert:
            r = _apply_symbol_observation(store=store,symbol='BTC',review=review,frames={},
                opportunity_snapshot={},external_evidence=None,observation_number=999,preexisting_store=before)
        eligible.assert_not_called()
        builder.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual(r['candidate_status'],'NO_DIRECTION_CANDIDATE')
        self.assertEqual(r['direction'],'NONE')
        self.assertIsNone(r['eligible_origin'])
        self.assertEqual(store,before)
        self.assertEqual(review,{'Swing_Bias':'NONE','Preferred_Direction':'NONE'})

    def test_directional_observation_still_builds_candidate(self):
        for bias, direction in [('BULLISH','LONG'),('BEARISH','SHORT')]:
            with patch('market_reviewer.missed_opportunity_live.build_tracker_candidate_from_observation',
                       side_effect=RuntimeError('sentinel')) as builder:
                with self.assertRaisesRegex(RuntimeError,'sentinel'):
                    _apply_symbol_observation(store={'records':[]},symbol='BTC',review={'Swing_Bias':bias},
                        frames={},opportunity_snapshot={},external_evidence=None,observation_number=999,
                        preexisting_store={'records':[]})
                builder.assert_called_once()
