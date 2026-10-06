# V4.7.6.6.1 Shadow sequence invalidation metric reconciliation

Baseline: 922bc8b80102a69cbac328456854de33c10f0107.

## Counter trace

- tactical_shadow_capture.live_status: legacy invalidated counts every durable
  transition with to_state INVALIDATED. It does not count current invalidated
  heads or deduplicate sequence IDs.
- tactical_sequence_acceptance_audit: legacy invalidations.total counts distinct
  sequences with an invalidation, using each sequence's first invalidation.
  Its reasons/categories retain that first-invalidation-per-sequence scope.
- Explicit sequence_metrics now use one pure telemetry helper over the validated
  records supplied by each caller. They retain all invalidation transitions and
  separately count distinct symbol/sequence_id pairs.
- Old fields retain their behavior and carry explicit alias semantics.
  Unavailable live ledger data yields sequence_metrics = null.

## Fixed durable cohort #301-#419

119 receipt records; read-only validated replay passed.

| Metric | Count |
| --- | ---: |
| sequence_started (FORMING transitions) | 18 |
| unique_sequence_ids | 18 |
| invalidated_unique_sequences | 16 |
| invalidation_transitions | 16 |
| withdrawn_transitions | 9 |
| reversal_transitions | 7 |
| other_invalidation_transitions | 0 |
| still_active_sequences | 2 |
| current_invalidated_sequences (final per-symbol heads) | 0 |

Conservation: 18 = 16 + 2; 16 = 9 + 7 + 0. Each invalidated
sequence has exactly one invalidation transition. These are diagnostic checks,
not capture/validation gates or new lifecycle rules.

Cohort SHA256:
4a7c4a3c065a58836474d0186b1b8a64db0a2934c3b9473a2f911dc084fd2ca8.

## The reported 11

The original live-status implementation reproduces 16 for this cohort, not 11.
The existing saved artifact/tactical-sequence-live-status.json also records
latest_observation 419 and invalidated 16.

11 matches the earlier cumulative prefix through observations 368/369.
That is a possible scope/staleness explanation, not proof of the original
reported value's source. No diagnostic omission was found for #301-#419.
The audit's expected_invalidations = 11 is retained as the original reported
baseline, explicitly labeled as not a lifecycle invariant.

During verification the current live ledger had advanced to #425 and live
invalidated was 18. Default live status covers its whole durable ledger;
acceptance audit remains fixed to #301-#419. Both now expose first/latest
observation, record_count and cohort fingerprint. Compare equal cohorts only.

## Safety and verification

- No lifecycle, target, direction, market, MSS, setup, eligibility, origin,
  Production or Research semantics changed.
- No ledger rewrites, Runner restart, backfill or runtime writes performed.
- 128 source files (durable ledger, frozen inputs, Production and Research
  canonical files) had identical SHA256 before/after the read-only audit/status.
- Capture gate remained true; sequence validation remained false.
- Nine new metric tests passed, including fresh-process determinism,
  separate unique/transition counts, reason decomposition, cohort identity,
  read-only shared status/audit source, and unchanged gates.
- Related V4.7.6.6, shadow, provenance, research, direction and Runner regression:
  403 tests, 402 passed, one optional archive skip.
- compileall and git diff --check passed.
