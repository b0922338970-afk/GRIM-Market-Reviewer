# V4.7.6.2 OB displacement ancestry

## Method and isolation

Only non-canonical tactical provenance changes. Production OrderBlock dataclass,
Reviewer output, selected OB, detector rules, checkpoint, origins, gate and
execution remain unchanged.

`order_blocks_with_ancestry` invokes the actual `find_order_blocks` with each
individual displacement. That detector applies its existing VALID/STRONG,
structure-break, prior-ten-candle and last-opposite-candle rules. The parent is
the explicit input to that invocation, not a matched strength or nearest event.
The existing last-eight selection is retained, and the resulting OB payloads
must exactly equal the unchanged multi-displacement detector output or capture
fails closed. No positional join, random ID or index is used for ancestry.

Research-only OB fields:

- ancestry_version = ob-displacement-ancestry.v1
- related_displacement_timestamp / direction / id (TF:timestamp)
- related_displacement_event_id referencing raw_directional_displacement
- related_displacement_strength / body_ratio
- ancestry_producer = market_reviewer.reviewer.find_order_blocks

The OB ID retains symbol, timeframe, source candle, direction and bounds, and
adds the exact displacement event ID. Different causes using one source candle
are distinct lineage records. Same lineage remains deterministic and identical
payload duplicates can dedupe. Different payloads with one ID still fail.
Validation checks parent existence, direction, timeframe, timestamp and quality
field consistency. FVG identity and payloads are unchanged.

## #282 CORRECTED_PROVENANCE_VIEW

The saved market source still has M5 open 1790698800, checkpoint 1790699100.
Read-only replay uses its legal prefix and persisted #282 review. It reproduces
all unchanged exposure fields exactly. Removing only new OB ancestry metadata
and translating IDs back to the legacy scheme must reproduce the complete old
zone payload multiset, including multiplicity. Any mismatch blocks replay.
This helper is not called by runtime recovery or normal status commands.

| Symbol | OB rows | Unique OB IDs | Collisions | valid_exposure |
| --- | ---: | ---: | ---: | --- |
| BTC | 40 | 40 | 0 | true |
| ETH | 40 | 40 | 0 | true |

Each symbol still has 120 total zones. No #282 journal or other runtime file was
rewritten. Existing saved records continue to show their original validation
result; corrected replay is a separate in-memory view.

Both binding results:

- status = TACTICAL_IDENTITY_INCOMPLETE
- reason = EXPLICIT_H1_OR_CONTEXTUAL_REFERENCE_MISSING_OR_AMBIGUOUS
- Contextual_MSS = NONE; Active_Setup_ID = NONE
- origin_creation_allowed = false

Missing existing V4.7.4 requirements:

- BTC: DIRECTIONAL_DISPLACEMENT, LINKED_CONTEXTUAL_TRIGGER,
  LINKED_LIQUIDITY_CONFIRMATION, LINKED_ACTIVE_DEFENDABLE_ZONE,
  ELIGIBLE_EXECUTION_TRIGGER.
- ETH: M15_DIRECTIONAL_CONFIRMATION, LINKED_CONTEXTUAL_TRIGGER,
  LINKED_LIQUIDITY_CONFIRMATION, LINKED_ACTIVE_DEFENDABLE_ZONE,
  ELIGIBLE_EXECUTION_TRIGGER.

These are existing shadow predicate failures, not assertions that raw market
events do not exist. An explicit causal chain is still required. No confirmed
sample or tactical origin is manufactured by correcting zone identity.

## Verification

Eight targeted tests cover same-source distinct causes, deterministic IDs,
parent consistency, dedup/collision protection, unchanged FVG and Production OB
outputs, and persisted #282 BTC/ETH corrected replay with source parity checks.
The optional #282 archive test ran locally; it skips when that local archive is
not installed, and fails if a present source cannot reproduce the saved evidence.
Five files (Production state, Research ledger, Runner state, commit journal and
market input) had unchanged SHA256 across the explicit read-only replay.

Forward capture uses the new ancestry automatically when this code is loaded.
The existing Runner was not restarted. No historical backfill and no push.

Combined targeted/provenance/shadow/Reviewer/origin/research/Runner regression:
394 tests passed. Compileall and diff-check passed.
