# Liquidity Interaction Cluster Foundation v1

Namespace: `smc-liquidity-cluster.v1`. Research only; no production authority.
45 frozen raw-prefix origins and the frozen parent-attribution results are inputs.
No original events, inventories, snapshots, production rules or genesis are changed.
Outcome files are never consumed to decide membership or ancestry.

## Membership contract

A cluster represents one conservatively bounded, observed liquidity-taking
interaction. This foundation covers references with an exact raw SWEEP_OF event;
unswept inventory references are not manufactured into interaction episodes.

Pair compatibility requires the same symbol and liquidity side, overlapping
interaction intervals, and either identical reference price or explicit nested
sweep-candle geometry. Nested geometry requires an internal reference and both
reference prices inside both interacting candle ranges. Overlap alone is not
enough. No price tolerance, learned time window or proximity ranking is added.

Opposing confirmed structure/displacement, sweep-extreme loss and post-reclaim
acceptance outside are context-break evidence. Only fully closed candles and
already available events can establish those breaks. The existing raw-prefix
price/temporal/hierarchy proof and parent-attribution route exclusions still apply.

Compatibility components must be complete pairwise-compatible groups. If A-B and
B-C are compatible but A-C are not, this implementation does not greedily choose
an ordering or merge transitively. It retains singleton UNRESOLVED_CLUSTER records
and lists every unresolved membership alternative. This conservative fallback is
a major limitation, not proof that all such interactions are unrelated.

Cluster identity hashes the namespace, symbol, direction, reference IDs and sorted
sweep IDs. All original event IDs survive unchanged. Nested member depth, earliest
and latest sweep times, reference composition, pair proofs, reactions and context
breaks remain inspectable. A different membership set creates a different cluster
identity; a cluster view adds an explicit as-of timestamp.

## Temporal and lifecycle contracts

Origin-level membership is not retroactively applied to earlier displacements.
Each displacement uses a separate view frozen at its own open time. A sweep whose
confirmation becomes available later cannot join that earlier parent view, even
if its candle opened earlier. Higher-timeframe open candles cannot supply context
break evidence. Earlier views remain unchanged when later members or breaks appear.

Member reaction classification reuses the existing v1.1 sweep_state/deadline
semantics, including the existing five-sweep-bar footprint and reclaim-before-
rejection tie handling. This is not a new threshold or a retuned causal window.
Out-of-contract raw reactions are listed in excluded_reaction_ids, not silently
included as effective reaction members. All members must have compatible resolution states for a cluster-wide resolution.
Mixed member responses remain UNRESOLVED. SWEPT is never automatically RESOLVED.

`resolution` records the known reaction result; `lifecycle_state` separately records
subsequent INVALIDATED evidence within that reaction horizon. Thus historical
RECLAIM_RESOLUTION and current INVALIDATED can coexist without treating the cluster
as a valid displacement parent. INVALIDATED or accepted-outside cluster views cannot
be promoted. No execution-ready meaning is assigned to a resolution label.

This foundation does not force IDENTIFIED/INTERACTING/RESOLVED transitions when the
raw detector has no corresponding evidence. Observed states are SWEPT, RECLAIMED,
REJECTED, ACCEPTED_OUTSIDE and INVALIDATED. Sequential non-overlapping sweeps are
not merged, even at the same price; longer multi-stage episodes need more evidence.

## Ancestry contract

DISPLACEMENT_AFTER_CLUSTER preserves supporting original candidate relation IDs
and all five factor groups: direction, temporal continuity, locality, context and
structure consequence. It never creates a new displacement candidate that lacked
a surviving event-level route. Multiple possible clusters or unresolved membership
stay AMBIGUOUS_CLUSTER_PARENT. One parent cluster does not select a winner sweep.

Contextual BOS/MSS requires both unique displacement ancestry for the structure
event and an unambiguous cluster parent for that displacement. Structure reference
proofs, confirmed swing penetration and protected structure evidence are retained.
These are research contextual labels, never production MSS or entry signals.

Chains retain exact detector-linked FVG/OB descendants. Reversal candidates require
external composition, known reclaim/rejection, displacement, MSS and an existing
linked zone. Continuation candidates require internal composition, resolution, BOS
and a linked zone. The direction is the existing compatible event direction; no new
trend detector is introduced. Mixed clusters may expose both profiles, explicitly
sharing ancestry rather than becoming independent samples.

Chains are per cluster-view/displacement/profile branches, not complete unique
market episodes. Multiple branches are preserved; no best child is selected. A
same-displacement FVG is not silently made into a production post-MSS SETUP_FVG.

## Frozen results

All counts are origin memberships across 45 independent origins, not new episodes.
Origin cluster counts exclude the separately stored displacement-time views.

| Cluster type | Count |
| --- | ---: |
| External | 1,510 |
| Internal | 7,092 |
| Nested | 39 |
| Mixed | 83 |
| Unresolved | 9,514 |
| Total | 18,238 |

20,884 sweep memberships: mean 1.1451, median 1, maximum 7 sweeps per cluster.
Maximum observed nested depth is 1. There are 7,597 non-clique unresolved singleton
records; the other unresolved types lack a definitive external/internal profile.

Resolution counts: reclaim 12,669; rejection-only 0; accepted outside 5,123;
unresolved 446. Zero rejection-only clusters does not mean no rejection events:
the inherited first-reaction tie handling selects reclaim when both coincide.
Current lifecycle: RECLAIMED 5,593; INVALIDATED 11,658; SWEPT 147;
ACCEPTED_OUTSIDE 840. These are interaction states, not trading outcomes.

### Same-denominator ambiguity comparison

| Parent state | Prior single-sweep | Cluster |
| --- | ---: | ---: |
| Unambiguous | 974 | 824 |
| Ambiguous | 966 | 1,041 |
| None | 38 | 113 |
| Candidate displacements | 1,978 | 1,978 |

172 previously ambiguous candidates become unambiguous: 161 solely group all
prior valid sweep parents into one cluster; 11 also require additional context
exclusions. Another 773 remain ambiguous and 21 lose all eligible cluster parents.
Of 974 previously unambiguous candidates, 652 remain so, 268 acquire membership
uncertainty and 54 fail cluster continuity. All 38 previously invalid cases stay
without a parent. Do not present the 172 as a net reduction: ambiguity rises by 75.

Of 824 unique cluster parents, 465 have known reclaim resolution at displacement
open and 359 are unresolved interactions. Unique parent does not mean a full chain.

Contextual MSS after cluster: 195; contextual BOS after cluster: 372. There are
823 ambiguous structural candidates, 1,518 generic MSS and 5,880 generic BOS.
These reconcile to the complete raw 1,923 MSS and 6,865 BOS universe.

| Chain profile | Unambiguous candidate | Ambiguous candidate | Partial |
| --- | ---: | ---: | ---: |
| Reversal | 21 | 24 | 416 |
| Continuation | 86 | 116 | 2,855 |
| Unclassified | 5 | 37 | 675 |

The old per-liquidity-root chains and these per-cluster/displacement branches have
different denominators; their totals must not be interpreted as a performance gain.

## Representative cases

For ETH origin HIST-ETH-LONG-MOT-1610074500-c05877f0d46f:

- DISPLACEMENT-015ac90093592292922e1e56 at open 1609883100 now has cluster
  LIC-cd7ba10ca022894578b43cc2. It retains three sweeps, including the two former
  plausible parents SWEPT-449b2038e5007c0921f43a50 and
  SWEPT-49bc0e96f3d9af95db925f4a, without selecting either as a winner.
- DISPLACEMENT-56d2e9f8337091ca69c5f4cb at open 1609911600 remains ambiguous.
  Its six candidate sweep views belong to a non-clique membership component;
  proximity and transitive nesting are insufficient to certify one cluster.

Full proofs and IDs are in the namespace's examples.json and origins/*.json files.

## Validation and reproduction

```text
python -m historical_research.run_liquidity_cluster
python -m historical_research.run_liquidity_cluster --verify
python -m unittest discover -s tests -v
python -m compileall historical_research market_reviewer tests
git diff --check
```

28 new tests; 700 full-suite tests passed. Fresh-process determinism compared 48
files with a different hash seed. All 294 protected files were hash-identical.
The driver only writes the new namespace, using atomic JSON writes. There is no
outcome join. Reconstructing all 45 origins took about 14.6 seconds on this machine.
Peak membership pair checks per origin/view: 7,766. Time-indexed overlap scanning,
cached pair proofs and targeted displacement views avoid a global all-to-all join.

Conclusion: cluster abstraction improves representation for a specific subset,
but does not yet demonstrate globally better causal attribution. The pairwise
clique requirement exposes unresolved membership rather than forcing a result.
Recommended next action: REFINE_CLUSTER_MEMBERSHIP, keeping research isolated.
Do not loosen thresholds to maximize this batch's ambiguity-reduction count.
