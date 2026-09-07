# V4.3 SMC Provenance v1.1

Research-only liquidity-to-structure ancestry on the same 45 frozen historical
origins. No production imports this package. No live integration, scoring,
weights, fitted thresholds, genesis changes, new market fetch, or training.

## Reproduction

Use a working Python 3.12 runtime from the repository root:

```powershell
python -m historical_research.run_smc_ancestry_v11
python -m historical_research.verify_smc_ancestry_v11
python -m unittest discover -s tests -v
python -m compileall -q market_reviewer historical_research tests
git diff --check
```

Local prerequisites, intentionally not added to the source commit:

- `research/historical-replay/smc-provenance-v1/frozen-origin-graphs.json`
- The v1 `baseline.json` and raw historical files referenced by event provenance.
- `smc-quality-v1/frozen-features.json` and `outcome-join.json`.
- `first-feature-review/cohorts.json`.

All new data is written under
`research/historical-replay/smc-provenance-v11/`. Missing prerequisites are errors;
the driver does not fetch or manufacture them. Original data and live state are
read-only. The verifier checks 182 protected files including the live thesis and
missed-opportunity store, and runs two fresh processes with hash seeds 11 and 97.

## Decision-Time Contract

Event timestamp is candle OPEN. Evidence availability is no earlier than OPEN
plus its own timeframe duration. Additional displacement confirmation may delay
availability. The engine crops raw candles at the exact confirmation checkpoint,
and reconstructs the opposing confirmed swing using the prefix ending at the
sweep's OPEN, before the sweep occurred.

The fixed causal deadline is:

`sweep.available_at + 5 * liquidity_timeframe_seconds`.

Five bars reuse the existing two-left/centre/two-right pivot footprint. It is a
research convention, not a calibrated optimum. Confirmation exactly at the
deadline is allowed. Displacement OPEN must be at or after parent availability.
Same sweep candle cannot also be its later reclaim/rejection. No outcome is read
until all 45 ancestry graphs have been frozen.

Rejection means a later candle re-probes beyond the exact swept price and closes
inside with a directionally aligned body. It can coexist with reclaim; earliest
available reaction wins, reclaim first on ties. Accepted-outside requires no
qualifying reaction and a closed deadline candle outside, with the entire causal
window present. Incomplete windows remain unresolved. Unlinked displacement is
not interpreted as absent displacement.

An ancestry link additionally needs exact-price interaction and penetration of
the pre-sweep confirmed opposing swing. Without reaction, the displacement body
must cross back through the swept price. With reaction, its range must intersect
the reaction candle range. A generic structure break must match the exact
pre-sweep reference price. This is deliberately narrower than proximity.

Opposing structure/displacement, sweep-extreme loss, loss of reclaimed liquidity,
and aligned protected-level loss block sweep-to-displacement attribution.
Inherited v1 context breaks terminate chains. Events after the first known break
are excluded; future breaks cannot rewrite the current decision graph.
Multiple eligible sweep parents are rejected as ambiguous, not resolved by
nearest price, outcome, or distance optimization.

Generic MSS needs a valid reaction and explicit liquidity/sweep/displacement/
structure parentage before contextual promotion. Continuation BOS separately
requires explicit INTERNAL liquidity and continuation/pullback phase reverified
at displacement confirmation. Equal highs/lows are not silently assigned an
internal/external scope. A reversal chain requires explicit EXTERNAL liquidity.

FVG/OB must share the contextual event's exact displacement parent. A zone on
another branch of the same component is insufficient. OB occurrence can precede
displacement, but its derivation is only available once displacement confirms.
Thus an OB relation can have negative occurrence delta, never future availability.

Every relation stores deterministic identity, parent/child IDs, type, reason,
occurrence and availability deltas, direction/timeframe compatibility and raw
source proof. Original event IDs remain stable; new identities are versioned.
Chains use immutable root ancestry, not later outcome. Components can acquire
different observed statuses across origins; membership counts are not independent
sample counts and must not be summed as such.

## Reconstruction Results

| Event / relation | Unique count |
| --- | ---: |
| Liquidity | 827 |
| Exact liquidity sweeps | 418 |
| Reclaimed sweeps | 229 |
| Accepted-outside sweeps | 82 |
| Sweep-linked displacement | 3 |
| Reaction-linked displacement | 2 |
| Contextual MSS | 1 |
| Contextual continuation BOS | 0 |

Observed unique chain/status memberships: REVERSAL 0, CONTINUATION 0,
PARTIAL 1681, FAILED 1304. This inventory includes isolated context components.

Primary origin chains: REVERSAL 0, CONTINUATION 0, PARTIAL 25, FAILED 20.
Primary selection is the latest known non-singleton root aligned with the origin
direction, with stable chain-ID tie breaking. It never selects deepest/best chain
using outcomes. L0 includes components with no displacement ancestry, such as
sweep/reclaim-only context; it does not mean absolutely no relationships.

Depth distribution: L0=27, L1=18, L2=L3=L4=L5=0. The one contextual event occurs in
older context, not the selected primary chain. Continuation BOS is separately
reported and is not artificially assigned reversal-specific L4/L5.

## Origin Outcome Comparison

Each cell below is median MFE% / signed MAE%. N counts independent origins, never
events. L2-L5 and complete reversal/continuation groups have N=0 and null medians.

| Group | N | LONG/SHORT | 1H | 4H | 12H | 24H |
| --- | ---: | --- | --- | --- | --- | --- |
| L0 | 27 | 14/13 | 0.3050/-0.4404 | 0.5078/-0.7561 | 0.8453/-1.2677 | 1.6686/-1.5901 |
| L1 | 18 | 9/9 | 0.6490/-0.4823 | 1.3938/-0.6993 | 2.1170/-0.7368 | 4.3911/-1.5274 |
| PARTIAL | 25 | 15/10 | 0.3825/-0.3751 | 0.6124/-0.6886 | 1.7983/-0.7637 | 2.8635/-1.1832 |
| FAILED | 20 | 8/12 | 0.3958/-0.6022 | 0.6054/-0.9768 | 1.7887/-1.6020 | 2.0408/-2.0706 |

Decision-phase mix (CONTINUATION / PULLBACK / REVERSAL_CANDIDATE):
L0=6/15/6; L1=10/4/4; PARTIAL=14/10/1; FAILED=2/9/9.
The machine-readable `outcome-comparison.json` contains all horizon medians,
full retrospective regime/direction mixes, decision-phase splits and frozen
TOP/MID/LOW/FRAGILE memberships. Twelve later origins remain UNASSIGNED to the
old cohort definition; cohorts are not refitted. Retrospective regime is used
only for stratification after graphs freeze, never for attribution.

Aligned isolated inventory occurs in 40/45 origins for sweep, 45/45 for generic
MSS/BOS, 45/45 for FVG, and 44/45 for OB. These nearly universal inventory fields
cannot establish useful separation alone. Aligned sweep-linked and reclaim-linked
displacement occur in only 2 origins, both LONG / TREND_PULLBACK; the third
linked displacement is opposite its origin direction. Their 24H median MFE/MAE
is 1.2949/-5.2495, versus 2.8635/-1.3034 for the 43 without aligned linkage.
One aligned contextual MSS origin has 24H 2.2072/-2.4760. These tiny, imbalanced,
overlapping groups do not establish superiority or inferiority.

L1's larger MFE is descriptive, with materially different phase/regime mixes.
It is not evidence that full liquidity ancestry outperforms isolated inventory.
`CHAIN_PROVENANCE_SIGNAL = NONE` for demonstrated incremental separation.
This does not mean the hypothesis is disproven.

## Specific Findings And Limits

- No external-sweep branch survives all linkage requirements into displacement
  and contextual MSS. The main missing stage is external liquidity reaction to
  a relevant, unbroken, exact-reference displacement/structure branch.
- The one contextual MSS is BTC M5 bullish @1696191000 through protected high
  27114.48, rooted in EQUAL LOWS, not proven EXTERNAL liquidity. It cannot be
  relabeled a complete external reversal.
- The observed bullish BOS ancestor is also equal-lows scoped, not proven
  INTERNAL. The internal-liquidity linked case is MSS without qualifying reaction,
  not contextual continuation BOS. Zero continuation chains is therefore explicit
  provenance scarcity, not a reason to relax definitions.
- Failed chains retain the earliest known source defense/opposing-structure/
  opposing-displacement break, or covered-window acceptance/no-reaction/no-D
  failure. A rejected ancestry candidate is retained with reason and proof; it
  is not automatically a failed market opportunity.
- `DISPLACEMENT_NO_STRUCTURE_BREAK` is represented by rejected-link
  `NO_STRUCTURE_PENETRATION`, not a fabricated linked chain. Unsupported terminal
  absence claims (`MSS_NO_FOLLOW_THROUGH`) and specific immediate-failure timing
  (`LINKED_FVG_IMMEDIATE_FAILURE`) are not invented from missing evidence. Source
  zone defense failure remains `CHAIN_DEFENSE_FAILURE`. These finer failure
  categories require separately observable confirmation before use.
- No PDH/PDL/PWH/PWL labels are manufactured: this reconstruction uses the
  existing confirmed swing/EQ/external/internal inventory where available.
- Legacy event quality/mitigation attributes retain their original
  `attributes_known_at` / decision context. They must not be interpreted as
  known at original event occurrence. New ancestry uses cropped raw proofs.
- Rejection and the five-bar causal boundary are transparent uncalibrated
  research conventions. They are not evidence of actual institutional intent
  or economic causation. Generic detector output may be conservative about its
  first-known timestamp; this is not a live-ready event emitter.
- No new horizons or independent episodes were added. No original snapshot or
  prior quality cohort was rewritten.

## Future Frozen Live Fields (Definition Only)

`liquidity_event_id`, `sweep_event_id`, `reclaim_event_id`, `rejection_event_id`,
`displacement_event_id`, `contextual_structure_event_id`, `linked_fvg_id`,
`linked_ob_id`, `smc_chain_id`, `chain_type`, `ancestry_depth`, `chain_status`.

Each field must carry its availability, source identity and snapshot checkpoint;
unknown values remain null. No live schema or decision code is changed here.

## Verification

32 deterministic tests cover exact identity, reaction ordering, acceptance,
bounded attribution, context breaks, contextual/generic separation, same-parent
zone inheritance, partial/failure construction, no revival, unavailable-window
safety, future-break isolation, immutable inputs and IDs.

Fresh-process frozen graph SHA-256:
`b464844dec8f7e499acd1868daf63d692c0f49ba99600699d6d081d2f75ce3d3`.
Fresh-process comparison SHA-256:
`1e89d7787fd05fadb26c367e41828e3b3ee28cceaf942c418fbc540be8da0b85`.

Detailed verification and full test output remain in the new runtime output
directory. The installed system Python 3.12.0 has incompatible stdlib files;
validation uses the Codex bundled Python 3.12.14, without modifying the system.

Recommendation: `KEEP_CHAIN_RESEARCH_ONLY`. Inspect additional chain-eligible
historical examples before freezing a predictive profile. No score or threshold
change is supported by these results.
