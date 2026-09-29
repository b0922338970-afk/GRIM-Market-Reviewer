# Research direction correction

V4.7.3 aligns research routing with Reviewer direction: BULLISH -> LONG,
BEARISH -> SHORT, all other/missing values -> NONE. Existing case normalization
is retained. Reviewer, Swing Bias production and weighted eligibility are unchanged.

The live symbol integration returns NO_DIRECTION_CANDIDATE before candidate
construction, eligibility, identity matching, append, conversion or freeze for
NONE. This is not a third trade direction. Normal outer observation bookkeeping
and existing horizon updates continue unchanged. No manual migration or replay
is performed. Historical 45 and existing live identities/snapshots are untouched.

Diagnostics now explicitly display CORRECTED_DIAGNOSTIC_VIEW. Candidate totals
use explicit bullish/bearish bias, with neutral non-candidates reported separately.
Origin counts and maturity retain their persisted authority and are not
counterfactually reclassified. Ratios involving these two authorities are
descriptive, not a claim that corrected historical routing actually ran.
The read-only legacy_origin_audit uses only creation-observation snapshots,
never later context. A stored LONG with creation NONE is labeled
LEGACY_DIRECTION_ROUTING_ARTIFACT in the report only; V4.7 gate is not changed.

The V4.7.1/V4.7.2 documents describe the original pre-correction contract.
Their diagnostic commands now expose the corrected view plus that legacy
contract, not a rewrite of persisted historical behavior. Tactical direction
permissions are unchanged: this fix does not authorize counter-HTF SHORTs.

Only observations processed by code loaded with this change use the new routing.
An already running Python process may retain the old imported function until
its normal separately authorized restart. This change does not restart it,
publish, deploy, backfill, or mutate runtime data.
