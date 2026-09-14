# SMC sample status

```powershell
py -m market_reviewer.cli smc-sample-status
py -m market_reviewer.cli smc-sample-status --historical-only
py -m market_reviewer.cli smc-sample-status --root research/historical-replay/matched-smc-contrast.v1-verified
```

The default v2 report reads `freeze.json`, `frozen-states.json` and `membership.json` from the supplied historical directory, plus the existing `research/missed-opportunities.json` live episode store and `research/historical-replay/smc-hybrid-outcome.v1/outcome-join.json` for outcome completeness only. Override these with `--live-store` and `--historical-outcomes`. It does not replay, fetch, reconstruct SMC evidence, persist research, inspect/control the Observation Runner or modify Production. `--historical-only` preserves the original v1 output and three-file read contract.

The default is the verified matched expansion namespace, not the excluded initial trial. The command consumes existing categorical cells including symbol, direction, phase, observed regime, D1/H4 context, location, extension, liquidity scope and displacement. It never combines cells or counts trajectory checkpoints as origins.

Best cell is a deterministic display choice: largest `min(A, B)`, then largest total membership, then canonical context order. This is support monitoring, not an SMC score or changed attribution rule.

Each contrast reports its full context, historical/live/combined A/B counts, target 5 per side and preferred 8-10 per side. `MATCHED_SAMPLE_READY` requires >=5 classified origins per side. `OUTCOME_READY` requires >=5 per side with all four complete horizons, matching outcome reference identity, complete coverage and finite MFE/MAE. Pending outcomes do not affect sample membership. Outcome readiness is evaluated at the same sample-selected cell; outcomes never select another cell. `READY` aliases sample readiness. `NEXT_REVIEW_READY` and `READY_CONTRAST_COUNT` have separate `SAMPLE_READY` and `OUTCOME_READY` fields. Next review readiness requires at least two contrasts. These monitoring flags never start a review and are not statistical validation or execution readiness.

Live origins are keyed by tracker ID and symbol/direction/origin timestamp, not snapshot count, latest observation, outcome status or episode lifecycle status. Historical IDs (including the historical prefix) and matching origin identities are excluded; historical replay records cannot masquerade as live. Conflicting duplicate origin payloads remain unclassifiable. No implicit backfill is performed.

Live classification requires an already archived `smc_state` payload on the exact origin snapshot, using the existing `smc-hybrid-outcome.v1` state shape plus the complete nine-field `matching_context`. It must match the tracker identity and origin checkpoint (M5 open + 300), contain all six contrast fields, and contain no post-checkpoint availability. The monitor only supports reading such a payload; it does not add this field to the tracker schema or any producer. Current coarse live opportunity-evidence labels are not a substitute. Later snapshots are never used to repair missing origin evidence. Missing, conflicting, future or unrecognized evidence yields `LIVE_NOT_CLASSIFIABLE`.

Eligible archived fields use exactly the historical arm mappings and phase filter (CONTINUATION, PULLBACK, REVERSAL_CANDIDATE, EXHAUSTION). Composite reaction states are not split into separate arms. Context matching includes symbol, direction, phase, regime, HTF, location, extension, liquidity_type and displacement.

With unclassifiable origins, exact live/combined cell counts are `N/A`; `known_classifiable_live` and `known_combined` expose verified lower bounds separately. Readiness can still be true when known counts already satisfy the target; otherwise unknown contributors remain `N/A`, not false zero. Completed but unclassifiable origins count toward `OUTCOME_COMPLETE_LIVE_ORIGINS`, not a matched outcome-ready cohort.

A recorded empty arm is a known zero count. Missing files, missing arms, corrupt hashes, duplicate identities, context mismatches or no recorded cell return `N/A` with a reason, not zero evidence. Independent-origin totals remain available if their file is valid but membership is missing. Aggregate readiness is unknown when unavailable contrast data could change the answer.

Example for the current 45-origin frozen outputs: LONG 23 / SHORT 22; MSS 3/1; BOS 1/3; FVG/BPR 2/0; Single/Cluster 3/1; Failed OB/Breaker 1/0; Reclaimed/Accepted-outside 4/0. READY_CONTRAST_COUNT=0, TOTAL_CONTRAST_COUNT=6, NEXT_REVIEW_READY=false.

The historical-only example above remains unchanged. The current live store contributes two additional independent LONG origins (47 combined), both outcome-complete but lacking frozen Hybrid SMC state. They are reported as `LIVE_NOT_CLASSIFIABLE`; combined readiness is unknown, not an assertion that these two origins have no SMC evidence.
