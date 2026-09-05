# V4.3 Historical Opportunity Replay Foundation

Historical Temporal Replay is the research sample expansion line. LIVE samples
remain forward validation. No score, model training, execution, threshold tuning,
live schema migration, or automatic historical network fetching is introduced.

## Architecture and isolation

- `historical_data.py`: offline market-data.v1 import, content-addressed cache,
  per-timeframe closed-only views, optional point-in-time external archive.
- `historical_replay.py`: isolated in-memory Reviewer lifecycle, Opportunity
  extraction, shared research episode rules, freeze, outcomes, windows and summary.
- `cli.py`: historical-only subcommands. Existing live branches are unchanged.

Default output: `research/historical-replay/`.
Default cache: `artifact/historical-market-data/`.
No writes to live thesis, trackers, head, journal, runner or liquidation stores.
Alternate empty caller-owned directories are supported. Project roots, live
subtrees and paths escaping the historical root are rejected. Runtime JSON files
and large candle datasets are not committed.

Phase 1 imports an existing market-data.v1 file using `--input`. It requires all
five timeframes, at least 50 closed candles per timeframe at the first checkpoint,
one provider per symbol, finite OHLCV, and contiguous closed-candle histories.
No fallback to live providers or live state is performed. Unsupported/missing
history fails closed. Later historical provider adapters can populate the same
cache contract without changing replay decisions.

## Time and no hindsight

Start/end are UTC Unix timestamps or offset-aware ISO timestamps, aligned to
300 seconds. They identify availability/close checkpoints, not candle open times.
At T a candle is visible only when open + timeframe duration <= T. No current-open
candle object is passed to Reviewer, features, or episode generation. Higher TFs
use their own duration. Synthetic frame fetch time is T, and generation IDs hash
only the visible prefix. Raw archive identities stay in provenance, outside
decision-time feature input.

The first checkpoint cold-starts an isolated lifecycle from prior closed context.
Every intervening M5 is reviewed, even with the default 60-minute observation step.
The final endpoint is emitted if it is off the observation cadence. Maximum 2016
internal M5 checkpoints per window prevents accidental multi-year work. Dry mode
defaults to two observations, accepts 1..24, and writes nothing.

Each observation records explicit HISTORICAL_REPLAY source, deterministic ID,
symbol, checkpoint, available_at, per-TF data cutoff, visible source provenance,
Reviewer result, unchanged feature semantics and external evidence. Observation
IDs include the visible review context: different cold-start histories can produce
different IDs at the same checkpoint.

Optional external archive schema: `historical-external-evidence.v1`, with `records`
containing existing ExternalMetric mappings plus symbol. A metric must have
integer source_timestamp and available_at with 0 <= source <= available <= T.
Latest eligible availability is selected; future or undated records are omitted.
Existing external classifiers are reused. Missing raw metrics are UNAVAILABLE;
missing domain evidence is DATA_UNAVAILABLE, never negative or manufactured zero.
Historical availability of candles is modeled as their scheduled close; exchange
publication delays/revisions are not available in OHLCV fixtures. This is a stated
Phase 1 data limitation, not measured delivery-time provenance.

## Episodes and outcomes

Live research genesis, eligibility, trajectory, break and conversion logic is
reused through the in-memory symbol application function. No live store is loaded.
One continuing trajectory stays one episode. IDs have a HIST- namespace and
preserve symbol, direction, origin open timestamp and live context identity.
Conversion is a historical simulation equivalent; it never changes production.

Episodes originating at the initial cold-start boundary carry
`origin_left_censored = true`: their true pre-window genesis may be unknown.
Expand verified warmup/windows before treating these as fully observed genesis
samples. End-of-window does not automatically close an OPEN episode.

Decision observations, episode origins/lifecycle and snapshots are hashed and
frozen before any outcome pass. The separate outcome pass reuses the live horizon
engine for 1H/4H/12H/24H and verifies the decision digest before and after.
Measurements use origin_snapshot_timestamp (M5 open identity, distinct from
origin_checkpoint = open + 300):
`origin < candle.timestamp <= horizon_end`.
Only source-declared closed candles enter outcomes. Missing future coverage stays
PENDING/DATA_GAP. COMPLETE outcomes remain immutable on subsequent shorter inputs.
No future outcomes are fed back into decisions, and no win rate is inferred.

## Cache and manifest

Cache objects are `<sha256>.json` with schema historical-market-cache.v1,
sample source, provider/source/market/timeframe metadata, bounds, counts, checksum,
and source market_data. `BTC-index.json` / `ETH-index.json` point to cached content.
Repeated imports reuse the same object. No repeated provider fetching occurs.

`historical/windows.initial.json` is an unselected BTC/ETH sampling queue across
all requested regime categories. Dates and analysis labels are deliberately null.
The JSON schema is `historical/window-manifest.schema.json`.
To execute a window, provide a verified source, start/end, sampling reason and
status READY. Relative source paths resolve against the manifest directory.
The runtime validates the required execution fields; external JSON-schema tools
may additionally validate the published schema.

Retrospective regimes live exclusively under ANALYSIS_LABEL/OUTCOME_CONTEXT.
They never enter the decision engine or feature input. No speculative periods or
regime claims are supplied. No year is hard-coded into the engine.

Windows process sequentially. Each COMPLETE window is atomically stored as one
document with its specification and frozen digest. Identical reruns return
SKIPPED_COMPLETED without replay. Changed inputs/config under the same window ID
fail closed. A crash before commit can safely recompute the window; resume is
window-granular, not intra-window. Keep output directories single-writer.

Summary deduplicates identical episode IDs and shared same-context/price trajectory
snapshots across overlapping cold starts, retaining alias provenance. Canonical
record is the earliest sorted window/origin; alternative-origin outcomes are not
pooled. Raw per-window records stay available for audit. Ambiguous same-direction
temporal overlaps without shared identical evidence are reported as
UNRESOLVED_OVERLAP_NOT_COUNTED and excluded from the independent episode count
until their boundaries are reviewed. Counts describe research episodes, not
statistically proven independence.

Maturity includes observation and unique checkpoint counts, episodes by symbol,
direction and analysis regime, lifecycle completion separately from outcome
completion, four-horizon coverage, and domain availability rates. Historical
counts are always separate. An explicit read-only live-store input adds LIVE and
TOTAL counts; without it LIVE/TOTAL are null, not guessed as zero. Summary labels
the role of live data FORWARD_VALIDATION without editing existing live records.

## CLI

Bounded read-only fixture replay (substitute checkpoints with verified coverage):

```powershell
py -m market_reviewer.cli historical-replay --symbol BTC --start <UTC-checkpoint> --end <UTC-checkpoint> --input <market-data.v1.json> --dry-run
py -m market_reviewer.cli historical-replay --symbol ETH --start <UTC-checkpoint> --end <UTC-checkpoint> --input <market-data.v1.json> --step-minutes 60 --window-id <verified-window>
py -m market_reviewer.cli historical-replay-batch historical/windows.initial.json --dry-run
py -m market_reviewer.cli historical-replay-status --live-store research/missed-opportunities.json
```

Omit --input to reuse the symbol cache. Optional --external-history loads an
offline archive. --output-dir and --cache-dir allow isolated research/test roots.
Status rebuilds only the historical summary; the optional live store is read-only.

Future workflow: historical research -> frozen candidate rules/model -> LIVE
forward evaluation. Calibration, retroactive rule rewriting, historical data
downloads, live enrichment, and large-scale replay are outside this foundation.
