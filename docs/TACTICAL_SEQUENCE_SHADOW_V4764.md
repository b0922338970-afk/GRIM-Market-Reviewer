# V4.7.6.4 Independent Shadow Tactical Sequence

Baseline: d91ff0ac81ad9dacd16fa302b06eef01f4d4479d.

## Scope and invocation

`python -m market_reviewer.cli tactical-sequence-shadow --json`

Optional `--journal` selects an archive. This command only reads files and folds
forward observations into **in-memory** state. It does not write even a shadow
artifact, call a production review, create an origin, or control the Runner.
It is not installed into the Runner. No automatic accumulation is claimed.

Schema: `tactical-sequence-shadow.v1`. Every state includes symbol, checkpoint,
observation, primary/tactical direction, relationship, sequence identity/state,
target provenance, selected sweep/displacement, contextual MSS, namespaced setup,
eligible retest, transition history and blockers. Fixed safety fields:
`activation_status=SHADOW_ONLY`, `canonical=false`,
`origin_creation_allowed=false` (including RETEST_CONFIRMED).
The observed tactical direction is recorded separately from a locked sequence's
direction; a reversal terminates the old sequence without relabeling its side.

## Direction and target authority

The existing H1/M15 `classify_tactical()` determines explicit LONG/SHORT; neither
Swing Bias nor production Contextual_MSS/Active_Setup_ID supplies a fallback.
Primary direction is informational. Counter-trend sequences are permitted.

Target selection reuses `_liquidity_draws()` unchanged with tactical direction.
Its H1-first directional ranking is not replaced by a nearest-liquidity guess.
The exact-checkpoint legal raw source must reproduce the frozen exposure
(including the existing private OB correction parity check when necessary).
The current price comes from that verified prefix. The full liquidity detector
output must reproduce the exposed `Liquidity[-24:]` inventory, and the winner
must itself exist in that exposed inventory. No truncated-subset ranking,
production active-target text parsing, or inferred price is allowed.

Unavailable original source or a winner not exposed gives
`TACTICAL_ACTIVE_TARGET_UNAVAILABLE`. The target stores liquidity ID, price,
type, TF, side, formed timestamp, availability, selection checkpoint and source
module. It remains locked independently of production. Invalidated shadow
targets are retired; no fallback to a second-ranked target is invented.

## Lifecycle semantics

`FORMING -> MSS_CONFIRMED -> SETUP_FORMED -> RETEST_PENDING -> RETEST_CONFIRMED`

NONE and UNAVAILABLE are non-started states. INVALIDATED ends an existing
sequence. Missing input suspends evaluation without erasing active identities.
An explicit tactical direction reversal/withdrawal or confirmed locked-zone
invalidation terminates this **shadow** sequence, not Production.

- Selection checkpoint establishes the forward evidence barrier. Initial target
  selection cannot consume a historical sweep already present in that snapshot.
- `_events_for_active_target()` is reused with the shadow-selected timestamp;
  the exact pool identity must match, in addition to price/type/TF.
- Latest eligible sweep and `_latest_displacement()` are reused. Ambiguous event
  identities fail closed. `_contextual_mss()` keeps its M15-then-M5 priority.
- Selected MSS/sweep/displacement anchors are frozen before looking for setup.
  `_setup_fvg()` is unchanged. Its result must additionally resolve to the exact
  selected displacement reference before receiving a shadow identity. This is
  provenance binding, not a new detector or threshold. No alternative FVG is
  silently selected after a binding failure.
- IDs use a `TACTICAL-` namespace, symbol/direction, detector setup identity,
  displacement/MSS anchors and independent sequence identity. Production IDs
  cannot collide. A later candidate cannot replace the locked zone.
- Every stage is recorded. MSS and setup may first become observable at one
  checkpoint; both transitions are recorded there, not backdated to event time.
  SETUP_FORMED must reach a later checkpoint before retest evaluation.
- `_refresh_setup_status()` and `_eligible_setup_retest()` are reused over closed
  candles. Complete history since formation is required to rule out missed
  invalidation. Only candles opening at/after setup selection can be eligible
  forward retests. Pre-selection touches cannot count. Invalidated zones never
  confirm retest. Missing history is UNAVAILABLE, not a negative market finding.
- Equal-checkpoint identical input is idempotent. Conflicting input, backward
  checkpoints/observation numbers or cross-symbol state reuse are rejected.

## Frozen evidence safety

Only structure, displacement, liquidity and FVG collections feed these helpers.
OB/BPR/Breaker are not alternative active setups. Legacy OB collisions are not
repaired in any persisted file and do not authorize binding. Every consumed
collection must validate IDs, symbol and timeframe close boundaries. Nested
event timestamps/directions/TFs/types must agree with their envelopes, and the
detector dataclass must be constructible. A locked setup invalidation must match
the original zone event identity. Missing retest scope/history is explicitly
evaluation UNAVAILABLE while the lifecycle and locked identity are retained.

Only COMPLETE non-historical journal rows with forward provenance from #282
onward participate. Old #46-#281 rows cannot contribute acceptance evidence.
All source bytes are rechecked at the end. Changed input fails the report;
there is no persistence to roll back.

## Mandatory #288 ETH trace

Primary LONG, tactical LONG. Four conditional producer probes share:

- bullish VALID displacement @1790719200, `structure_broken=MSS 2681.69`,
  `fvg_created=false`;
- bullish M5 MSS @1790719200, price 2681.69;
- no eligible SETUP_FVG.

Their pool IDs are H1-EQL-1790348400, M15-EQL-1790657100,
M5-EQL-1790715600 and M5-EQL-1790718600. The JSON includes each sweep/event
timestamp and exact displacement/MSS candidate identities.
All four swept references are Equal Lows. The unchanged `_liquidity_draws`
selector requires a Sell-side typed tactical reference for LONG; Equal Lows
probe inputs do not meet that filter and cannot be promoted to a target.

For each trial, the exposed 80 FVG records split into 37 opposite-direction,
30 same-direction GENERIC_FVG, and 13 same-direction SETUP_FVG formed before
the required minimum 1790719200. Latest earlier matching SETUP_FVG formed at
1790707200. Consequently the unchanged `_setup_fvg()` returns NONE.

The original #288 raw market file is no longer present at its journal path;
the exact current-price/full-ranking scope cannot be proven. Therefore no
deterministic active target is authorized for #288. Probe candidates are not
shadow-selected contextual MSS. SHADOW SETUP_FORMED is not established.
This separates target/source insufficiency from genuine exposed FVG eligibility
failure. Retest and later lifecycle remain unestablished, not fabricated.

## Observed replay at implementation

Forward #282-#294: 13 observations / 26 symbol-checkpoint rows.
Source availability at validation time permits first selection at #294 only:

| Symbol | Primary / tactical | Target | State |
|---|---|---|---|
| BTC | NONE / SHORT | H1-BSL-1790722800, 83849.00 | FORMING |
| ETH | LONG / LONG | H1-SSL-1790733600, 2666.19 | FORMING |

Both selected_at=1790745900. They await a post-selection sweep; earlier sweep
records cannot be backfilled. Sequence starts=2 (LONG=1, SHORT=1), contextual
MSS=0, setup formed=0, retest pending/confirmed=0, invalidated=0. Actual
counter-trend SHORT sequence starts=0; BTC primary NONE is not counter-trend.
Counter-trend capability and multi-observation completion are covered by tests,
not claimed as observed live successes.

This replay depends on explicitly supplied frozen exposure **and raw source
availability**. The volatile shared market path is not an immutable archive.
An overwritten source can make a prior replay start unavailable. No state is
silently persisted or auto-resumed from such a reconstruction. A future durable
forward integration must retain the exact target-selection input/state before
claiming continuous live accumulation.

Recommendation: **KEEP_SHADOW_SEQUENCE_EXPERIMENTAL**. The independent adapter
and state machine exist; no new MSS/FVG detector is required. Historical source
availability and real forward lifecycle completion remain limitations. No
tactical research origins or execution permission are enabled.

## Validation

- New shadow lifecycle tests: 30 passed, including a multi-checkpoint path to
  RETEST_CONFIRMED followed by INVALIDATED, direction/identity guards, strict
  post-selection scope, unavailable-input handling and no origin permission.
- Requested combined regression: 450 tests run, 449 passed, one skipped. This
  includes Reviewer, tactical provenance/direction/audit, research maturity,
  missed-opportunity/origin and Runner modules. The skip is the existing optional
  #282 raw source replay whose volatile market file was overwritten.
- Compileall and diff-check passed. No production detector, origin eligibility,
  Runner integration, canonical persistence or historical data was modified.
