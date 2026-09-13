# SMC sample status

```powershell
py -m market_reviewer.cli smc-sample-status
py -m market_reviewer.cli smc-sample-status --root research/historical-replay/matched-smc-contrast.v1-verified
```

Reads only `freeze.json`, `frozen-states.json` and `membership.json` from the supplied output directory. It verifies frozen hashes and origin/context identity. It does not replay, fetch, recompute matching, join outcomes, persist research, inspect/control the Observation Runner or modify Production.

The default is the verified matched expansion namespace, not the excluded initial trial. The command consumes existing categorical cells including symbol, direction, phase, observed regime, D1/H4 context, location, extension, liquidity scope and displacement. It never combines cells or counts trajectory checkpoints as origins.

Best cell is a deterministic display choice: largest `min(A, B)`, then largest total membership, then canonical context order. This is support monitoring, not an SMC score or changed attribution rule.

Each contrast reports its full context, A/B counts, target 5 per side, preferred 8-10 per side, and READY. NEXT_REVIEW_READY is true when at least two of the six priority contrasts have a best cell with >=5 per side. This flag never starts a review or changes research/trading rules. It is not statistical validation or execution readiness.

A recorded empty arm is a known zero count. Missing files, missing arms, corrupt hashes, duplicate identities, context mismatches or no recorded cell return `N/A` with a reason, not zero evidence. Independent-origin totals remain available if their file is valid but membership is missing. Aggregate readiness is unknown when unavailable contrast data could change the answer.

Example for the current 45-origin frozen outputs: LONG 23 / SHORT 22; MSS 3/1; BOS 1/3; FVG/BPR 2/0; Single/Cluster 3/1; Failed OB/Breaker 1/0; Reclaimed/Accepted-outside 4/0. READY_CONTRAST_COUNT=0, TOTAL_CONTRAST_COUNT=6, NEXT_REVIEW_READY=false.
