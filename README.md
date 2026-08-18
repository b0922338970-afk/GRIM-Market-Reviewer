# GRIM Market Reviewer

Standalone market-data and market-review project for BTC and ETH.

This repository does not depend on AI_QUANT runtime, databases, schedulers,
execution systems, backend, frontend, ops, deploy code, or order execution.

## Scope

- Market Data
- Market Review
- Thesis Persistence
- External Fetch

## Commands

```bash
python -m unittest discover -s tests -v
python -m compileall -q market_reviewer tests
python -m market_reviewer.cli --help
```

## Review Only

Daily reviewer usage is:

```bash
python -m market_reviewer.cli review-external path/to/market-data-v1.json
```

Review-only loads an existing `DATA_READY` generation. It does not fetch
providers, combine providers, or create execution instructions.
