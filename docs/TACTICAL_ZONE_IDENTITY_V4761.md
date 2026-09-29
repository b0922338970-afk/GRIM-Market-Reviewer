# V4.7.6.1 Zone identity deduplication

## #282 read-only findings

| Symbol | Zone rows | Repeated-ID groups | Extra rows | Identical extra rows | Different-payload groups |
| --- | ---: | ---: | ---: | ---: | ---: |
| BTC | 120 | 10 | 13 | 0 | 10 |
| ETH | 120 | 11 | 16 | 0 | 11 |

All colliding groups are OB. The differing field is evidence.displacement_strength.
`reviewer.find_order_blocks` iterates qualifying displacement events and selects
the last opposite candle in each event's existing ten-bar detector window. More
than one displacement can select the same candle/bounds, while its body_ratio
is exposed as a different displacement_strength. The V4.7.6 zone ID encodes the
OB candle, bounds, direction and timeframe, but not the causal displacement.

These are IDENTITY_COLLISION_DIFFERENT_PAYLOAD, not identical duplicates. Changing
IDs using strength, random values or a list index would not establish ancestry.
No ID changes or inferred displacement assignments were made in this patch.
A future ancestry repair needs an explicit, uniquely proven detector cause.

## Fix

Forward capture calls `deduplicate_zones`. Group by unchanged event_id; compare
the entire JSON payload with sorted object keys and compact canonical JSON,
rejecting NaN. Do not drop evidence, availability, bounds or setup fields during
comparison. Equal-ID groups with exactly one canonical payload retain one row.
Colliding groups retain every row and fail validation, even when some rows within
that group happen to be identical. Sort by timestamp, TF, ID and canonical payload.

The diagnostic adds counts and safe reason codes only. Non-zone collections,
Production selected output, hashes, checkpoint and origin identity are untouched.

`exposure_validation` separates RAW_EVIDENCE_UNAVAILABLE from
EXPOSURE_VALIDATION_FAILED. `valid_exposure` remains a boolean compatibility API.
Binding still reports TACTICAL_IDENTITY_INCOMPLETE, with an explicit validation
reason. The status CLI aggregates safe failure reasons without dumping evidence.
Validation never silently deduplicates persisted records.

## CORRECTED_VALIDATION_VIEW

Apply `deduplicate_zones` to an in-memory copy of persisted #282 exposure only.
For both BTC and ETH:

- Capture status remains AVAILABLE.
- Removed rows: 0. All 120 zones retained.
- valid_exposure = false.
- binding status = TACTICAL_IDENTITY_INCOMPLETE.
- binding reason = EXPOSURE_VALIDATION_FAILED.
- validation reason = EVENT_ID_COLLISION_DIFFERENT_PAYLOAD.

Thus #282 has not passed chain-level validation. This is the required fail-closed
result for different-payload collisions, not a claim that market evidence is
unavailable. No #282 journal, historical evidence or runtime file was rewritten.

## Validation

10 added tests, including optional persisted #282 BTC/ETH replay (executed on the
local archive). Combined targeted, tactical, shadow, direction, origin/research,
maturity and runner regression: 275 tests pass. Identical duplicate fixtures
validate and reach actual chain requirements; real collisions remain blocked.
No Production/rule/gate/Execution changes, no Runner restart, no push.
