# Supabase Hosted Read Store

Manual CLI plus the V4.6.4 post-COMPLETE hook. No RLS/schema migrations, retries, or execution API.
The existing public snapshot validator runs before sending any data.

## Configuration

Inject these environment variables in the local publisher process:

- GRIM_WEBSITE_SUPABASE_URL: HTTPS project origin, no credentials/path/query/fragment.
- GRIM_WEBSITE_SUPABASE_SERVICE_ROLE_KEY: privileged server-side credential.

Do not put credentials into source files, shell history, snapshots, frontend env
variables, screenshots, or logs. The adapter reads them at invocation; status only
prints booleans. No environment values are persisted by this implementation.

## Commands

```text
python -m market_reviewer.website_cli supabase-status
python -m market_reviewer.website_cli publish-supabase
```

Status performs no network access or writes. ready means configuration presence
and URL/header validation, not verified database authorization. Publish builds a
fresh public projection and makes exactly one HTTPS POST with a 10-second socket
timeout. Redirects and implicit environment proxies are disabled. No response body
is read or logged. HTTP errors return PUBLISH_FAILED with a numeric http_status;
other failures use normalized codes. Exit code is 0 on success, 1 on failure.

Destination: public.grim_website_snapshot; conflict key id; id always latest.
Upsert columns: id, schema, generated_at, source_observation, stale_after_seconds,
snapshot, updated_at. updated_at is the UTC timestamp corresponding to the public
snapshot generated_at; no database update trigger is required. Consumers use
generated_at/source time for freshness.
Concurrent/manual writes are last-write-wins; no remote monotonic CAS is claimed.

Legacy service_role JWT is sent in apikey and Authorization Bearer. A new
sb_secret_ key, if supplied through the same env name, is sent only in apikey.
See [Supabase API keys](https://supabase.com/docs/guides/getting-started/api-keys).
Upsert follows [PostgREST upsert](https://docs.postgrest.org/en/v13/references/api/tables_views.html#upsert)
with resolution=merge-duplicates,return=minimal and on_conflict=id.

## Future Website Read Contract

remote_read_url(project_url) constructs this GET URL without network access:

```text
/rest/v1/grim_website_snapshot?id=eq.latest&select=snapshot,generated_at,source_observation,stale_after_seconds,updated_at
```

Use a public publishable/anon key for the read client, NEVER the service-role key.
The configured SELECT-only policies remain unchanged. REST returns an array:
empty means unavailable; otherwise consume the latest row's snapshot. Recompute
staleness using current time, generated_at/source_updated_at and stale_after_seconds.
Do not map stale to NO_TRADE. No local paths or raw journals belong in this store.

## Validation Boundary

Tests use fake HTTP transport and synthetic credentials only. Passing them verifies
request construction, isolation, error handling and redaction, not live RLS/table
access. Live publication is an explicit operator action after config validation.
