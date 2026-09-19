# Live Hybrid SMC producer

`live-hybrid-smc-payload.v1` is a research adapter, called only after a new live
independent record is inserted. It cannot approve/reject origin eligibility.
It feeds `live-smc-frozen-context.v1` in the same existing research transaction.
No scheduling, Production persistence, recovery ordering or historical data change.

Pipeline: legally available per-timeframe prefix -> existing raw_prefix_smc ->
parent_attribution -> liquidity_cluster -> hybrid_attribution ->
hybrid_chain_state.extensions / freeze_origin -> existing matching context.

The previously untracked hybrid_chain_state.py is versioned unchanged. The pure
family/checked_records/extract functions in smc_origin_context.py are preserved
verbatim from the frozen archival quality script. A parity test checks this where
the archival script exists. No historical batch driver or outcome join is called.
Inventory visibility diagnostics are not evaluated by this live adapter and do
not determine ancestry or selection.

The checkpoint is origin M5 open + 300. Each frame contributes only its own
closed candles with open + duration <= checkpoint. Future fetch metadata is
removed. Only whitelisted decision feature inputs reach the context extractor;
the record's outcomes and later snapshots are never passed to an engine.
Prefix hash, event count, cutoff map and engine version are retained as provenance.

Complete inputs produce symbol/direction/phase/regime/HTF/location/extension,
liquidity scope and displacement state under the existing matching semantics.
Parent, reaction, contextual MSS/BOS and FVG/BPR/OB/Breaker categories come from
the existing primary-chain selector. A valid scan with no zone gives its existing
NONE/NOT_CONFIRMED category, not a fabricated positive. Ambiguity is preserved.
Missing frames/context or engine failure yields UNAVAILABLE and an exact reason;
capture remains immutable and the independently accepted origin is retained.

There is no implicit history fetch. Provenance describes the legally available
supplied prefix, not an assertion of unlimited historical coverage. Historical
sample outcomes and retrospective regime labels are not accessed.

The old live origins are never visited for capture. No live observation or runner
restart is performed during installation. An already loaded Python process needs
its normal code deployment/reload before using the new producer.
