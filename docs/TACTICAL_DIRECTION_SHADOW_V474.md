# Multi-timeframe direction framework (shadow only)

CLI: `python -m market_reviewer.cli tactical-direction-shadow [--json]`.
`--journal` and `--live-store` select existing read-only sources. No report files
are created. The framework is not imported by Reviewer, Runner or origin code.

Machine-readable contract: `mtf-direction-shadow-contract.v1`.

| Role | Timeframes |
| --- | --- |
| Macro / primary direction | D1 / H4 |
| Swing setup context | H4 / H1 |
| Tactical direction | H1 / M15 |
| Execution / trigger | M15 / M5 |

This is a shadow role contract, not a change to the production timeframe rules.
PRIMARY uses the existing Swing Bias mapping. Missing/neutral primary remains
NONE, never LONG. Tactical direction and execution trigger are separate fields.

## Context vs confirmation

The latest visible H1 MSS/BOS anchors a possible tactical direction. Tied
opposing events do not establish direction. An agreeing M15 structure event or
non-invalidated exposed tactical FVG/OB must corroborate the H1 direction before
the tactical direction becomes LONG/SHORT. A single H1 indicator remains
TACTICAL_CONTEXT with tactical NONE. This is directional context, not causality:
nearby inventory is never treated as proof of same-chain setup ancestry.

CONFIRMED additionally requires M15 direction agreement; selected VALID/STRONG
same-direction displacement; explicit contextual MSS references to that exact
displacement and sweep; a matching swept/reclaimed liquidity reference; the
active SETUP_FVG identity with displacement provenance; and an eligible later
closed retest matching that setup. No ARMED or origin permission is granted.
Counter-primary context is allowed, but missing lineage is INCOMPLETE, not
inferred confirmation. Existing exposed OB/BPR/Breaker context does not replace
missing proof of the active zone/retest chain. This conservative v1 does not
invent OB/BPR/Breaker trigger provenance absent from the Reviewer output.

Protection, global phase and tactical draw are reported as exposed context.
M15-specific phase is UNAVAILABLE rather than inferred from global phase.
BPR/Breaker values are copied only from existing smc_context; no new detector.
No separate rejection detector is invented from sweep/reclaim presence.

Each structured event must be available by its own timeframe close boundary.
Selected displacement has no timeframe in its exported text, so its provenance
is explicitly CLOSED_REVIEW_OUTPUT, not an invented M5 identity. Confirmation
also checks its exact contextual link and subsequent closed structure/setup/
retest chronology. The shadow relies on accepted COMPLETE review closed-evidence
semantics; it is not an independent raw-candle detector or availability proof.

## Replay and isolation

Replay is classification of archived COMPLETE reviews, not market replay.
Early tracker-only observations supply primary bias but tactical evidence is
UNAVAILABLE. No outcomes or maturity gates are used for classification.
24H/72H are inclusive windows ending at the latest archived M5 availability
checkpoint. Counts are symbol-observation presence, NOT independent samples.
Repeated zones/events may appear in many observations. Missing evidence is
counted on INCOMPLETE rows and never assigned market-negative meaning.

Future origin_scope/direction_source fields exist only in the disabled contract.
No tactical origin type, eligibility changes, ledger writes, execution APIs,
website changes, runner hooks or migrations are introduced.
Recommendation remains KEEP_SHADOW_ONLY until an independently authorized
validation establishes adequate complete, linked evidence.
