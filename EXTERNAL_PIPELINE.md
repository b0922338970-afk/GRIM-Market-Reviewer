# External Pipeline

The external fetcher is designed for GitHub Actions and currently supports
manual execution only through `workflow_dispatch`.

Flow:

1. Fetch BTC and ETH.
2. Use one complete provider per symbol across D1, H4, H1, M15, and M5.
3. Stage the full generation before publication.
4. Run schema and integrity validation.
5. Publish an immutable `market-data.v1` artifact named
   `market-data-v1-<run_id>-<run_attempt>`.

The first provider able to supply all required timeframes wins:

1. Bitunix perpetual futures
2. Binance
3. Coinbase
4. Kraken

If a generation is incomplete, stale, invalid, or has no new closed candles
after the first successful external fetch, it must not replace the previous
complete artifact.
