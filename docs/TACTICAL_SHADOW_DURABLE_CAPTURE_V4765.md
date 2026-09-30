# Durable Shadow Tactical Capture (V4.7.6.5)

This is non-canonical, SHADOW_ONLY research. It never creates an origin,
changes a Reviewer decision, or changes Production/Research state or hashes.

## Capture and commit boundary

`execute_production_observation` captures while legal frames are in memory.
`recovery_payload.non_canonical_research_evidence.tactical_shadow_selection`
contains a versioned BTC/ETH bundle. Each symbol uses
`tactical-shadow-selection-input.v1`: direction/context, minimal review fields,
validated detector exposure, full liquidity ranking inputs, selected identity,
and zone-local retest coverage facts. No full OHLCV market dump is retained.

Ranking uses the existing Reviewer detector and selection semantics. Retest
scopes retain the existing detector's refreshed zone status, coverage counts,
source digest, and intersecting closed candles. Incomplete scope is UNAVAILABLE,
never an assumed no-touch or valid retest. Input validation enforces checkpoint
availability. Lifecycle transitions continue to use the V4.7.6.4 pure fold.

Locked targets are evaluated explicitly by the existing liquidity-event detector
even after leaving the display inventory. Their identity must match the preceding
shadow state. Complete retest scopes must end at the latest closed candle for
their own timeframe at the checkpoint; stale scopes cannot hide later failures.

Only after the observation journal has durably recorded both COMPLETE and
research_status COMPLETE does the best-effort hook commit the separate store.
The hook runs before website publishing; failures cannot suppress that hook,
rollback the observation, or alter its result. Recovery branches use the same
post-COMPLETE boundary. The next cycle may recover version-marked COMPLETE
receipts without reading overwritten market sources.

Dry runs never commit or recover shadow state. Recovery skips already verified
identical receipts and processes pending captures in observation order. A failed
earlier version-marked capture blocks later shadow appends, not the Runner.

## Storage and identities

- `artifact/tactical-shadow-inputs/<observation>.json`: immutable frozen bundle.
- `artifact/tactical-shadow-sequence.json`: `tactical-shadow-sequence-ledger.v1`.
- `artifact/tactical-shadow-health.json`: best-effort safe failure telemetry.
- `artifact/tactical-shadow.lock`: exclusive writer lock; no automatic stealing.

The existing same-directory temporary file, flush, fsync, and atomic replace
writer is reused. Inputs commit before the ledger. A crash between these writes
leaves an input that an identical retry can consume; conflicting input is never
overwritten. The ledger appends immutable state receipts and transition records.
A stale lock after process death blocks only shadow persistence and requires
operator inspection, not deletion by the Runner.

Processing identity is observation + symbol, committed as an atomic two-symbol
bundle. Same bundle is NOOP; a changed symbol or bundle is CONFLICT. Sequence
identity remains evidence anchored, not observation-count or wall-clock based.
BTC and ETH have independent current sequences; each symbol has at most one
current sequence, as in V4.7.6.4. No anchor means FORMING with no fabricated ID.

## Forward activation and status

Only new V4.7.6.5-marked captures with observation >=295 are eligible. The first
successfully persisted capture defines activation_observation. Observations
#282-294 remain development evidence and are never seeded into this store.
Already running Python processes are not restarted or reloaded by this patch.

`python -m market_reviewer.cli tactical-sequence-live-status --json`
is read-only. It reconstructs from frozen inputs, validates state and transition
receipts, and does not need the original market file. Corruption is UNAVAILABLE.
Failure counts reflect successfully persisted best-effort telemetry, not a
guarantee of telemetry survival during a storage outage.

Capture readiness requires four consecutive COMPLETE receipts, both symbols,
available evaluation, no conflict/corruption/pending newer failure or writer
lock, and deterministic replay with Production isolation receipts. Fresh-process
reproducibility is additionally covered by subprocess tests.

Sequence validation additionally requires an actual persisted forward
FORMING -> MSS_CONFIRMED progression with replay-verified evidence lineage.
Four FORMING observations alone cannot validate a sequence. Neither gate
authorizes origins, execution, or strategy readiness. No real runtime acceptance
is claimed until the newly activated Runner produces the required cohort.
