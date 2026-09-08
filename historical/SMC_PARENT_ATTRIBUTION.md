# SMC Parent Attribution Resolver v1

Research namespace: `smc-parent-attribution.v1`.
Inputs: 45 frozen `smc-provenance-raw-prefix.v1` origins and their hash-verified
raw candle sources. Original inventories and previous provenance remain separate
authorities. No production imports are patched and no outcomes are read for attribution.

## Contract

Every existing displacement candidate route is retained, including contradicted
routes. Sweep, reclaim/rejection and liquidity IDs remain attached. Routes sharing
one sweep identity form one parent unit, not competing parents.

Nine factors return SUPPORTS_PARENT, NEUTRAL, CONTRADICTS_PARENT or UNAVAILABLE:
temporal proximity, price locality, direction compatibility, liquidity relevance,
reclaim continuity, structure consequence, timeframe compatibility, context
continuity and supersession state. Unavailable evidence is not negative evidence.
No score, weights, optimized thresholds or universal liquidity ranking exist.

Temporal/locality/hierarchy constraints reuse the raw-prefix contract, including
its five-liquidity-bar causal window and SWEEP_TFS matrix. The nearest sweep does
not win. Continuity checks only use candles fully closed before displacement open;
overlapping higher-timeframe candles cannot veto a parent. A direct sweep route
cannot bypass a reclaim that was subsequently accepted outside its level.

Supersession requires the same liquidity identity, non-overlapping events, an
observed prior context break and a still-valid later parent. Same-price cluster
membership alone is insufficient. Cluster equality uses exact price, not a new
distance tolerance. Internal nested price geometry preserves ambiguity.
No supported historical supersession was found in this batch.

Exact identity requires explicit detector/reference evidence whose source is the
displacement itself or a linked structure consequence, legally available at the
checkpoint. Current raw graphs have no explicit_parent_identities records: EXACT
is therefore zero. Synthetic tests exercise this optional contract only. A generic
structure reference mismatch alone cannot exclude another plausible parent.

## Results

Counts below are origin memberships, not new independent episodes.

| Population | Unambiguous | Ambiguous | None/partial |
| --- | ---: | ---: | ---: |
| Exact 229 reclaimed sweeps | 1 | 26 | 202 no valid candidate |
| 1,978 displacements with candidate routes | 974 | 966 | 38 no valid parent |
| Contextual MSS parent-only candidates | 216 | 204 | 1,503 generic |
| Reversal chains | 18 | 66 | 2,076 partial |
| Continuation chains | 63 | 504 | 14,647 partial |

EXACT_PARENT = 0; SUPERSEDED_PARENT = 0. Parent-unit states include 974 ACTIVE,
2,695 NESTED, 75 PARALLEL and 1,101 UNRESOLVED. These are not displacement counts.
The 229-sweep reconciliation is unchanged from 1/26/202. Multiple valid children
still make a reclaimed sweep ambiguous even when each child's parent is unique.

### MSS comparison caveat

The previous 109/318 MSS counts used joint ancestry uniqueness, including competing
sweep children. This resolver reports parent-only uniqueness: exactly one structure
displacement parent and one surviving sweep parent. Of 110 old-ambiguous to
new-unambiguous memberships, 107 already had a unique displacement parent in the
raw graph; only 3 actually resolve previously multiple sweep parents. Another
106 remain unambiguous; 3 former-unambiguous and 4 former-ambiguous become generic
after continuity exclusion. Do not interpret 216 as 216 proven causal chains or
the increase as 110 new causal discoveries. Chains retain child ambiguity.

### Ambiguity causes

Exclusive primary reporting categories reconcile to 966: same-cluster sweeps 663,
nested sweeps 237, cross-timeframe collision 10, timing overlap 41, no price-locality
separation 15, no structure separation 0, other 0. Reporting precedence only avoids
double counting; it is not an attribution ranking.

Overlapping diagnostic labels: same cluster 663, nested 592, cross-timeframe 367,
timing overlap 933, no price separation 966, no structure separation 966. The last
two mean existing evidence cannot exclude remaining parents, not missing prices
or missing structure events. Cross-timeframe relationships are preserved, not
rejected solely for using different timeframes.

## Representative historical cases

Full IDs and parent lists are in `examples.json` under the research namespace.

- Nested/cross-TF: `DISPLACEMENT-56d2e9f8337091ca69c5f4cb`, open 1609911600,
  origin checkpoint 1610074800. Six surviving parents, including
  `SWEPT-27ee038881c65316d812cc8d` (open 1609906500) and
  `SWEPT-4939e377254df3d3deffbecb` (open 1609907100), both available 1609907400.
  Overlapping/nested observations remain ambiguous.
- Unresolved: `DISPLACEMENT-015ac90093592292922e1e56`, open 1609883100, with
  `SWEPT-449b2038e5007c0921f43a50` and `SWEPT-49bc0e96f3d9af95db925f4a`.
  Both sweeps opened 1609882200 and became available 1609882500; distinct liquidity
  IDs survive. Neither nearest time nor inventory order breaks the tie.
- Exact parent, unambiguous-after-supersession and structure-resolves examples:
  NO_REAL_EXAMPLE. Only synthetic tests exercise these contracts; no examples
  are manufactured from historical outcomes.

## Reproduction and safety

Run from repository root with a working Python runtime:

```text
python -m historical_research.run_parent_attribution
python -m historical_research.run_parent_attribution --verify
python -m unittest discover -s tests -v
python -m compileall historical_research market_reviewer tests
git diff --check
```

The driver writes only `research/historical-replay/smc-parent-attribution.v1/`.
Fresh verification uses a second process and a different hash seed; 49 deterministic
files matched. All 242 protected input files remained hash-identical. Runtime for
the 45-origin reconstruction was about 8.9 seconds on this machine. Timing is
excluded from deterministic comparison. Candidate units are grouped before pair
comparison; this is not a global event all-to-all join.

26 new tests cover nearest-event rejection, exact identity, supersession, nested
parents, hierarchy, reference/locality/continuity, accepted outside, MSS/chains,
ambiguity, future evidence, fresh-process determinism and input/production isolation.

Recommended next action: KEEP_AMBIGUITY_RESEARCH_ONLY. Most remaining collisions
need stronger detector-level identity evidence, not looser windows or outcome ties.
No production changes, scoring, training, outcome joins or push.
