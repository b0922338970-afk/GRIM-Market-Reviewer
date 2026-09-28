# V4.6.3 Hosted Read Website

Browser -> GET /api/snapshot -> Supabase REST latest row. No local filesystem,
Runner, research engine, notification delivery or exchange integration is used.
No deployment is performed by this implementation.

## Configuration for future deployment

Use repository root, framework Other, Node 24, and the checked-in vercel.json.
The output directory is web; / serves index.html, /app.js and /styles.css remain
static assets. api/snapshot.js is a Node function, not a static file. No SPA
catch-all rewrite intercepts /api/snapshot. No npm dependencies are required.
.vercelignore excludes everything except explicitly allowed website/function files.
Production/runtime/research data must never be uploaded as web assets.

Set these ONLY as server-side Vercel environment variables:

- GRIM_PUBLIC_SUPABASE_URL: HTTPS project origin under supabase.co.
- GRIM_PUBLIC_SUPABASE_PUBLISHABLE_KEY: publishable key or legacy anon JWT.

Do not use GRIM_WEBSITE_SUPABASE_SERVICE_ROLE_KEY in this deployment. Secret keys,
service_role JWTs, malformed/unknown keys and non-anon JWT roles fail closed before
fetch. JWT parsing is only a misuse guard; Supabase still verifies signatures/RLS.
No credentials are hardcoded or sent to the browser.

New sb_publishable_ keys use apikey only, because they are not JWTs. Legacy anon
JWTs use apikey plus Authorization: Bearer. This intentional header distinction
follows [Supabase API-key documentation](https://supabase.com/docs/guides/getting-started/api-keys).
Node function/config follow [Vercel Node](https://vercel.com/docs/functions/runtimes/node-js)
and [project configuration](https://vercel.com/docs/project-configuration/vercel-json).

## Response and freshness

GET only, Cache-Control: no-store on success and failure. Upstream GET is bounded
by a five-second abort timeout and 128 KiB body limit; no redirects or retries.
Missing/malformed rows, metadata mismatch, unsafe snapshot fields and upstream
failures return HTTP 503 with only status=UNAVAILABLE. Other methods return 405.
No upstream error text, credential, stack trace or journal is forwarded.

Success returns status=OK, snapshot, source_observation, generated_at, updated_at,
and request-time stale. The public v1 wire contract is validated before forwarding;
unknown schema fields fail closed until explicitly supported. No market meaning is
recomputed. Tests check compatibility against the Python public exporter.

stale is true when request time exceeds generated_at + stale_after_seconds. It
also preserves upstream stale and detects missing/old/future source timestamps.
The stored snapshot and Reviewer states are unchanged. updated_at is optional
validated timestamp metadata, not the freshness authority.

## Shared frontend

Loopback hosts default LOCAL (/api/status). Other hosts default REMOTE
(/api/snapshot). A pre-script window.GRIM_WEBSITE_MODE = 'LOCAL' or 'REMOTE'
can override for LAN/testing; ?mode=remote is a loopback remote smoke-test option.
No credentials belong in frontend config. Both modes use one renderer and the
existing presentation helpers; only the transport envelope is adapted.

DATA STALE / 資料逾時 appears at the top without changing WATCH/ARMED/etc.
REMOTE DATA UNAVAILABLE retains last known Reviewer fields, including tab switches,
and never invents a market state. Execution stays RESERVED / connected=false.
The existing Python local serve command is unchanged.

## Verification

node --test tests/website_hosted.test.cjs
node tests/website_presentation.test.cjs
python -m unittest tests.test_website_hosted_v463 tests.test_website_public_snapshot tests.test_website_read_model_v45 tests.test_website_presentation_v452 -v

Mocked REST tests do not certify a live Vercel deployment or Supabase credentials.
Do not deploy or push until separately authorized.
