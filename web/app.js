const Z={NO_TRADE:'不交易',WAIT:'等待',WATCH:'關注',ARMED:'已武裝',UNAVAILABLE:'資料不可用'};let sym='BTC',data=null;const $=id=>document.getElementById(id);const d=v=>v==null||v===''?'—':typeof v==='object'?JSON.stringify(v):String(v);const esc=s=>s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
function drawValue(value){
  if(value==null||value===''||value==='UNAVAILABLE')return 'UNAVAILABLE';
  if(Array.isArray(value))return value.map(drawValue).join(', ')||'UNAVAILABLE';
  if(typeof value==='object'){
    if('availability' in value)return value.availability==='AVAILABLE'?drawValue(value.value):'UNAVAILABLE';
    return Object.entries(value).map(([key,item])=>`${key}: ${drawValue(item)}`).join(', ')||'UNAVAILABLE';
  }
  return String(value);
}
function formatDraw(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return drawValue(value);
  if(!('htf' in value)&&!('tactical' in value))return drawValue(value);
  const htf=drawValue(value.htf),tactical=drawValue(value.tactical);
  return htf==='UNAVAILABLE'&&tactical==='UNAVAILABLE'?'UNAVAILABLE':`HTF: ${htf}\nTactical: ${tactical}`;
}
function readyLabel(value){return value===true?'READY':value===false?'NOT READY':'UNAVAILABLE';}
function storySummary(value){
  if(typeof value!=='string'||!value.trim())return 'UNAVAILABLE';
  const headline=value.split(';',1)[0].trim();
  return headline.length>140?headline.slice(0,137).trimEnd()+'...':headline;
}
function render(){if(!data)return;const s=data.symbols?.[sym]||{},state=d(s.review_state);$('reviewState').textContent=state;$('reviewState').dataset.state=state;$('stateZh').textContent=Z[state]||'系統狀態';$('direction').textContent=d(s.direction);$('regime').textContent=d(s.htf_regime);$('timeframe').textContent=d(s.timeframe);$('marketStory').textContent=storySummary(s.market_story);const q=s.structure||{};$('structureGrid').innerHTML=[['HTF Draw',formatDraw(q.active_draw)],['Liquidity Reaction',q.liquidity_reaction],['Displacement',q.displacement],['MSS',q.MSS],['BOS',q.BOS],['FVG',q.FVG],['OB',q.OB],['Breaker',q.Breaker]].map(([k,v])=>`<div><span>${k}</span><strong>${esc(d(v))}</strong></div>`).join('');const r=data.runtime||{};$('runtimeBadge').textContent=runtimeLabel(r);$('productionObs').textContent=d(r.production_latest_observation);$('researchObs').textContent=d(r.research_latest_observation);const l=data.evidence?.liquidation?.[sym]||{},n=data.evidence?.notification||{};$('liquidation').textContent=d(l.status??'UNAVAILABLE');$('notification').textContent=d(n.enabled_channels?.join(', ')||n.last_delivery?.channel||'NONE');const m=data.research?.summary||{};$('historicalOrigins').textContent=d(m.historical_origins);$('liveOrigins').textContent=d(m.live_origins);$('classifiableOrigins').textContent=d(m.classifiable_live_origins);$('completeOrigins').textContent=d(m.outcome_complete_live_origins);const nr=m.next_review_ready||{};$('firstReview').textContent=readyLabel(nr.SAMPLE_READY);$('calibration').textContent=readyLabel(nr.OUTCOME_READY);$('executionState').textContent=d(data.execution?.status);$('executionMessage').textContent=d(data.execution?.message_zh);$('updatedAt').textContent='最後更新 '+(data.generated_at?new Date(data.generated_at*1000).toLocaleString('zh-TW',{hour12:false}):'—');$('rawJson').textContent=JSON.stringify(data,null,2)}
function websiteMode(location=globalThis.location,configured=globalThis.GRIM_WEBSITE_MODE){
  if(configured==='LOCAL'||configured==='REMOTE')return configured;
  if(!location)return 'LOCAL';
  if(new URLSearchParams(location?.search||'').get('mode')==='remote')return 'REMOTE';
  return !location||['localhost','127.0.0.1','[::1]'].includes(location.hostname)?'LOCAL':'REMOTE';
}
const remoteMode=websiteMode()==='REMOTE';
let remoteUnavailable=false;
function remoteReadModel(packet){
  const s=packet?.snapshot;
  if(packet?.status!=='OK'||s?.schema!=='grim-website-public-snapshot.v1')throw Error('UNAVAILABLE');
  return {
    generated_at:packet.generated_at,
    stale:packet.stale===true,
    source:{review_observation:packet.source_observation},
    runtime:s.runtime,
    symbols:s.symbols,
    evidence:{liquidation:s.evidence?.liquidation,
      notification:{enabled_channels:s.evidence?.notification?.channels||[]}},
    research:{summary:{...s.research,next_review_ready:{
      SAMPLE_READY:s.research?.FIRST_REVIEW,OUTCOME_READY:s.research?.CALIBRATION}}},
    execution:{status:'RESERVED',connected:false,
      message_zh:'Execution API 尚未接入；架構保留，未永久停用。'}
  };
}
function runtimeLabel(r){
  if(remoteMode&&remoteUnavailable)return 'REMOTE DATA UNAVAILABLE';
  if(data?.stale===true||r.stale===true)return 'DATA STALE / 資料逾時';
  return r.running===true?'Runtime RUNNING':r.running===false?'Runtime IDLE / STOPPED':'Runtime UNAVAILABLE';
}
async function load(){
  $('refreshBtn').disabled=true;
  try{
    const r=await fetch(remoteMode?'/api/snapshot':'/api/status',{cache:'no-store'});
    if(!r.ok)throw Error('UNAVAILABLE');
    const packet=await r.json();
    data=remoteMode?remoteReadModel(packet):packet;
    remoteUnavailable=false;
    render();
  }catch{
    remoteUnavailable=remoteMode;
    $('runtimeBadge').textContent=remoteMode?'REMOTE DATA UNAVAILABLE':'Runtime UNAVAILABLE';
    $('rawJson').textContent=JSON.stringify({status:'UNAVAILABLE'});
  }finally{$('refreshBtn').disabled=false}
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{sym=b.dataset.symbol;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));render()});$('refreshBtn').onclick=load;load();setInterval(load,30000);
