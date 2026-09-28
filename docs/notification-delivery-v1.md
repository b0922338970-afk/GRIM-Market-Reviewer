# V4.4 Notification Delivery Phase 2

`notification-delivery.v1` is an output-only, best-effort router. It consumes
only `opportunity_alert.v1`; it never recalculates a market decision. No order,
exchange, sizing or TRIGGER_READY behavior is introduced.

All channels are disabled unless their ENABLED variable is exactly `true`
(case insensitive). Configuration is read only from the process environment.
No environment file, token, endpoint URL, recipient, message body or provider
response body is stored in the delivery journal.

| Channel | Environment variables (prefix GRIM_NOTIFY_) |
| --- | --- |
| Telegram | TELEGRAM_ENABLED, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID |
| Discord | DISCORD_ENABLED, DISCORD_URL |
| LINE | LINE_ENABLED, LINE_TOKEN, LINE_TO |
| Generic webhook | WEBHOOK_ENABLED, WEBHOOK_URL, optional WEBHOOK_TOKEN |

Endpoints must be HTTPS; redirects are refused. Webhook TOKEN is optional
Bearer authorization. Telegram uses sendMessage; LINE uses push messages;
Discord disables mentions and requests wait=true. Requests use a five-second
socket timeout and bounded response reads. No automatic retries, including
429 or 5xx. Oversized text is rejected with MESSAGE_TOO_LONG, never silently
truncated or split into partially delivered messages.

## Safety and persistence

NO_TRADE and WAIT are always SILENT, even if packet priority is malformed.
WATCH must have NORMAL_ALERT and ARMED HIGH_PRIORITY_ALERT. Inconsistent
packets are rejected, never corrected. Rendering preserves evidence values,
including UNAVAILABLE. Generic webhook includes an unchanged copy of the packet.

Runner delivery happens only after the normal Production/Research transaction
is COMPLETE. Recovery and cadence logic are unchanged. Notifications are not
replayed by recovery; a crash between COMPLETE and dispatch may lose delivery.
There is no guarantee of exactly-once receipt by an external provider.

Journal: `<runner output dir>/notification-delivery.json`. The journal stores
only schema and rows: alert_identity, channel, attempted_at, status,
response_code, normalized error, delivered_at. Identity is SHA256 of canonical
complete packet JSON, including symbol, generated_at, state, priority and any
setup/sequence identifiers carried by context. Each channel deduplicates
independently. Any previously attempted packet is suppressed, including failed
or uncertain attempts. Skipped disabled/unconfigured packets may later be sent.

An exclusive sibling .lock prevents concurrent sends. ATTEMPTED is atomically
persisted before HTTP. Crash-uncertain ATTEMPTED remains suppressed. A stale
lock is not automatically stolen; an operator must first verify no sender is
active before removing it. Corrupt/unwritable journals block sending, never
Production. Protect the journal from deletion to retain dedupe history.
The journal contains delivery metadata, not Production or Research state.

`py -m market_reviewer.cli notification-status` reads configuration and journal
without sending. `--path` selects another journal. It reports enabled channels,
last successful delivery, failures, skips, duplicates and uncertain attempts.

## Synthetic message example

```text
[GRIM]
Symbol / TF: BTC / M5
State: WATCH
Direction: LONG
Priority: NORMAL_ALERT
Market Story: Closed-candle context
HTF Draw: UNAVAILABLE
Liquidity Reaction: UNAVAILABLE
Displacement: UNAVAILABLE
Contextual MSS: UNAVAILABLE
Contextual BOS: UNAVAILABLE
FVG: UNAVAILABLE
BPR: UNAVAILABLE
OB: UNAVAILABLE
Breaker: UNAVAILABLE
Entry Zone: UNAVAILABLE
Invalidation: UNAVAILABLE
Target: UNAVAILABLE
Remaining Opportunity: UNAVAILABLE
Fragility: []
Generated At: 1787974800
Notification only. No order has been placed.
```

An ARMED packet retains its own unchanged context and displays State: ARMED
and Priority: HIGH PRIORITY. Neither message is an instruction to buy/sell.

API contracts: [Telegram](https://core.telegram.org/bots/api#sendmessage),
[Discord](https://docs.discord.com/developers/resources/webhook#execute-webhook),
[LINE](https://developers.line.biz/en/reference/messaging-api/#send-push-message).
Validation uses only fake transports; no live credentials or sends required.
