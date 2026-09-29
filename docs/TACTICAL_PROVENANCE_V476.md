# V4.7.6 Tactical Evidence Provenance

Research-only, forward-only. No detector, threshold, primary direction, origin,
outcome or maturity-gate changes. No tactical origins. No runtime backfill.

## Integration and authority

`execute_production_observation` builds a separate
`non_canonical_research_evidence.tactical_provenance` area after production review.
It is retained in future recovery payloads, not in review-state, Reviewer output,
opportunity snapshot, production hash, canonical checkpoint, or tracker identity.
Research execution still receives exactly its existing explicit inputs. Exposure
failure is UNAVAILABLE and cannot veto Production. Recovery does not recapture
old exposure from a later market file. No schedule/recovery logic changes.

The current running process was not restarted. Forward capture becomes active
only when a process loads this code through the normal operator lifecycle.

`tactical-evidence-provenance.v1` stores symbol/checkpoint and:

- All `find_displacements` results, both directions, with timeframe, event open,
  candle close, candle identity, original computed quality fields, producer,
  primary bias, selected flag and selection-match reason.
- All detector structure events including M15 MSS/BOS, not just latest cursors.
  No new confirmation/phase event is invented. H1 relation stays UNAVAILABLE
  unless binding can resolve explicit existing references.
- Existing liquidity detector events, side, level data and unique existing pool
  identity where available. Colliding reference identities are not guessed.
- Existing FVG/OB detector output and FVG setup identity where present.

Existing detector-level FVG16/OB8 per-TF limits and liquidity reference selection
remain. Coverage is the supplied legal prefix, not complete market history.
An absent raw candidate means absent only within that saved detector prefix.
No secrets, environment data, paths, or arbitrary review dictionaries are copied.

Availability is a conservative `CHECKPOINT_VERIFIED_UPPER_BOUND`: every input
candle is closed and visible at checkpoint. Displacement follow-through and zone
status may depend on later closed bars, so event close is NOT mislabeled as the
availability of all current attributes. Reusing this payload at an earlier
checkpoint is prohibited. No new detector is required for this exposure.

## Binding

`tactical_setup_identity.v1` is SHADOW ONLY. It requires unique H1 structure,
raw displacement, explicit contextual sweep/displacement reference, M15 MSS,
pool-linked reclaim, active setup, and existing eligible retest identity.
Direction/timeframe collisions, missing anchors and multiple reclaim matches
remain `TACTICAL_IDENTITY_INCOMPLETE`. Timing only validates existing references.
No nearest-event heuristic, automatic inventory linkage, or origin creation.

FVG detector references use TF:timestamp; contextual text uses direction:timestamp.
Only after exact event/TF resolution does a private shadow projection translate
the FVG reference for the existing V4.7.4 predicate. Original payloads remain
unchanged. Synthetic complete-chain tests exercise this translation.

The unchanged V4.7.4 classifier evaluates that projection. Raw matching history
does not waive latest-M15 alignment, displacement/H1 chronology, contextual MSS,
active setup or eligible retest. Binding does not add permission to trade.
Existing production contextual/active references constrain what can bind; raw
inventory alone cannot create an independent countertrend contextual chain.

## CLI

```
python -m market_reviewer.cli tactical-provenance-status --json
python -m market_reviewer.cli tactical-direction-shadow --json
```

Only persisted exposure is read. Missing legacy payloads remain unavailable;
current market files are never used to reconstruct past observations. Source
changes during reading fail closed. The #46-276 reaudit is separate from the
latest full window. CONTEXT counts retain V4.7.4's context-candidate meaning and
overlap INCOMPLETE; they are not additional independent samples.

## Read-only acceptance at #280

470 symbol-observation rows (#46-280), no saved raw exposure. All 470 tactical
identity states incomplete. Fresh-process CLI output deterministic; Production,
Research ledger, Runner state and commit-journal SHA256 unchanged across checks.

Baseline groups:

| Group | Matching found | Opposite only | Absent | Unavailable |
| --- | ---: | ---: | ---: | ---: |
| 215 selected NONE | 0 proven | 0 proven | 0 proven | 215 |
| 74 opposite selected | 0 proven | 0 proven | 0 proven | 74 |
| 136 opposite latest M15 | 0 proven | 0 proven | 0 proven | 136 |

These zeros are lack of proven classifications, not market-absence assertions.
Countertrend SHORT: BTC16 / ETH62, all E_UNAVAILABLE; no A-D assignments.

| Window | Symbol | LONG context/incomplete | SHORT context/incomplete | Confirmed L/S | Counter SHORT incomplete |
| --- | --- | ---: | ---: | --- | ---: |
| 24H | BTC | 2/2 | 13/13 | 0/0 | 0 |
| 24H | ETH | 18/18 | 4/4 | 0/0 | 0 |
| 72H | BTC | 20/20 | 28/28 | 0/0 | 5 |
| 72H | ETH | 33/33 | 22/22 | 0/0 | 10 |
| Full | BTC | 85/85 | 113/113 | 0/0 | 16 |
| Full | ETH | 106/106 | 112/112 | 0/0 | 62 |

All countertrend confirmed counts are zero. Recommendation KEEP_SHADOW_ONLY,
not SHADOW_VALIDATED. Exposure/binding implementation passes synthetic tests;
forward runtime evidence and complete real tactical chains are not yet validated.
