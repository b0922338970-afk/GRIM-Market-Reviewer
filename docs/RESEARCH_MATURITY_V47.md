# Reviewer acceptance projection v1

`python -m market_reviewer.cli research-maturity` reads existing data only.
Add `--json` for the `research-maturity.v1` projection; `--root`,
`--live-store` and `--historical-outcomes` select existing sources.
No files, checkpoints, episodes or outcomes are created or repaired.

FIRST_REVIEW requires 5 fully complete, classifiable live independent origins
per direction. CALIBRATION additionally requires 8 per direction and valid
outcome metrics. Preferred support is 8-10 per direction, not a profitability
criterion. Execution remains RESERVED. These are acceptance input gates, not
strategy performance approval or executable rules.

The existing frozen-context classifier and independent-origin deduplication
remain authoritative. The verified 45 historical origins are used for identity
exclusion, never as live gate samples. Missing legacy frozen context remains
unclassifiable; nothing is reconstructed from subsequent observations.
Classification is evaluated before outcomes. The existing outcome readiness
contract requires all four horizons COMPLETE, matching reference timestamps,
complete coverage, and finite MFE/MAE. Summaries retain signed MAE and use only
included independent origins, one value per horizon per origin.

Origin observation/timestamp and frozen identity must agree. Production
sequence ID/state and review state at origin must agree with the origin
snapshot. Invalid frozen identity/time/schema, conflicting duplicates, missing
lineage, invalid episode state or malformed outcome metrics fail readiness
closed. Repeated/persistent mismatches therefore cannot be bypassed by a larger
sample count. Read-time ledger replacement returns SOURCE_CHANGED_DURING_READ;
no retry or write occurs. This validates stored provenance, not a cryptographic
proof against external rewriting of all source records.

Pending origins are excluded from complete support. They explain a shortfall
but do not block a sufficient complete cohort. Two legacy unclassifiable
origins likewise remain excluded, not zero market evidence. Missing stores
produce null counts and explicit blockers. Duplicate identical records count
once; conflicting records cannot establish readiness.

CLI and Website use the same gate. Website retains its existing presentation
keys (`SAMPLE_READY` maps to FIRST_REVIEW, `OUTCOME_READY` to CALIBRATION),
while `research.acceptance` exposes the detailed reasons and side metrics.
The matched-contrast monitor itself is unchanged. Public snapshot and hosted
schema are unchanged. No Runner restart, auto-execution, runtime migration,
historical backfill, publication or deployment is performed by this command.
