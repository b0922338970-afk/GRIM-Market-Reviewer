const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = {};
const context = {
  document: {getElementById: id => nodes[id] ??= {dataset: {}}, querySelectorAll: () => []},
  fetch: () => new Promise(() => {}),
  setInterval: () => {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);
const run = source => vm.runInContext(source, context);
assert.equal(run("formatDraw({htf:'UNAVAILABLE',tactical:'UNAVAILABLE'})"), 'UNAVAILABLE');
assert.equal(run("formatDraw({htf:'External liquidity 123',tactical:'UNAVAILABLE'})"), 'HTF: External liquidity 123\nTactical: UNAVAILABLE');
assert.equal(run("formatDraw({htf:null,tactical:456})"), 'HTF: UNAVAILABLE\nTactical: 456');
assert.equal(run("formatDraw({htf:{price:123,timeframe:'H4'},tactical:null})"), 'HTF: price: 123, timeframe: H4\nTactical: UNAVAILABLE');
assert.equal(run('readyLabel(false)'), 'NOT READY');
assert.equal(run('readyLabel(true)'), 'READY');
assert.equal(run('readyLabel(null)'), 'UNAVAILABLE');
assert.equal(run('readyLabel(undefined)'), 'UNAVAILABLE');
assert.equal(run("readyLabel('N/A')"), 'UNAVAILABLE');
const story = 'BTC | NONE | RANGE | INVALIDATED | NO_TRADE; contextual evidence details remain available';
context.fixture = {symbols: {BTC: {review_state:'NO_TRADE',market_story:story,
  structure:{active_draw:{htf:'UNAVAILABLE',tactical:'UNAVAILABLE'}}}},
  research:{summary:{next_review_ready:{SAMPLE_READY:false,OUTCOME_READY:true}}},
  execution:{status:'RESERVED',message_zh:'Execution API 尚未接入；架構保留，未永久停用。'}};
const before = JSON.stringify(context.fixture);
run('data=fixture;render()');
assert.equal(nodes.marketStory.textContent, story.split(';')[0]);
assert.equal(nodes.firstReview.textContent, 'NOT READY');
assert.equal(nodes.calibration.textContent, 'READY');
assert.equal(nodes.stateZh.textContent, '不交易');
assert.equal(nodes.executionState.textContent, 'RESERVED');
assert.equal(JSON.stringify(context.fixture), before);
assert.equal(JSON.parse(nodes.rawJson.textContent).symbols.BTC.market_story, story);
assert.equal(run("storySummary('x'.repeat(200)).length"), 140);
assert.ok(run("storySummary('x'.repeat(200)).endsWith('...')"));
context.fixture.symbols.BTC.structure.active_draw.htf = '<script>bad</script>';
run('render()');
assert.ok(!nodes.structureGrid.innerHTML.includes('<script>'));
assert.ok(nodes.structureGrid.innerHTML.includes('&lt;script&gt;'));
console.log('PASS: draw formatting, readiness, summary, immutability, escaping and execution presentation');
