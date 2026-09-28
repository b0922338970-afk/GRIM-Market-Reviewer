const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createHandler} = require('../api/snapshot');
const {validSnapshot} = require('../server/public-snapshot');
const root = path.resolve(__dirname,'..');
const env = {GRIM_PUBLIC_SUPABASE_URL:'https://example.supabase.co',
  GRIM_PUBLIC_SUPABASE_PUBLISHABLE_KEY:'sb_publishable_SYNTHETIC_TEST_ONLY'};
function snapshot(){
  return {schema:'grim-website-public-snapshot.v1', generated_at:2000, source_updated_at:2000,
    review_observation:257,source_observation:257,stale_after_seconds:1000,stale:false,
    runtime:{running:true,production_latest_observation:258,research_latest_observation:258,stale:false},
    symbols:Object.fromEntries(['BTC','ETH'].map(symbol=>[symbol,{
      review_state:symbol==='BTC'?'WATCH':'ARMED',direction:symbol==='BTC'?'LONG':'SHORT',timeframe:'M5',htf_regime:'RANGE',
      market_story:symbol+' | NONE | RANGE | RETEST_PENDING | WATCH',
      structure:{active_draw:{htf:'UNAVAILABLE',tactical:'UNAVAILABLE'},liquidity_reaction:'SWEEP_RECLAIMED',
        displacement:'VALID',MSS:'CONTEXTUAL',BOS:'GENERIC',FVG:'CHAIN_LINKED_FRESH',OB:'ISOLATED_TESTED',Breaker:'NOT_CONFIRMED'}}])),
    evidence:{liquidation:{BTC:{status:'CONNECTED'},ETH:{status:'DISCONNECTED'}},notification:{channels:['TELEGRAM']}},
    research:{historical_origins:45,live_origins:2,classifiable_live_origins:0,outcome_complete_live_origins:2,FIRST_REVIEW:false,CALIBRATION:true},
    execution:{status:'RESERVED',connected:false}};
}
function row(s=snapshot()){
  return {snapshot:s,generated_at:s.generated_at,source_observation:s.source_observation,
    stale_after_seconds:s.stale_after_seconds,updated_at:'2026-09-28T12:00:00+00:00'};
}
async function invoke({rows=[row()],status=200,now=2500,config=env,method='GET',failure}={}){
  const calls=[];
  const fetchImpl=async (...args)=>{calls.push(args);if(failure)throw failure;return new Response(JSON.stringify(rows),{status});};
  const headers={};let result;
  const res={setHeader:(k,v)=>{headers[k]=v;},end:text=>{result=JSON.parse(text);}};
  await createHandler({fetchImpl,clock:()=>now*1000,env:config})({method},res);
  return {status:res.statusCode,result,headers,calls};
}
const jwt=role=>[Buffer.from(JSON.stringify({alg:'HS256'})).toString('base64url'),
  Buffer.from(JSON.stringify({role})).toString('base64url'),'syntheticSignature'].join('.');

test('unwrap latest row, GET select, timeout, redirect and no-store',async()=>{
  const r=await invoke();assert.equal(r.status,200);assert.equal(r.result.status,'OK');
  assert.deepEqual(r.result.snapshot,snapshot());assert.equal(r.result.source_observation,257);
  assert.equal(r.result.stale,false);assert.equal(r.headers['Cache-Control'],'no-store');
  assert.equal(r.calls.length,1);
  assert.equal(r.calls[0][0],'https://example.supabase.co/rest/v1/grim_website_snapshot?id=eq.latest&select=snapshot,generated_at,source_observation,stale_after_seconds,updated_at');
  const options=r.calls[0][1];assert.equal(options.method,'GET');assert.equal(options.redirect,'error');
  assert.equal(options.cache,'no-store');assert.ok(options.signal instanceof AbortSignal);
  assert.equal(options.headers.apikey,env.GRIM_PUBLIC_SUPABASE_PUBLISHABLE_KEY);
  assert.equal(options.headers.Authorization,undefined);
});
test('request-time stale boundary does not change reviewer state',async()=>{
  assert.equal((await invoke({now:3000})).result.stale,false);
  const r=await invoke({now:3001});assert.equal(r.result.stale,true);
  assert.equal(r.result.snapshot.symbols.ETH.review_state,'ARMED');
  assert.equal(r.result.snapshot.stale,false);
});
test('source age, future timestamps and existing stale cannot be hidden by export',async()=>{
  for(const change of [s=>s.source_updated_at=1,s=>s.source_updated_at=null,s=>s.stale=true,s=>s.generated_at=4000]){
    const s=snapshot();change(s);assert.equal((await invoke({rows:[row(s)]})).result.stale,true);
  }
});
test('missing/malformed rows fail safely',async()=>{
  for(const rows of [[],null,{},[{}],[row(),row()]]){
    const r=await invoke({rows});assert.equal(r.status,503);assert.deepEqual(r.result,{status:'UNAVAILABLE'});
    assert.equal(r.headers['Cache-Control'],'no-store');
  }
});
test('4xx/5xx and network failures never expose provider body or stack',async()=>{
  for(const status of [400,401,403,429,500,503]){
    const r=await invoke({status,rows:{secret:'SENSITIVE',path:'C:/private/data'}});
    assert.equal(r.status,503);assert.deepEqual(r.result,{status:'UNAVAILABLE'});
  }
  assert.deepEqual((await invoke({failure:Error('SENSITIVE')})).result,{status:'UNAVAILABLE'});
});
test('only publishable/anon read credentials accepted',async()=>{
  for(const key of ['sb_secret_SUPER_SECRET',jwt('service_role'),jwt('authenticated'),'service_role','', 'invalid.jwt.key']){
    const r=await invoke({config:{...env,GRIM_PUBLIC_SUPABASE_PUBLISHABLE_KEY:key}});
    assert.equal(r.status,503);assert.equal(r.calls.length,0);assert.deepEqual(r.result,{status:'UNAVAILABLE'});
  }
  const key=jwt('anon');const r=await invoke({config:{...env,GRIM_PUBLIC_SUPABASE_PUBLISHABLE_KEY:key}});
  assert.equal(r.status,200);assert.equal(r.calls[0][1].headers.Authorization,'Bearer '+key);
  assert.ok(!JSON.stringify(r.result).includes(key));
});
test('invalid endpoint and non-GET never contact Supabase',async()=>{
  for(const url of ['http://example.supabase.co','https://secret@example.supabase.co','https://example.supabase.co/private','https://attacker.example']){
    const r=await invoke({config:{...env,GRIM_PUBLIC_SUPABASE_URL:url}});assert.equal(r.calls.length,0);assert.equal(r.status,503);
  }
  const r=await invoke({method:'POST'});assert.equal(r.status,405);assert.equal(r.calls.length,0);
});
test('public wire contract rejects secrets, nested records, paths and execution changes',async()=>{
  for(const change of [s=>s.token='SECRET',s=>s.symbols.BTC.structure.MSS={journal:'SECRET'},
    s=>s.symbols.BTC.market_story='C:/private',s=>s.execution.status='ACTIVE',
    s=>s.evidence.notification.channels=['SECRET'],s=>s.symbols.BTC.structure.active_draw='C:/private']){
    const s=snapshot();change(s);assert.equal(validSnapshot(s),false);
    assert.equal((await invoke({rows:[row(s)]})).status,503);
  }
});
test('metadata mismatch rejected, unsafe updated_at never forwarded',async()=>{
  const r=row();r.source_observation=999;assert.equal((await invoke({rows:[r]})).status,503);
  const clean=row();clean.updated_at='C:/private/token';assert.equal((await invoke({rows:[clean]})).result.updated_at,null);
});
test('oversized response fails safely',async()=>{
  assert.equal((await invoke({rows:'x'.repeat(140000)})).status,503);
});
function browser(host='hosted.example'){
  const elements=new Map();const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',dataset:{},classList:{toggle(){}}});return elements.get(id);};
  const calls=[];
  const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[]},
    location:{hostname:host,search:''},URLSearchParams,Date,console,setInterval(){},
    fetch:(url)=>{calls.push(url);return new Promise(()=>{});}});
  vm.runInContext(fs.readFileSync(path.join(root,'web/app.js'),'utf8'),context);
  return {context,element,calls};
}
test('remote BTC/ETH use shared renderer and preserve raw source',()=>{
  const b=browser();assert.equal(b.calls[0],'/api/snapshot');
  const packet={status:'OK',snapshot:snapshot(),source_observation:257,generated_at:2000,stale:false};
  b.context.packet=packet;const before=JSON.stringify(packet);
  vm.runInContext('data=remoteReadModel(packet);render()',b.context);
  assert.equal(b.element('reviewState').textContent,'WATCH');assert.equal(b.element('direction').textContent,'LONG');
  assert.equal(b.element('liquidation').textContent,'CONNECTED');assert.equal(b.element('notification').textContent,'TELEGRAM');
  assert.equal(b.element('firstReview').textContent,'NOT READY');assert.equal(b.element('calibration').textContent,'READY');
  assert.equal(b.element('productionObs').textContent,'258');
  vm.runInContext("sym='ETH';render()",b.context);
  assert.equal(b.element('reviewState').textContent,'ARMED');assert.equal(b.element('direction').textContent,'SHORT');
  assert.equal(b.element('liquidation').textContent,'DISCONNECTED');assert.equal(b.element('executionState').textContent,'RESERVED');
  assert.equal(JSON.stringify(packet),before);
});
test('stale and failed refresh preserve last reviewer state including tab switches',async()=>{
  const b=browser();b.context.packet={status:'OK',snapshot:snapshot(),source_observation:257,generated_at:2000,stale:true};
  vm.runInContext('data=remoteReadModel(packet);render()',b.context);
  assert.equal(b.element('runtimeBadge').textContent,'DATA STALE / 資料逾時');
  assert.equal(b.element('reviewState').textContent,'WATCH');
  b.context.fetch=async()=>{throw Error('private stack');};await vm.runInContext('load()',b.context);
  assert.equal(b.element('runtimeBadge').textContent,'REMOTE DATA UNAVAILABLE');
  assert.ok(!b.element('rawJson').textContent.includes('private'));
  vm.runInContext("sym='ETH';render()",b.context);
  assert.equal(b.element('runtimeBadge').textContent,'REMOTE DATA UNAVAILABLE');
  assert.equal(b.element('reviewState').textContent,'ARMED');
});
test('local mode preserves /api/status without response wrapper',async()=>{
  const b=browser('127.0.0.1');assert.equal(b.calls[0],'/api/status');
  b.context.fetch=async()=>({ok:true,json:async()=>({symbols:{BTC:{review_state:'NO_TRADE'}},runtime:{running:true}})});
  await vm.runInContext('load()',b.context);assert.equal(b.element('reviewState').textContent,'NO_TRADE');
  assert.equal(vm.runInContext("websiteMode({hostname:'localhost',search:'?mode=remote'})",b.context),'REMOTE');
  assert.equal(vm.runInContext("websiteMode({hostname:'192.168.1.1'},'LOCAL')",b.context),'LOCAL');
});
test('hosting publishes only web assets and explicit function, not runtime filesystem',()=>{
  const config=JSON.parse(fs.readFileSync(path.join(root,'vercel.json'),'utf8'));
  assert.equal(config.outputDirectory,'web');assert.equal(config.framework,null);
  assert.deepEqual(config.rewrites,[{source:'/',destination:'/index.html'}]);
  assert.ok(config.functions['api/snapshot.js']);
  const ignore=fs.readFileSync(path.join(root,'.vercelignore'),'utf8');
  assert.ok(ignore.startsWith('*'));assert.ok(!ignore.includes('!artifact'));assert.ok(!ignore.includes('!research'));
  assert.ok(!fs.readFileSync(path.join(root,'web/app.js'),'utf8').includes('SERVICE_ROLE'));
});
