# Live side diagnostics

Run `python -m market_reviewer.cli research-side-diagnostics` or add `--json`.
Inputs may be selected with `--journal`, `--live-store`, `--root` and
`--historical-outcomes`. The command does not write an artifact or update data.

The source universe is COMPLETE live observation journal transactions plus
persisted live tracker snapshots for earlier observations not retained there.
Keys are observation number + symbol. Duplicate identical rows count once;
conflicting rows are excluded and disclosed. Historical origins are excluded
using the unchanged V4.7 gate's verified live-origin identities. Missing or
invalid gate sources fail diagnostics closed. Concurrent source replacement
also fails closed. No recovery/status mutator is invoked.

Candidate counts are routed symbol-observation occurrences, NOT independent
origins and NOT counts of actionable market signals. The existing research
direction function routes BEARISH to SHORT and all other explicit Swing_Bias
values, including NONE, to LONG. Missing review bias is not guessed. Earlier
tracker-only entries retain their recorded direction, not a reconstructed bias.
Bias composition is reported to avoid mistaking routed LONGs for bullish views.

Only stored tracker snapshot opportunity_evidence is used for the four domains.
The journal does not retain the enriched research candidate for every rejected
observation. We do not substitute current external data or invent historical
price enrichment. Each funnel measure includes known and total denominators;
incomplete totals are UNAVAILABLE. Weighted scores describe only the persisted,
survivor-selected subset. They are never used to change eligibility or the gate.
Threshold qualification reuses is_eligible_origin with its non-score guards
neutralized; actual research eligibility applies the unchanged guards only
when their inputs are known. These are current predicates on archived evidence,
not an assertion of a past version's decision.

Reported rejection predicates are non-exclusive, supported failed conditions
from the existing eligibility function. Percentages use all archived non-origin
candidates of that side/window. They are NOT fabricated recorded rejection
messages. The formal rejection report is UNAVAILABLE. Individual non-positive
domains are not mandatory gates. Existing tracker/cooldown effects are not
inferred just because no new origin appeared. Production legal-genesis
eligibility remains UNAVAILABLE; an observed transition is not a full genesis
eligibility trace and research eligibility is a separate concept.

Origin/classifiable/complete counts are independent-origin cohorts created in
the window, evaluated using current persisted frozen context and outcomes.
They are not as-of historical maturity claims. Legacy unclassifiable origins
never contribute to complete counts. 24H and 72H are inclusive, anchored to the
latest archived M5 open timestamp (not wall clock), so repeated reads are stable.
Conversion rates have archived candidates as denominator, not trade win rates.

Assessment remains INSUFFICIENT_EVIDENCE_TO_DIAGNOSE without complete rejected
candidate enrichment, recorded rejection traces and matched directional
controls. Neither a sample-count skew nor the existing neutral-to-LONG default
alone demonstrates market imbalance or a directional pipeline defect.
No website integration, rule change, backfill, historical expansion or Runner
restart is involved.
