# V4.4 Phase 2.1 Telegram activation

The Telegram adapter remains disabled by default. The application reads only
the current process environment; no credential files, command-line credentials,
registry lookup, token printout or automatic activation are implemented.

## Environment and preflight

- GRIM_NOTIFY_TELEGRAM_ENABLED=true
- GRIM_NOTIFY_TELEGRAM_TOKEN: Bot API token from your secret manager
- GRIM_NOTIFY_TELEGRAM_CHAT_ID: numeric chat ID or @channel_username
- GRIM_NOTIFY_DISCORD_ENABLED=false (or unset)
- GRIM_NOTIFY_LINE_ENABLED=false (or unset)
- GRIM_NOTIFY_WEBHOOK_ENABLED=false (or unset)

Inject the token and chat ID securely into the intended process environment.
Do not paste them into commands, reports, chat, tracked files or shell history.
Run `py -m market_reviewer.cli telegram-status` before activation. It reports
presence/format only, never credential values. `telegram_only_ready=true`
requires enabled Telegram, locally valid credentials and no other enabled
channels. It is not a provider credential/authentication validation.
The normal Telegram adapter also checks credential format before HTTP.

`py -m market_reviewer.cli notification-status` includes the same redacted
Telegram config, enabled channels and delivery journal counts. Neither status
command connects to Telegram or writes a journal.

## Explicit synthetic send

```powershell
py -m market_reviewer.cli telegram-test-send --test-id activation-001
py -m market_reviewer.cli telegram-test-send --test-id activation-001
py -m market_reviewer.cli notification-status
```

The first command attempts one synthetic message only if preflight passes.
The second uses the same identity and is suppressed, even after timeout or
failure. A new explicit ID authorizes a separate attempt; there are no automatic
retries. IDs are bounded alphanumeric/hyphen/underscore labels, not free text.
An unconfigured command returns BLOCKED_CONFIG with exit code 1 and no send.
DELIVERED or DUPLICATE_SUPPRESSED returns 0; all other results return 1.

The message begins `[GRIM] SYNTHETIC TEST / NON-ACTIONABLE` and says it is not
a live market review, opportunity or trade alert. It has no state, entry or
direction instruction. It is NOT an opportunity_alert.v1 packet. The regular
router rejects synthetic schemas; WATCH/ARMED can still enter that router only
through the existing opportunity packet contract.

Tests and normal notifications share the existing atomic journal, exclusive
lock, pre-send ATTEMPTED reservation and no-retry policy. Synthetic identities
use a separate TELEGRAM_TEST_ namespace, stable across time. Only Telegram is
attempted on this path. Credentials, chat ID, URL and message are never saved
to the journal. `--path` on either CLI selects the journal; retain the same
path when checking deduplication.

Timeout, HTTP errors, Telegram ok=false and journal failures remain fail-open
to Production/Research. No Runner decision/cadence/recovery, Reviewer, SMC,
alert packet or execution changes. Actual provider delivery is not claimed
until the journal records DELIVERED for a successful Telegram response.

## Running process boundary

An already-running Runner retains its inherited environment and imported code.
Changing a terminal's environment does not reconfigure that existing process.
After a successful explicit test, use the normal approved Runner launch/change
procedure with this environment when ready. These commands do not stop, restart
or reschedule the Runner. The existing post-COMPLETE notification hook handles
future real opportunity packets; no old packets are replayed by activation.

Example without credentials:

```json
{"enabled_channels": [], "telegram_config": {
  "enabled": false, "token_present": false, "chat_id_present": false,
  "credentials_valid": false, "telegram_only_ready": false,
  "validation_scope": "LOCAL_FORMAT_ONLY", "live_credentials_verified": false
}}
```
