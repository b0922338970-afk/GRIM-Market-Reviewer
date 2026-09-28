"""Read-only website projection for GRIM Market Reviewer."""
from __future__ import annotations
import json, time
from pathlib import Path
from typing import Any, Callable
from .liquidation_collector import CoverageStore, LiquidationEventStore, _has_open_connected_interval, _process_is_alive
from .missed_opportunity_live import missed_opportunity_status
from .notification_delivery import notification_status
from .observation_runner import observation_runner_status
from .smc_live_sample_status import smc_live_sample_status
from .research_maturity import research_maturity
SCHEMA='grim-website-read-model.v1'; SYMBOLS=('BTC','ETH')
DEFAULT_REVIEW_STATE=Path('reviews/thesis-baseline.json'); DEFAULT_RUNNER_STATE=Path('artifact/observation-runner.json')
DEFAULT_RESEARCH_STORE=Path('research/missed-opportunities.json'); DEFAULT_LIQUIDATION_ROOT=Path('artifact/liquidations')
DEFAULT_NOTIFICATION_JOURNAL=Path('artifact/notification-delivery.json')
DEFAULT_SMC_ROOT=Path('research/historical-replay/matched-smc-contrast.v1-verified')
DEFAULT_HISTORICAL_OUTCOMES=Path('research/historical-replay/smc-hybrid-outcome.v1/outcome-join.json')

def _read_json(path: Path)->dict[str,Any]:
    try:
        v=json.loads(path.read_text(encoding='utf-8')); return v if isinstance(v,dict) else {}
    except (OSError,ValueError,TypeError): return {}

def _safe(call:Callable[[],Any])->Any:
    try: return call()
    except Exception as exc: return {'status':'UNAVAILABLE','error':exc.__class__.__name__}

def _pick(source:dict[str,Any],*keys:str,default:Any=None)->Any:
    for k in keys:
        if k in source and source[k] is not None: return source[k]
    return default

def _unwrap(value:Any)->Any:
    if isinstance(value,dict):
        if 'availability' in value:
            if value['availability']!='AVAILABLE' or value.get('reason')=='EVIDENCE_NOT_EXPOSED':
                return 'UNAVAILABLE'
            return _unwrap(value.get('value'))
        return {k:_unwrap(v) for k,v in value.items()}
    if isinstance(value,list): return [_unwrap(v) for v in value]
    return 'UNAVAILABLE' if value is None or value=='EVIDENCE_NOT_EXPOSED' else value

def _latest_reviews(journal:dict[str,Any])->tuple[Any,dict[str,Any]]:
    transactions=journal.get('transactions',[])
    if not isinstance(transactions,list): return None,{}
    def number(tx):
        try: return int(tx.get('observation_number',0))
        except (TypeError,ValueError): return 0
    for tx in sorted((t for t in transactions if isinstance(t,dict)),key=number,reverse=True):
        if tx.get('status') not in {'COMPLETE','PRODUCTION_COMMITTED','RESEARCH_PENDING'}: continue
        payload=tx.get('recovery_payload')
        reviews=payload.get('reviews') if isinstance(payload,dict) else None
        if isinstance(reviews,dict) and any(isinstance(reviews.get(s),dict) and reviews[s] for s in SYMBOLS):
            return tx.get('observation_number'),reviews
    return None,{}

def _liquidation_status_read_only(root:Path)->list[dict[str,Any]]:
    # The collector's status command repairs dead-process metadata; a website must not.
    if not root.is_dir(): return []
    store=CoverageStore(root); events=LiquidationEventStore(root); rows=[]
    for symbol in SYMBOLS:
        coverage=store.load(symbol); pid=coverage.get('collector_process_id')
        connected=(coverage.get('collector_stopped_at') is None and _has_open_connected_interval(coverage)
                   and (pid is None or _process_is_alive(int(pid))))
        rows.append({'symbol':symbol,'connected':connected,
                     'disconnect_count':len(coverage.get('disconnect_intervals',[])),
                     'stored_event_count':events.event_count(symbol),
                     'last_transport_alive_at':coverage.get('last_transport_alive_at')})
    return rows

def _liquidation_projection(rows:Any)->dict[str,Any]:
    result={s:{'symbol':s,'status':'UNAVAILABLE','connected':None,'disconnect_count':None,
               'stored_event_count':None,'last_transport_alive_at':None} for s in SYMBOLS}
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict): continue
        symbol=str(row.get('symbol','')).removesuffix('USDT')
        if symbol not in result: continue
        result[symbol].update({k:row.get(k) for k in ('connected','disconnect_count','stored_event_count','last_transport_alive_at')})
        result[symbol]['status']='CONNECTED' if row.get('connected') is True else 'DISCONNECTED' if row.get('connected') is False else 'UNAVAILABLE'
    return result

def _symbol_projection(symbol:str, review_state:dict[str,Any])->dict[str,Any]:
    symbols=review_state.get('symbols') if isinstance(review_state.get('symbols'),dict) else {}
    row=symbols.get(symbol) if isinstance(symbols.get(symbol),dict) else {}
    alert=row.get('opportunity_alert') if isinstance(row.get('opportunity_alert'),dict) else {}
    quality=alert.get('quality_context') if isinstance(alert.get('quality_context'),dict) else {}
    return {
      'symbol':symbol,
      'review_state':_pick(alert,'review_state',default='UNAVAILABLE'),
      'direction':_pick(alert,'direction',default='UNAVAILABLE'),
      'timeframe':_pick(alert,'timeframe',default='UNAVAILABLE'),
      'market_story':_pick(alert,'market_story',default='UNAVAILABLE'),
      'htf_regime':_pick(row,'Market_Regime',default='UNAVAILABLE'),
      'structure':{
        'active_draw':_pick(quality,'active_draw',default='UNAVAILABLE'),
        'liquidity_reaction':_pick(quality,'liquidity_reaction',default='UNAVAILABLE'),
        'displacement':_pick(quality,'displacement_quality',default='UNAVAILABLE'),
        'MSS':_pick(quality,'contextual_mss',default='UNAVAILABLE'),
        'BOS':_pick(quality,'contextual_bos',default='UNAVAILABLE'),
        'FVG':_pick(quality,'fvg_state',default='UNAVAILABLE'),
        'OB':_pick(quality,'ob_state',default='UNAVAILABLE'),
        'Breaker':_pick(quality,'breaker_state',default='UNAVAILABLE'),
      },
      'opportunity':{
        'entry_zone':_pick(alert,'entry_zone',default='UNAVAILABLE'), 'invalidation':_pick(alert,'invalidation',default='UNAVAILABLE'),
        'target':_pick(alert,'target',default='UNAVAILABLE'), 'remaining_room':_pick(alert,'remaining_room',default='UNAVAILABLE'),
        'fragility':_pick(alert,'fragility',default='UNAVAILABLE')},
      'generated_at':_pick(alert,'generated_at',default=_pick(row,'updated_at','timestamp'))}

def build_website_read_model(*,review_state_path:Path=DEFAULT_REVIEW_STATE,runner_state_path:Path=DEFAULT_RUNNER_STATE,
 research_store_path:Path=DEFAULT_RESEARCH_STORE,liquidation_root:Path=DEFAULT_LIQUIDATION_ROOT,
 notification_journal_path:Path=DEFAULT_NOTIFICATION_JOURNAL,smc_root:Path=DEFAULT_SMC_ROOT,
 historical_outcomes_path:Path=DEFAULT_HISTORICAL_OUTCOMES,clock:Callable[[],float]=time.time,
 commit_journal_path:Path|None=None)->dict[str,Any]:
    journal_path=Path(commit_journal_path) if commit_journal_path is not None else Path(runner_state_path).parent/'observation-commit-journal.json'
    review_observation,reviews=_latest_reviews(_read_json(journal_path))
    runner=_safe(lambda: observation_runner_status(Path(runner_state_path),state_path=Path(review_state_path),research_tracker_path=Path(research_store_path)))
    research=_safe(lambda: missed_opportunity_status(Path(research_store_path)))
    smc=_safe(lambda: smc_live_sample_status(Path(smc_root),Path(research_store_path),Path(historical_outcomes_path)))
    maturity=_safe(lambda: research_maturity(Path(smc_root),Path(research_store_path),Path(historical_outcomes_path)))
    liq=_safe(lambda: _liquidation_status_read_only(Path(liquidation_root))); note=_safe(lambda: notification_status(Path(notification_journal_path)))
    return {'schema':SCHEMA,'generated_at':int(clock()),'read_only':True,
      'ui':{'language':'zh-TW','terminology':'ENGLISH_TERMS_WITH_CHINESE_OPERATIONS','labels':{'refresh':'重新整理','details':'查看詳細','evidence':'證據完整度','research':'研究成熟度','last_updated':'最後更新'}},
      'source':{'review_observation':review_observation,'review_path':str(journal_path),'review_source':'recovery_payload.reviews'},
      'runtime':runner,'symbols':{s:_unwrap(_symbol_projection(s,{'symbols':reviews})) for s in SYMBOLS},
      'evidence':{'liquidation':_liquidation_projection(liq),'notification':note},
      'research':{'tracker':research,'smc':smc,'acceptance':maturity,'summary':{
        'historical_origins':smc.get('HISTORICAL_ORIGINS') if isinstance(smc,dict) else None,
        'live_origins':smc.get('LIVE_ORIGINS') if isinstance(smc,dict) else None,
        'classifiable_live_origins':smc.get('CLASSIFIABLE_LIVE_ORIGINS') if isinstance(smc,dict) else None,
        'outcome_complete_live_origins':smc.get('OUTCOME_COMPLETE_LIVE_ORIGINS') if isinstance(smc,dict) else None,
        'next_review_ready':{'SAMPLE_READY':maturity.get('FIRST_REVIEW_READY',False),
                             'OUTCOME_READY':maturity.get('CALIBRATION_READY',False)}}},
      'execution':{'status':'RESERVED','connected':False,'mode':'NOT_CONNECTED_YET','message_zh':'Execution API 尚未接入；架構保留，未永久停用。'}}
