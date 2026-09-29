import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from market_reviewer.cli import main
from market_reviewer.tactical_direction_shadow import classify_tactical, relationship, tactical_direction_shadow
import test_research_side_diagnostics as fixtures


def review(primary='BULLISH', tactical='BEARISH'):
    return {'Symbol':'BTC','Swing_Bias':primary,'Preferred_Direction':'LONG','State':'WATCH',
        'Last_MSS':{'H1':f'{tactical} 100 @ 3600', 'M15':f'{tactical} 100 @ 7200'},
        'FVG':[{'direction':tactical,'timeframe':'M15','formed_at':8100,'status':'FRESH'}]}


class TacticalShadowTests(unittest.TestCase):
    def test_primary_long_and_tactical_short(self):
        r=classify_tactical(review(),12000)
        self.assertEqual((r['primary_direction'],r['tactical_direction'],r['relationship']),('LONG','SHORT','COUNTER_TREND'))
        self.assertEqual(r['state'],'TACTICAL_INCOMPLETE')

    def test_capability_matrix(self):
        for p,t,rel in [('BULLISH','BULLISH','ALIGNED'),('BEARISH','BULLISH','COUNTER_TREND'),
                        ('NONE','BEARISH','NEUTRAL')]:
            r=classify_tactical(review(p,t),12000)
            self.assertEqual(r['relationship'],rel)
        self.assertEqual(classify_tactical(review('NONE'),12000)['primary_direction'],'NONE')

    def test_single_structure_cannot_supply_direction(self):
        r=review()
        r['Last_MSS'].pop('M15')
        r.pop('FVG')
        x=classify_tactical(r,12000)
        self.assertEqual(x['tactical_direction'],'NONE')
        self.assertEqual(x['state'],'TACTICAL_CONTEXT')

    def test_mss_plus_fvg_not_confirmed(self):
        r=review()
        r['Last_MSS'].pop('M15')
        x=classify_tactical(r,12000)
        self.assertEqual(x['state'],'TACTICAL_INCOMPLETE')
        self.assertIn('DIRECTIONAL_DISPLACEMENT',x['missing_evidence'])
        self.assertIn('ELIGIBLE_EXECUTION_TRIGGER',x['missing_evidence'])

    def test_confirmed_requires_complete_identity_chain_not_primary_override(self):
        r=review()
        r['Displacement']='BEARISH VALID @ 6000; structure_broken=YES'
        r['Contextual_MSS']='BEARISH @ 7200; related_sweep_id=M5:4800; related_displacement_id=BEARISH:6000'
        r['Liquidity_Events']=[{'timeframe':'M5','timestamp':t,'event_type':event,
            'level_price':100,'level_type':'Internal Buy-side Liquidity'} for t,event in [(4800,'SWEPT'),(5100,'RECLAIMED')]]
        zone=r['FVG'][0]
        zone.update(setup_type='SETUP_FVG',related_displacement_id='BEARISH:6000')
        r.update(Active_Setup_ID='M15-BEARISH-SETUP_FVG-8100',Eligible_Retest_Confirmed='YES',
                 Eligible_Retest_Setup_ID='M15-BEARISH-SETUP_FVG-8100',Eligible_Retest_Timestamp='9000',
                 Eligible_Retest_Evidence_ID='M15:9000')
        result=classify_tactical(r,12000)
        self.assertEqual(result['state'],'TACTICAL_CONFIRMED')
        self.assertEqual(result['primary_direction'],'LONG')
        self.assertFalse(result['origin_creation_allowed'])
        self.assertEqual(r['State'],'WATCH')
        r['Eligible_Retest_Setup_ID']='different'
        self.assertEqual(classify_tactical(r,12000)['state'],'TACTICAL_INCOMPLETE')

    def test_future_zone_not_used(self):
        r=review()
        r['Last_MSS'].pop('M15')
        r['FVG'][0]['formed_at']=12000
        self.assertEqual(classify_tactical(r,12000)['tactical_direction'],'NONE')

    def test_outcomes_and_mutation_isolation(self):
        r=review()
        before=copy.deepcopy(r)
        x=classify_tactical(r,12000)
        self.assertEqual(r,before)
        r['outcomes']={'24H':{'MFE':9999,'MAE':-100}}
        self.assertEqual(x,classify_tactical(r,12000))

    def test_missing_review_unavailable(self):
        self.assertEqual(classify_tactical({},12000)['state'],'UNAVAILABLE')
        self.assertEqual(relationship('LONG','UNAVAILABLE'),'UNAVAILABLE')

    def test_readonly_replay_cli_and_segments(self):
        f=fixtures.SideDiagnosticsTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        base=f.f.base
        (base/'production.json').write_text('{}')
        before={p:p.read_bytes() for p in base.rglob('*') if p.is_file()}
        with patch('market_reviewer.missed_opportunity_live.is_eligible_origin',side_effect=AssertionError('eligibility accessed')), \
             patch('market_reviewer.missed_opportunity_live.upsert_tracker',side_effect=AssertionError('origin created')):
            result=tactical_direction_shadow(f.journal,f.f.live)
            again=tactical_direction_shadow(f.journal,f.f.live)
            out=io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(['tactical-direction-shadow','--journal',str(f.journal),'--live-store',str(f.f.live),'--json']),0)
        self.assertEqual(result,again)
        self.assertEqual(result,json.loads(out.getvalue()))
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(set(result['segments']),{'latest_24H','latest_72H','full_window'})
        self.assertEqual(set(result['segments']['full_window']),{'BTC','ETH'})
        self.assertEqual(before,{p:p.read_bytes() for p in base.rglob('*') if p.is_file()})
