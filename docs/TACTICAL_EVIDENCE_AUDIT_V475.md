# V4.7.5 Tactical Evidence Binding Audit

Read-only, shadow-only. Run `python -m market_reviewer.cli tactical-evidence-audit`
or add `--json` for every observation/symbol trace. No raw market fetch, outcome
join, origin creation, gate call, or persistence. Archive changes during reading
fail closed. Baseline #46-276 is separated from later observations.

## Authority and limitations

`AVAILABLE_AND_BOUND` is requirement-local, not proof of a complete setup.
For example aligned M15 structure satisfies the existing M15 alignment check,
but does not establish causal linkage. Existing shadow pass/fail is reported
separately from explicit event identity completeness. The audit does not change
V4.7.4 confirmation semantics. Missing raw input means UNKNOWN, not absent.
All A-D readiness counts mean proven assignments; zero is not negative evidence.

Producer source audit:

- `reviewer.find_displacements`: both directions, per-frame closed candles;
  no Swing Bias argument. Follow-through uses up to two subsequent closed bars:
  exposing event open time alone would not establish availability.
- `reviewer.review_symbol`: collects per-TF raw events, flattens them, then
  filters after the selected active sweep. `_latest_displacement` selects
  matching Swing Bias, VALID/STRONG, latest timestamp. `Displacement` exposes
  only selected text, without timeframe. Raw occurrence per historical review
  cannot be reconstructed from selected NONE/opposite-direction output.
- `analyze_structure`: per-TF BOS/MSS; `Last_BOS.M15` and `Last_MSS.M15` expose
  direction and open time. `_current_phase` is global, not an M15 phase producer.
  `_contextual_mss` selects primary-bias M15/M5 MSS and drops TF from its text.
- `find_liquidity_events`: side/reference price, TF, timestamp, sweep/reclaim
  exist. Cross-TF inventory is truncated to 24 events. Contextual sweep identity
  TF:timestamp lacks level identity; displacement direction:timestamp lacks TF.
  These compound references are not universally collision-proof identities.
- `_setup_id` and `_eligible_setup_retest`: locked production FVG identity and
  retest contract exist. They are not independent tactical setup IDs. FVG/OB
  inventories (24/12) and production SMC BPR/Breaker wrappers cannot prove a
  tactical chain. Touched/mitigated does not imply eligible retest or defense.

No new market detector is demonstrated necessary. A future research exposure
adapter could retain existing raw per-TF data and confirmation availability;
an independent tactical chain identity/binding contract would still need
validation. Exposure alone cannot be promised to yield confirmed samples.

## Frozen baseline #46-276

462 symbol-observation rows, 410 tactical incomplete, zero shadow confirmed.

294 directional displacement failures reconcile exactly:

| Existing exported selection | Count |
| --- | ---: |
| NONE; raw occurrence UNKNOWN | 215 |
| Opposite tactical direction | 74 |
| Before latest H1 structure; existing shadow ordering rejection | 5 |

The last group is a predicate/order attribution, not a finding that the market
lacked displacement. No ordering rule was relaxed.

136 M15 failures: all latest visible selected M15 structure was opposite to
tactical direction. This is actual exposed disagreement, not missing M15 data;
it does not prove a complete raw tactical setup was absent.

All 410 incomplete rows expose liquidity inventory. None gains ancestry merely
through inventory presence. Raw bullish/bearish occurrence remains UNKNOWN.

Counter-trend SHORT: BTC16 / ETH62. A binding-only=0, B proven unexposed
occurrence=0, C proven producer absence=0, D proven market incompleteness=0,
E UNKNOWN=78. Capability-level computed-but-unexposed is not per-case proof.

Assessment: **MIXED_EVIDENCE_GAP**. Selected-only exposure, missing displacement
TF, no per-M15 phase, and no independent tactical setup identity are distinct
limitations. Genuine setup incompleteness is not established by these counts.

## Representative traces

All six are NOT_CONFIRMABLE_WITH_EXISTING_EVIDENCE. Full requirements include
values, source, TF, timestamps, exposed paths and binding reason in CLI JSON.
Active setup and contextual trigger are NONE, eligible retest NO for all six.

| Observation | Symbol | Primary/tactical | Selected displacement | Latest M15 |
| --- | --- | --- | --- | --- |
| 247 | BTC | LONG/SHORT | BULLISH STRONG @1790554500 | BEARISH BOS @1790566200 |
| 247 | ETH | LONG/SHORT | BULLISH VALID @1790566500 | BEARISH BOS @1790565300 |
| 276 | BTC | NONE/SHORT | NONE | BULLISH BOS @1790666100 |
| 276 | ETH | LONG/LONG | BULLISH VALID @1790675100 | BEARISH BOS @1790672400 |
| 59 | BTC | NONE/SHORT | NONE | BEARISH BOS @1788132600 |
| 54 | ETH | LONG/LONG | BULLISH VALID @1788093300 | BULLISH BOS @1788087600 |

Automatic examples minimize missing existing shadow requirements, tie-break by
observation then symbol. This is audit completeness, not market quality or an
outcome score. No future outcomes inform selection.

## Validation

13 new tests; 194 combined targeted/shadow/direction/origin/maturity/diagnostic
tests passed. Compileall passed. No rules, thresholds, gate, production engine,
runner, or runtime files edited. Keep shadow-only until explicit provenance
and identity evidence can support a further acceptance audit.
