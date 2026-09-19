# Live SMC frozen context

Schema: `live-smc-frozen-context.v1`.

Capture runs only after the existing live research upsert creates a new origin.
The payload lives at `records[].snapshots[0].live_smc_frozen_context` in the
missed-opportunity store, using existing atomic research persistence. Existing
records and later snapshots are never enriched. No genesis, Production decision,
runner schedule or commit/recovery sequence changes.

`frozen_at` / `available_at` denote the origin checkpoint (M5 open + 300), not
wall-clock execution. Capture requires the latest closed M5 identity to match the
origin. Hybrid source events must already be available at this checkpoint.

The aligned review supplies phase/regime/HTF. Full capture consumes an existing
decision-time `opportunity_snapshot.smc_state` using `smc-hybrid-outcome.v1` plus
the complete matching context. It deep-copies ancestry and descriptive fields;
it never infers Hybrid MSS, BPR or Breaker from similar Production labels.

The live research path now invokes live-hybrid-smc-payload.v1 after creation and
before capture. It uses the existing raw-prefix, single/cluster, Hybrid and zone
engines plus the unchanged archival decision-context extractor. Missing inputs or
engine errors remain UNAVAILABLE and are recorded in the frozen producer report.
They are never filled later. The two legacy origins receive no payload.

When complete source fields are supplied at origin creation, smc-sample-status
automatically recognizes the wrapper and validates its embedded frozen state.
Pending outcomes do not prevent classification; outcome readiness is separate.
No outcome is used to select membership or ancestry.

The patch does not stop/restart any running Python process. A process already
holding old imported modules must load the new code through its normal deployment
procedure before capture takes effect; no restart is performed by this task.
