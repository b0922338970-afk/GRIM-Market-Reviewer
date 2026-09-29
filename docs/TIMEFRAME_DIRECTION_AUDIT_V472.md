# Multi-timeframe direction audit

`python -m market_reviewer.cli timeframe-direction-audit [--json]` reads existing
COMPLETE journals and origin tracker snapshots. It never fetches, replays,
repairs or writes runtime data. Source overrides follow research-side-diagnostics.

Reviewer _swing_bias combines D1 and H4: aligned directional states win; one
directional state plus RANGE uses that direction; otherwise NONE. Reviewer
Preferred_Direction preserves NONE. Research _direction_from_review instead
maps BEARISH to SHORT and everything else to LONG. The diagnostic calls this
existing function and displays its exact source/line rather than changing it.
NONE is counted independently from BULLISH regardless of the routed direction.

Direction authorities and propagation:
- reviewer._swing_bias / review_symbol: D1/H4 -> Swing_Bias -> Preferred_Direction.
- missed_opportunity_live._direction_from_review: Swing_Bias -> research identity.
- build_tracker_candidate_from_observation: M5 timestamp/price, combined evidence.
- production_smc_context.enrich_review: copies Preferred_Direction into alert;
  timeframe M5 is a literal output label, not a separate direction decision.
- live_hybrid_smc.build_live_hybrid_smc and capture_live_smc_context: consume/freeze
  the origin direction after origin creation. They do not authorize SHORT.
- website projections and notification delivery copy alert direction.

The code has HTF (D1/H4), tactical (H1/M15/M5), trigger (M15/M5) groups. It does
not have four independently directional macro/swing/tactical/execution layers.
H1 bearish against D1/H4 bullish can change phase to PULLBACK, not Swing Bias.
Selected displacement and contextual trigger MSS must align with Swing Bias.
No rule is added to reinterpret this design or to promote lower-TF evidence.

Bias counts use retained symbol-observation occurrences. Full window includes
tracker snapshots where early journals are unavailable; absent original bias
stays UNAVAILABLE. 24H/72H use the latest archived M5 open time, not wall clock.
Tactical counts inspect exported Last_MSS/Last_BOS H1 strings, tactical FVG/OB,
selected displacement and liquidity events. Structured event timestamps must
satisfy their own timeframe close boundary. Selected displacement output lacks
timeframe and is reported as exposed text only, not a raw event-universe count.
Sweeps/reclaims do not imply an unexposed rejection or valid short setup.

Presence counts repeat an event across observations; they are not independent
samples. Truncated inventory and historical unavailable reviews are explicit
limitations. Invalidated/mitigated bearish zones may still be context, never
trade eligibility. Conflicting exposed reviews and concurrent replacement fail
closed. The mixed-cause assessment identifies existing routing/default and
timeframe-role limitations, not proof of missed profitable SHORTs. Contract
review is recommended before any separately authorized semantic patch.
