# V4.3 Raw-Prefix SMC Provenance Foundation

## Authorities and isolation

`ORIGIN_INVENTORY` remains the frozen original Reviewer decision output. It is
not backfilled. `RAW_PREFIX_RESEARCH` is a separate candidate-only authority in
`research/historical-replay/smc-provenance-raw-prefix.v1/`.

Production rules, genesis, thresholds, live state, original snapshots and older
provenance outputs are not edited. The driver protects 189 existing files by
content hash, including original window documents, raw sources and live state.
No scoring, weights, training, threshold optimization or production integration.

## Frozen-prefix contract

Each origin supplies its immutable source hash and checkpoint. A candle is
visible only when its OPEN timestamp plus its own timeframe duration is no later
than that checkpoint. The reconstruction core accepts frames and origin metadata,
performs no file I/O, and has no outcome input.

Every event carries its symbol, direction, timeframe, OPEN identity timestamp,
`available_at`, evidence, source hash, status and decision-time context. Event
attributes computed at the origin are explicitly marked `attributes_known_at`.
Displacement candidate qualification uses separately stored confirmation-prefix
evidence, not later strength/follow-through upgrades.

Displacement availability is reverified at the event close and the next two
closes. Generic structure breaks are reverified at their event-close prefix.
Liquidity is verified after two right-hand confirmation candles. Where an earlier
confirmation cannot be reproduced, availability is conservatively the origin
checkpoint, labelled `ORIGIN_VERIFIED_UPPER_BOUND`. This is not an assertion of
earlier first knowledge. OB creation can become available after its source candle.
Creation relationships wait for all required evidence even when their timestamps
refer to the same candle or to the earlier OB source candle.

## Detector filters versus display limits

Uncapped FVG, OB and equal-level adapters compile private AST copies of existing
detectors. Exactly one recognized return slice is removed from each copy. An
unexpected source shape fails closed. No production function/global is replaced.
Equivalence tests compare the uncapped tails against the original detectors.

Removed display limits: structure last four, FVG last16/cross-TF24, OB
last8/cross-TF12, equal levels last3, and the latest high/low display cursor.
Every historically confirmed swing becomes a research reference. Original
inventory visibility is compared against the archived review, not the enriched
v1.1 graph. Visibility is evaluated only after candidate generation.

Retained detector truth:

- Strict two-left/two-right confirmed swings and HH/HL/LH/LL semantics.
- Equal-level tolerance `max(prefix close * .001, .01)` and consecutive same-side swings.
- First exact penetration after the reference becomes available; no same-confirmation-candle sweep.
- Later inside close/re-probe for reclaim/rejection. Same-candle sweep and reclaim are not collapsed.
  Raw reclaims remain in the universe through the origin checkpoint, including
  those outside the ancestry window. Rejection detection and displacement
  candidate qualification retain the inherited window. Origin liquidity status
  reflects legally observed same-timeframe events, not an unconditional UNSWEPT.
- Existing previous20 displacement medians, component predicates and VALID/STRONG qualification.
- Three-candle FVG geometry, exact same-third-candle displacement creation provenance.
- OB requires a valid structure-breaking displacement and its last opposite candle within10 bars.
- The inherited five-bar pivot-footprint research window, not a calibrated causal horizon.

Historical references are preserved even when no longer the current display
cursor. No new expiry or selection rule is invented. Equal levels retain unknown
external/internal significance instead of being silently assigned a scope.

## Candidate matrices and locality

Liquidity-to-sweep and sweep/reaction-to-displacement candidate matrix:

| Parent TF | Permitted child TF |
|---|---|
| D1 | D1, H4 |
| H4 | H4, H1 |
| H1 | H1, M15, M5 |
| M15 | M15, M5 |
| M5 | M5 |

Displacement-to-structure candidate matrix:

| Displacement TF | Permitted consequence TF |
|---|---|
| M5 | M5, M15, H1 |
| M15 | M15, H1 |
| H1 | H1, H4 |
| H4 | H4, D1 |
| D1 | D1 |

Rejected higher-TF displacement candidates remain visible in diagnostic rows;
they are not accepted via the structure bridge. Candidate generation is indexed
by symbol, timeframe, event type, direction and timestamp with bisected windows.
No unbounded all-event Cartesian graph is constructed.

For sweep/reaction-to-displacement, the D OPEN must follow parent availability;
confirmation must fit the inherited liquidity-TF window. Direction must align.
The D body crosses the exact swept price, or its range overlaps the reaction
candle. Context vetoes retain opposing structure/displacement, swept-extreme
loss, reclaimed-level loss and protected-structure loss. Structure penetration
is reported separately rather than silently removing non-penetrating diagnostic
candidates.

For a structure consequence, the reference must be known before D OPEN, and the
D body must actually cross that exact confirmed/protected price. A higher-TF
consequence must occur after D availability. Same-candle exact-reference breaks
are recorded explicitly; mere proximity is insufficient. Opposing intervening
structure/displacement vetoes the consequence relationship.

FVG/OB descendant relations preserve deterministic detector creation identity.
They are `CHAIN_CANDIDATE_FVG` / `CHAIN_CANDIDATE_OB`, never production setup
promotion. Their descriptive origin freshness does not retroactively decide
whether an earlier relationship existed.

## Ambiguity and chains

Every plausible sweep parent and D child is retained. Reclaim and rejection of
the same sweep do not count as independent parent ancestries. Multiple sweep
identities and multiple child displacements remain explicit ambiguities.
Each displacement has a parent count/state; each sweep/reaction has a child
count; all reclaimed/rejected events have raw candidate class counts.

`EXACT` is used only for detector/reference-proven edges. `UNAMBIGUOUS` means one
plausible ancestry, not causality proven by an experiment. `AMBIGUOUS` never
promotes a generic MSS to accepted contextual truth. Generic MSS IDs remain
generic; contextual MSS candidates are stored separately. Rejected diagnostics
carry `INVALID`, and missing plausible ancestry is `INSUFFICIENT_EVIDENCE`.

Chains are compact rooted candidate DAGs, not enumerated nearest/largest paths.
The chain ID derives from its liquidity/sweep ancestry, not outcomes or an
arbitrarily selected child. A reversal branch requires external liquidity,
reaction, D, MSS consequence and a descendant zone. A continuation branch uses
internal liquidity interaction, D, BOS consequence and a descendant zone; it
does not require external sweep. This first implementation observes exact
internal sweep interactions; touch-only continuation patterns are not asserted.
Unknown-scope equal-level roots remain `UNCLASSIFIED`.

No full chain is labelled `EXACT_CHAIN`: the reaction-to-D step is a plausible
structural relation, not explicit detector causation. Missing stages stay
`PARTIAL_CHAIN`; rejected edges never create an invented invalid/failed chain.
No child of a broken local context is admitted into that ancestry.

## Running and freezing

Use a working Python 3.12 runtime (the system `py` installation on this host has
a known mismatched standard library; the bundled Python 3.12.14 was used).

```text
python -m historical_research.run_raw_prefix_smc
python -m historical_research.verify_raw_prefix_smc
python -m historical_research.run_raw_prefix_smc --join-outcomes
python -m unittest discover -s tests -v
python -m compileall market_reviewer historical_research tests
git diff --check
```

The reconstruction writes per-origin JSON atomically and finally publishes a
FROZEN manifest. The separate outcome join requires45 frozen origins and verifies
every graph hash before opening the outcome file. It cannot change ancestry.
The full45 fresh-process verifier compares49 semantic files using a different
hash seed. Runtime measurements are separate from deterministic content.

## Initial45-origin results

Counts below are **origin-event / origin-chain memberships**, not additional
independent observations. There remain45 independent origins. Global event IDs
are also counted separately in `summary.json`.

| Raw family | Memberships |
|---|---:|
| Liquidity | 16611 |
| Exact sweeps | 20884 |
| Reclaims | 18719 |
| Rejections | 11714 |
| Displacement | 16219 |
| BOS | 6865 |
| MSS | 1923 |
| FVG | 10243 |
| OB ancestry instances | 4824 |

Liquidity/sweep totals increase because all historical confirmed references and
explicit lower-TF penetrations are included, not just current origin cursors.
OB4824 retains distinct displacement ancestry; the earlier3330 count collapsed
identical zone geometry across different causing displacements.

Exact229 original reclaimed sweep identities are reconciled at their earliest
frozen origin.185 have a raw diagnostic D in the window,159 have an expected-
direction diagnostic D,27 have a plausible candidate after all gates, and17
have a downstream structure consequence. Dispositions reconcile exactly:
1 unambiguous +26 ambiguous +202 without a plausible candidate =229.
`NO_CANDIDATE` does not mean no raw displacement exists. The old2 accepted links
are not interchangeable with27 new candidate ancestries.

Generic MSS1923; plausible D parent1290; plausible liquidity ancestry427;
unambiguous contextual MSS candidates109; ambiguous318. Nothing is promoted.

| Root family | Exact | Unambiguous candidate | Ambiguous candidate | Partial |
|---|---:|---:|---:|---:|
| Reversal | 0 | 16 | 68 | 2076 |
| Continuation | 0 | 62 | 528 | 14624 |
| Unclassified scope | 0 | 3 | 94 | 3413 |

Inventory visibility: fully115, partly1096, not visible19673 candidate DAGs.
There are2119 lower-TF D relations and1354 higher-TF structure consequences.
These remain candidates, not established chains or predictive samples.

Largest ambiguity source: multiple sweep parents (5002 candidate relation
memberships), versus multiple D children (3378), with2691 overlapping both.
913 D memberships have multiple distinct liquidity-reference parents;1027
sweeps have multiple child displacements. Do not resolve these by nearest time
or largest magnitude.

Final reconstruction runtime about154 seconds; peak5578 events and3613
accepted candidate/exact-reference relations in one origin; peak692 raw
diagnostic D comparisons for one parent. Rejected diagnostic rows are retained.
37 new tests cover boundaries, detector equivalence, candidate rejection,
ambiguity, same/lower/higher TF relationships, creation ancestry, isolation,
deterministic IDs and fresh-process behavior.

Full regression:646 tests passed. Compileall and git diff --check passed.

## Interpretation and next action

Availability improved substantially, but unambiguous causal evidence remains
sparse. Original decision inventories were intentionally minimal; their absence
of displayed evidence is not proof that raw structural events were absent.

Recommendation: **RESOLVE_PARENT_AMBIGUITY**, in research only. Preserve existing
candidate sets, distinguish competing liquidity identities and timeframe views,
and validate ancestry without outcome-driven selection. No production rule,
threshold, score or trading-readiness conclusion follows from these counts.
