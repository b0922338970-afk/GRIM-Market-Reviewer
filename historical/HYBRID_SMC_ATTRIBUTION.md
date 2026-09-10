# V4.3 Hybrid SMC Attribution

Research namespace: `smc-hybrid-attribution.v1`.
No production imports except the runner's existing atomic JSON writer. No
production entrypoint, genesis, detector, threshold or decision snapshot changes.
No outcome values are read by the resolver or runner. Protected files may be
hashed for immutability; hashing is not an outcome join.

## Authority and availability

The frozen single-event resolver defines candidate routes. Explicitly
contradicted routes cannot regain authority through a cluster. A single eligible
sweep retains exact/unambiguous authority independently of membership uncertainty.
The only additional parent exclusions are direct symbol/direction/time conflicts
or frozen cluster context-break evidence naming that same sweep, available no
later than displacement open. Another member's break does not invalidate it.
These are existing break semantics, not new distance or time thresholds.

For multiple remaining plausible sweeps, escalation requires one existing
as-of-displacement clique containing ALL of them. Every member pair must carry
the existing compatible membership proof. Membership alternatives, context
breaks, acceptance resolution and known reclaimed/rejected versus accepted-outside
divergence veto escalation. Missing reaction/reference-family/common structure
evidence does not veto otherwise proven membership. No transitive merge and no
nearest/largest/first sweep selection occurs.

`parent_ambiguity_state` and `cluster_membership_state` are separate. All clique
members, including members without their own eligible displacement route, remain
in `member_parent_event_ids`; `eligible_single_parent_ids` identifies actual routes.
The frozen cluster view ID identifies the as-of proof, and the cluster parent ID
identifies membership. Parent IDs also include origin, displacement and authority.

Structure inheritance requires exactly one displacement parent in the original
structural DAG and authoritative hybrid liquidity ancestry. Competing structural
parents and competing displacement children are not resolved by this layer.
FVG/OB descendants use existing detector creation relations only.

## Reproduction

```text
python -m historical_research.run_hybrid_attribution
python -m historical_research.run_hybrid_attribution --verify
python -m unittest discover -s tests -v
python -m compileall historical_research market_reviewer tests
git diff --check
```

The runner verifies frozen manifest hashes and writes only
`research/historical-replay/smc-hybrid-attribution.v1/`. `--verify` runs a fresh
process with a different hash seed into its `verify/` subdirectory. Runtime
corpora are intentionally not committed. `cohorts.json` contains every regression,
grouping and diagnostic-difference case with event IDs and timestamped evidence.
`baseline.json` protects 395 pre-existing files, including the original archives,
production/research state and previous provenance datasets.

## Frozen 45-origin results

Counts are origin memberships, not independent observations or proof of outcomes.

| Attribution | Unambiguous | Ambiguous | None |
| --- | ---: | ---: | ---: |
| Single event | 974 | 966 | 38 |
| Cluster only | 824 | 1041 | 113 |
| Hybrid | 1145 | 780 | 53 |

Authority: exact single 0; unambiguous single 983; coherent cluster 162;
ambiguous single-event parents 533; ambiguous cluster parents 247; none 53.

All 268 membership regression cases are preserved: zero legitimate or unexpected
downgrades in that cohort. All 161 pure grouping cases escalate: zero remain
ambiguous or are invalidated. The extra coherent grouping follows direct exclusion
of a contradicted parent, not broader clique formation.

### Diagnostic reconciliation

The earlier diagnostic applied grouping only, retaining all old single parents.
Runtime checks additionally inspect same-sweep pre-displacement context breaks.
Exactly 36 displacement memberships differ:

| Diagnostic -> runtime | Count | Explanation |
| --- | ---: | --- |
| Ambiguous -> single | 20 | Directly contradicted competing parents excluded |
| Ambiguous -> coherent cluster | 1 | Remaining parents all in one proven clique |
| Single -> none | 11 | That parent has direct break evidence |
| Ambiguous -> none | 4 | All parents directly contradicted |

Thus `1135/805/38` becomes `1145/780/53`. The 11 downgraded singles are outside
the 268 membership-only cohort. Exclusion evidence uses existing opposing
displacement/BOS/MSS or reclaim-acceptance-outside records, never membership
uncertainty or future outcomes. All 36 individual cases are retained for review.

| Contextual structure | Single | Cluster | Hybrid |
| --- | ---: | ---: | ---: |
| MSS | 216 | 195 | 252 |
| BOS | 444 | 372 | 500 |

Hybrid chains on the SAME original root-chain denominator:

| Profile | Unambiguous | Ambiguous | Partial |
| --- | ---: | ---: | ---: |
| Reversal | 18 | 64 | 2078 |
| Continuation | 85 | 473 | 14656 |
| Unclassified | 3 | 101 | 3406 |

Cluster-only branch-chain counts have a different denominator and must not be
compared directly with these root-chain counts. Hybrid IDs preserve the original
root ID and include branch authority; they do not deduplicate independent origins.

Reaction divergence appears for 11 displacement memberships across 9 as-of
cluster views. It never authorizes cluster escalation. A valid individual parent
can still survive divergence in other cluster members. The largest remaining
recorded ambiguity reason is unresolved membership (517 displacement memberships;
reason counts can overlap), not missing shared structure confirmation.

## Interpretation and next action

Hybrid authority avoids the cluster-only downgrade and preserves conservative
direct contradictions. More contextual parent attributions do not imply better
outcomes or complete chains: reversal unambiguous chains remain 18, and large
partial-chain populations remain. Missing reaction/descendant evidence and
competing children still matter.

Recommendation: `VALIDATE_HYBRID_CHAINS_WITH_OUTCOMES`, in a later separate
descriptive join against these frozen identities. Do not infer improved returns
from the increased attribution counts. No outcomes have been joined here.
