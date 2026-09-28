# Public Website Snapshot v1

Schema: `grim-website-public-snapshot.v1`. Source: existing
`build_website_read_model()`. This is a public projection, not a journal backup.

## Export

```text
python -m market_reviewer.website_cli export-public
python -m market_reviewer.website_cli export-public --output artifact/website-public-snapshot.json --stale-after-seconds 1800
```

Only the destination is written, using the existing fsync + same-directory
atomic replace writer. Failures retain the prior destination and return a
normalized error with exit code 1. No sensitive error text is printed.

## Contract

- Top level: schema, generated_at, review_observation, source_observation,
  source_updated_at, stale_after_seconds, stale, runtime, symbols, evidence,
  research, execution.
- Reviewer observation may trail runtime production/research observations.
- symbols has only BTC/ETH and the approved Reviewer/structure fields.
- Market story is only the typed five-part Reviewer headline; evidence suffixes
  are not public. Unknown classifications become UNAVAILABLE.
- Draws admit only the existing Reviewer level-display grammar, with no free-form
  reason or nested metadata. Unknown new display forms require explicit review.
- Notification contains only channel names, not delivery records or destinations.
- Liquidation contains per-symbol status only.
- Research contains four counts and nullable FIRST_REVIEW/CALIBRATION booleans.
- Execution is RESERVED / connected=false. This export adds no execution endpoint.
- No paths, journals, provider payloads, secrets or environment values are copied.

## Freshness

Times are Unix seconds. generated_at is export time. source_updated_at is the
oldest of BTC/ETH Reviewer generated_at timestamps, never export time. Missing or
future source timestamps, a reviewer observation behind production, upstream stale,
or source age greater than stale_after_seconds marks stale. Default 1800 seconds
is display freshness only, not a trading/research threshold.

A hosted consumer must additionally evaluate current time against generated_at
and source_updated_at. A frozen file's stored stale=false is not perpetually fresh.
Stale never changes WATCH/ARMED/NO_TRADE or any decision state.

## Publishers and Runner Isolation

SnapshotPublisher.publish(snapshot) returns None on success and raises on failure.
FilePublisher is the only concrete adapter. HttpSnapshotPublisher is a typed,
provider-neutral contract only; no HTTP calls or credentials are required here.
Future HTTP adapters must read endpoint/auth from environment, use HTTPS and
bounded timeouts, reject redirects/non-2xx, and avoid retries and secret logging.

publish_public_snapshot validates the closed public shape before sending and
passes a copy to the publisher. All ordinary publisher/build failures are contained
and return PUBLISH_FAILED, without paths, response bodies or exception messages.

This release deliberately does not hook or restart the running Observation Runner.
Export is explicit/manual, not automatic per observation. A future integration may
invoke export_public_snapshot only after formal COMPLETE, outside the observation
transaction. Publication must never determine transaction success or roll back
Production/Research. No cadence, recovery, decision or accumulation code is changed.
