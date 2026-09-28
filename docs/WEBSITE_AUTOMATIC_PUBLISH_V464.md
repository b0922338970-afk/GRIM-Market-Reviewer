# V4.6.4 Post-COMPLETE Publishing

Integration: _execute_ready_cycle, strictly after the COMPLETE journal write;
recovery paths only when they newly persist COMPLETE. Existing COMPLETE history,
watermark-only repair, WAITING/BLOCKED/dry-run paths are not publication triggers.
Legacy recovery without a persisted COMPLETE boundary cannot publish.

The hook verifies the persisted transaction and production head observation and
checkpoint, Production/Research hashes, and M5 open + 300 boundary. It builds the
existing website read model using RunnerConfig paths, then verifies source,
production and research observations all match the just-completed number. Both
Reviewer timestamps must equal the completed checkpoint. It rechecks captured
journal/head/state/research bytes before using that frozen model with the existing
Supabase publisher; the publisher does not reread the filesystem.

This rejects N-1/N+1 and inconsistent reads without guessing. An independent
concurrent publisher is not coordinated by this hook: remote CAS/ordering is not
claimed. The existing Runner single-process/transaction controls are unchanged.

Configuration is unchanged:
- GRIM_WEBSITE_SUPABASE_URL
- GRIM_WEBSITE_SUPABASE_SERVICE_ROLE_KEY

Absent configuration -> NOT_CONFIGURED. Invalid configuration -> PUBLISH_FAILED.
Source mismatch -> SKIPPED_SOURCE_MISMATCH; no verified COMPLETE ->
SKIPPED_NOT_COMPLETE. Other errors are normalized and contained. One attempt only,
existing 10-second transport timeout, no retry or later historical catch-up.
A crash before this best-effort hook may leave a missed website publication; the
next successfully completed observation publishes forward. Exactly-once delivery
across process crashes is not promised.

Only a safe JSON entry is appended to observation-runner.log:
website_snapshot_publish, observation, reason. No snapshot, keys, paths, provider
responses, stack traces or environment dumps. Even import/hook/log failures do not
escape to observation orchestration. No publish fields are added to canonical
payload/head/journal, Research lifecycle, or the existing observation result.
Cadence calculations and transaction/recovery semantics are unchanged. The bounded
synchronous network attempt may add transport latency after completion.

The public schema/freshness policy stays unchanged (1800 seconds). Supabase upsert
now explicitly supplies updated_at from generated_at in UTC, independent of whether
the deployed table has an update trigger. Stale never changes Reviewer state.
The manual publish CLI remains available and shares the same publisher.

## Deployment boundary

Editing Python does not hot-reload an already-running Runner. This change does not
stop/restart it or alter real runtime data. It takes effect when the Runner next
loads this code through a separately authorized normal startup. Its process must
also inherit the required environment variables. No live publish/backfill is run
as part of implementation/testing.
