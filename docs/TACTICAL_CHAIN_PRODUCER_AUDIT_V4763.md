# V4.7.6.3 Tactical Chain Producer Audit

Baseline: b0f11f88b8b5c907ecaf672fe8dd113f055ff1de.

## Read-only command

`python -m market_reviewer.cli tactical-chain-audit --json`

Optional `--journal` selects a local journal for inspection. No persistence,
fetch, lifecycle replay, origin creation, outcome access, or Runner control.
Only COMPLETE, non-historical transactions carrying forward tactical exposure
are included. Missing old exposures are excluded, never backfilled. Source
identity and checkpoint must agree; conflicting duplicate rows fail closed.
Source bytes are checked again at the end to detect concurrent changes.

## Exact producer contracts

Contextual MSS:

`review_symbol -> _events_for_active_target -> latest_tactical_sweep ->
_latest_displacement -> _contextual_mss -> _contextual_mss_text`

The active-target filter requires the selected reference and timestamp after
selected_at. Latest SWEPT is selected. Displacement is VALID/STRONG, matches
Swing Bias, and is later than the sweep. `_contextual_mss` accepts a bias
argument, searches M15 then M5, requires MSS direction==bias, MSS>sweep and
MSS>=displacement, then returns the last candidate of the first matching TF.
The output is one selected text, not all raw structure events. Reclaim is not
an additional predicate inside this helper; downstream contracts remain intact.

`CONTEXTUAL_MSS_PRIMARY_BIAS_FILTERED = true`.

Active setup:

`review_symbol -> _setup_fvg -> _resolve_sequence_lifecycle -> previous setup
lock / output_setup_fvg -> _setup_id`

Only SETUP_FVG can produce this ID, not OB/BPR/Breaker. Direction matches bias;
formation is at or after sweep/displacement/MSS. The lifecycle must be
SETUP_FVG_CREATED or RETEST_PENDING. An existing active setup is retained while
the active-state contract continues. A raw opposite FVG does not override it.

`ACTIVE_SETUP_PRIMARY_SEQUENCE_ONLY = true`.

## #282 component trace

Checkpoint 1790699100. Both selected Contextual_MSS and Active_Setup_ID are NONE.

| Component | BTC | ETH |
|---|---:|---:|
| Primary / tactical | NONE / SHORT | LONG / LONG |
| H1 structure records | 29 | 28 |
| M15 structure records | 50 | 42 |
| Tactical-direction raw displacement records, all strengths | 161 | 160 |
| SWEPT records, all sides | 11 | 12 |
| RECLAIMED records, all sides | 8 | 12 |
| Tactical-direction SETUP_FVG records | 11 | 17 |
| Tactical-direction OB records | 23 | 17 |

Latest BTC H1 anchor: BEARISH BOS @1790694000,
`BOS-045125fea5269675f72205dd`.
Latest ETH H1 anchor: BULLISH BOS @1790665200,
`BOS-f148801e38b0d37fdc2f3ebc`.
The JSON trace includes event IDs, timestamps, availability, existing setup IDs,
pool IDs and detector displacement references. These are inventories, not links.

The original #282 market path now contains a later observation. This audit
does not claim to repeat its earlier successful corrected raw replay. Frozen
non-zone collections still validate. Legacy OB collisions remain invalid for
full binding; zone counts above are records, not verified unique identities.
Each zone must independently pass temporal validation to appear in the trace.
No zone IDs or historical files are repaired.

BTC missing contextual/setup classifications: PRIMARY_BIAS_FILTERED. This
proves exclusion of the tactical direction, not that a complete hidden setup
exists. ETH classifications: UNKNOWN. Absence of selected output is not enough
to prove EVENT_NOT_FORMED or RAW_RESULT_NOT_EXPOSED.

## Existing helper probes

When exact raw reproduction is available, unchanged detectors and helpers run
on the legal prefix. Otherwise validated frozen structure/displacement/FVG
records are rehydrated into their original event dataclasses and fed to the
same helpers. This is explicitly not raw candle redetection. Nested timestamp,
direction and timeframe must agree with the validated envelope.

Only exact-pool SWEPT records on tactical TFs are probe inputs, with the proper
liquidity side. No sweep is chosen as an authorized active target. Every trial
records `production_eligible_target_proven=false` and
`active_setup_authorized=false`. OB records are not input to these helpers;
their identity collisions cannot be repaired or bypassed for binding.

Observed forward cohort #282-#289: 16 symbol rows. BTC classifications are
PRIMARY_BIAS_FILTERED (8); ETH UNKNOWN (8). Both #282 helper probes return zero
MSS and zero setup for their supplied inputs. #288 ETH returns MSS in four
conditional trials but zero setup; this is not four independent chains.
#289 permits exact corrected raw reproduction for both symbols and yields
zero MSS/setup in the conditional trials. No probe proves global event absence.

The helpers accept direction and can be reused. They cannot, by themselves,
establish an independent tactical active target and lifecycle. Binding still
requires production-selected Contextual_MSS and Active_Setup_ID. There is no
authority here to invent that missing selection contract.

Assessment: **MIXED_CHAIN_GAP**: primary selection/exposure dependence plus
missing independent tactical selection/lifecycle capability. Genuine market
chain absence remains unproven. No changes to eligibility or producer rules.

## Verification

Tests cover input immutability, separated selected/raw fields, no automatic
linkage, future envelope and nested timestamps, invalid zone isolation, symbol
separation, activation cohort, duplicate identities, CLI and fresh-process
determinism, and conditional helper outputs without active-setup authority.
The optional V4.7.6.2 #282 raw-replay test explicitly skips if its volatile
market source was overwritten. Synthetic OB ancestry regressions still run.
