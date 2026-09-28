'use strict';

// Public wire contract only. No market computation or local filesystem access.
const SCHEMA = 'grim-website-public-snapshot.v1';
const integer = v => Number.isSafeInteger(v) && v >= 0;
const nullableInt = v => v === null || integer(v);
const nullableBool = v => v === null || typeof v === 'boolean';
const enumeration = values => v => typeof v === 'string' && [...values, 'UNAVAILABLE'].includes(v);
const states = ['NO_TRADE','WAIT','WATCH','ARMED'];
const directions = ['LONG','SHORT','NONE','BULLISH','BEARISH','NEUTRAL'];
const zones = ['NONE','FRESH','TESTED','DEFENDED','FAILED','MULTIPLE_RELEVANT_ZONES',
  'CHAIN_LINKED_BPR','AMBIGUOUS_BPR','ISOLATED_BPR','CONFIRMED_CHAIN_BREAKER',
  'BREAKER_CANDIDATE','GENERIC_ROLE_REVERSAL','NOT_CONFIRMED','OTHER_CHAIN_LINKED_BPR','OTHER_CONFIRMED_CHAIN_BREAKER'];
for (const prefix of ['CHAIN_LINKED_','OTHER_CHAIN_','ISOLATED_'])
  for (const state of ['FRESH','TESTED','DEFENDED','FAILED']) zones.push(prefix + state);
const reactionParts = ['RECLAIMED','REJECTED','ACCEPTED_OUTSIDE','UNRESOLVED',
  'SWEEP_RECLAIMED','SWEEP_REJECTED','SWEEP_ACCEPTED_OUTSIDE','SWEEP_UNRESOLVED','NONE','UNAVAILABLE'];
function shape(value, spec) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).length === Object.keys(spec).length &&
    Object.entries(spec).every(([key, test]) => Object.hasOwn(value, key) && test(value[key]));
}
function draw(value) {
  if (value === 'NONE' || value === 'UNAVAILABLE') return true;
  if (typeof value === 'object') return shape(value, {htf: drawText, tactical: drawText});
  return drawText(value);
}
function drawText(value) {
  return typeof value === 'string' && (['NONE','UNAVAILABLE'].includes(value) ||
    /^(?:(?:Macro|Tactical) Draw: )?(?:(?:External|Internal) (?:Buy-side|Sell-side)(?: Liquidity)?|Equal (?:Highs|Lows)) [0-9]+\.[0-9]+ on (?:D1|H4|H1|M15|M5), (?:formed_at=[0-9]+, )?distance=[0-9]+\.[0-9]+$/.test(value));
}
function story(value, symbol) {
  if (value === 'UNAVAILABLE') return true;
  if (typeof value !== 'string') return false;
  const fields = value.split(' | ');
  const allowed = [[symbol], directions,
    ['CONTINUATION','PULLBACK','REVERSAL_CANDIDATE','RANGE','EXHAUSTION','TRANSITION'],
    ['NONE','SEEKING_LIQUIDITY','SWEPT','RECLAIMED','DISPLACEMENT_CONFIRMED','MSS_CONFIRMED',
      'SETUP_FVG_CREATED','RETEST_PENDING','INVALIDATED','EXPIRED_NO_TRIGGER','COMPLETED'], states];
  return fields.length === 5 && fields.every((v,i) => enumeration(allowed[i])(v));
}
function symbolRow(value, symbol) {
  return shape(value, {
    review_state: enumeration(states), direction: enumeration(directions),
    timeframe: enumeration(['D1','H4','H1','M15','M5']),
    htf_regime: enumeration(['TREND_CONTINUATION','TREND_PULLBACK','REVERSAL_CANDIDATE','RANGE',
      'TRANSITION','STRONG_UP','STRONG_DOWN','HIGH_VOL','LOW_VOL']),
    market_story: v => story(v, symbol),
    structure: v => shape(v, {
      active_draw: draw,
      liquidity_reaction: x => typeof x === 'string' && x.split('+').every(p => reactionParts.includes(p)),
      displacement: enumeration(['NONE','WEAK','VALID','STRONG','INVALID']),
      MSS: enumeration(['NONE','GENERIC','GENERIC_INVENTORY','CONTEXTUAL']),
      BOS: enumeration(['NONE','GENERIC','GENERIC_INVENTORY','CONTEXTUAL']),
      FVG: enumeration(zones), OB: enumeration(zones), Breaker: enumeration(zones),
    }),
  });
}
function validSnapshot(snapshot) {
  return shape(snapshot, {
    schema: v => v === SCHEMA, generated_at: integer, source_updated_at: nullableInt,
    review_observation: nullableInt, source_observation: nullableInt,
    stale_after_seconds: v => integer(v) && v > 0, stale: v => typeof v === 'boolean',
    runtime: v => shape(v, {running: nullableBool, production_latest_observation: nullableInt,
      research_latest_observation: nullableInt, stale: x => typeof x === 'boolean'}),
    symbols: v => shape(v, {BTC: x => symbolRow(x,'BTC'), ETH: x => symbolRow(x,'ETH')}),
    evidence: v => shape(v, {
      liquidation: x => shape(x, Object.fromEntries(['BTC','ETH'].map(s =>
        [s, row => shape(row, {status: enumeration(['CONNECTED','DISCONNECTED'])})]))),
      notification: x => shape(x, {channels: c => Array.isArray(c) && c.every(enumeration(
        ['TELEGRAM','DISCORD','LINE','GENERIC_WEBHOOK','WEBHOOK']))}),
    }),
    research: v => shape(v, {historical_origins: nullableInt, live_origins: nullableInt,
      classifiable_live_origins: nullableInt, outcome_complete_live_origins: nullableInt,
      FIRST_REVIEW: nullableBool, CALIBRATION: nullableBool}),
    execution: v => shape(v, {status: x => x === 'RESERVED', connected: x => x === false}),
  }) && snapshot.review_observation === snapshot.source_observation;
}
module.exports = {validSnapshot};
