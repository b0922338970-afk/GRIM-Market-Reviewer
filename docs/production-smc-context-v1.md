# Production SMC Context Phase 1

The official `review_snapshot` output adds `smc_context` and
`opportunity_alert`. Existing review keys and `review-state.v2` are unchanged.
The low-level decision engine remains unchanged. Enrichment runs once per
symbol after native sequential replay, not at each replay checkpoint.
The observation runner already retains this review output in its packet;
cadence, recovery, origin eligibility and research accumulation are unchanged.

## Contracts

`production-smc-context.v1` exposes active_draw, dealing_range,
premium_discount, liquidity_type, liquidity_reaction, displacement_quality,
contextual_mss, contextual_bos, fvg_state, bpr_state, ob_state, breaker_state,
irl, erl, remaining_opportunity, defense_state and fragility_flags.
Evidence envelopes contain availability, value and reason. Missing evidence
is UNAVAILABLE, distinct from an observed NONE classification. Numeric dealing
range bounds are currently not exposed and remain UNAVAILABLE. IRL/ERL list
existing explicitly classified liquidity references, without a new detector.
Selected zone defense observations retain their original provenance.

The adapter reuses live Hybrid SMC production, raw-prefix, cluster and zone
research engines. Its synthetic output identity is not an independent origin.
No original origin or historical dataset is updated. Closed boundaries are
per timeframe; generated_at is the final M5 open plus 300 seconds, never wall
clock. Failures are explanation unavailability and never decision vetoes.

`opportunity_alert.v1` contains symbol, timeframe (M5 review checkpoint),
direction, review_state, quality_context, market_story, entry_zone,
invalidation, target, remaining_room, fragility and generated_at.
Entry_zone comes only from the existing Production locked setup. Research
FVG/BPR/OB/Breaker candidates cannot become executable entry zones.
Invalidation preserves existing Production structural descriptions, not a
fabricated numerical stop. All packets are PACKET_ONLY; nothing is sent.

| Production state | alert_level | emit_alert | execution_ready |
| --- | --- | --- | --- |
| NO_TRADE | SILENT | false | false |
| WAIT | SILENT | false | false |
| WATCH | NORMAL_ALERT | true | false |
| ARMED | HIGH_PRIORITY_ALERT | true | true |

Execution readiness mirrors existing Production truth; context cannot upgrade
or downgrade it. No score, weights, thresholds, orders or position sizing.

Synthetic test examples (not live observations): BTC WAIT -> SILENT;
ETH WATCH -> NORMAL_ALERT. Both use checkpoint 1728000000. A fixture with
ARMED maps to HIGH_PRIORITY_ALERT without altering eligibility rules.
