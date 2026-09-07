"""Run V4.3 v1.1 research on existing frozen origins; no fetch or live writes."""
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path

from market_reviewer.historical_data import frames_at_checkpoint, to_market_data_frame
from market_reviewer.persistence import atomic_write_json
from .smc_ancestry_v11 import reconstruct, TF, VERSION, WINDOW_PIVOT_SPAN

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "research/historical-replay"
OUT = BASE / "smc-provenance-v11"


def read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(name, data):
    atomic_write_json(OUT / name, data)


def main():
    source = BASE / "smc-provenance-v1/frozen-origin-graphs.json"
    graphs = read(source)
    assert len(graphs) == 45
    protected = read(BASE / "smc-provenance-v1/baseline.json")
    for directory in ["smc-provenance-v1", "smc-quality-v1"]:
        protected.update({str(p): sha(p) for p in (BASE / directory).glob("*") if p.is_file()})
    before = {k: sha(Path(k)) for k in protected}
    # Snapshot actual state now, not stale historical expected hashes.
    write("protocol.json", {
        "schema": VERSION, "window_pivot_span": WINDOW_PIVOT_SPAN,
        "deadline": "sweep.available_at + 5 * liquidity timeframe seconds; inclusive confirmation deadline",
        "temporal_rule": "displacement open >= parent availability; confirmation <= sweep deadline",
        "price_rule": "without reaction: D body crosses swept price; with reaction: D range interacts with reaction range",
        "structure_rule": "D close penetrates pre-sweep confirmed opposing swing; exact generic break price matches reference",
        "reference_checkpoint": "sweep candle OPEN, not future confirmation",
        "same_candle_structure": "allowed only exact D structure break; contextual availability waits for D confirmation",
        "ambiguity": "multiple eligible sweep ancestors => unlinked",
        "accepted_outside": "no valid reaction in window, deadline candle closed outside",
        "rejection": "later re-probe beyond level, directional body closes inside; may coexist with reclaim",
        "outcomes": "joined only after frozen v1.1 graphs written",
        "depth_note": "L4/L5 require contextual reversal MSS. Continuation BOS retained separately, not ranked.",
    })
    cache = {}; output = []
    for i, graph in enumerate(graphs):
        src = next(iter(graph["events"].values()))["source"]
        path = ROOT / src["file"]
        assert sha(path) == src["sha256"], path
        key = str(path)
        if key not in cache:
            raw = read(path)
            symbol = next(iter(graph["events"].values()))["symbol"]
            payload = raw["market_data"] if raw.get("schema") == "historical-market-cache.v1" else raw
            cache[key] = {tf: to_market_data_frame(payload[symbol][tf]) for tf in TF}
        frames = frames_at_checkpoint(cache[key], graph["origin_checkpoint"])
        output.append(reconstruct(graph, frames))
        print(f"origin {i+1}/45 complete", flush=True)
    write("frozen-origin-graphs.json", output)
    frozen = sha(OUT / "frozen-origin-graphs.json")
    outcomes = read(BASE / "smc-quality-v1/outcome-join.json")["records"]
    cohorts = read(BASE / "first-feature-review/cohorts.json")["assignments"]
    isolated = {r["episode_id"]: r for r in read(BASE / "smc-quality-v1/frozen-features.json")}
    def stats(rows):
        ids = [r["episode_id"] for r in rows]
        return {"N": len(ids), "direction_mix": dict(Counter(r["direction"] for r in rows)),
                "regime_mix": dict(Counter(outcomes[k]["retrospective_regime"] for k in ids)),
                "phase_mix": dict(Counter(r["phase"] for r in rows)),
                "horizons": {h: {
                    "median_MFE_pct": statistics.median(outcomes[k]["outcomes"][h]["MFE_pct"] for k in ids) if ids else None,
                    "median_MAE_pct": statistics.median(outcomes[k]["outcomes"][h]["MAE_pct"] for k in ids) if ids else None,
                    "cohorts": dict(Counter(cohorts[h].get(k, "UNASSIGNED") for k in ids))}
                    for h in ["1H", "4H", "12H", "24H"]}}
    def table(rows):
        def present(row, kind):
            direction = "BULLISH" if row["direction"] == "LONG" else "BEARISH"
            return any(e["kind"] in kind and e["direction"] == direction for e in row["events"].values())
        def causal(row, typ):
            direction = "BULLISH" if row["direction"] == "LONG" else "BEARISH"
            return any(r["type"] == typ and row["events"][r["child"]]["direction"] == direction
                       for r in row["relations"].values())
        return {
            "isolated_inventory": {name: {str(flag): stats([r for r in rows if present(r,kinds) == flag])
                                   for flag in [True,False]} for name,kinds in {
                "SWEEP": {"SWEPT"}, "MSS_BOS": {"MSS","BOS"}, "FVG": {"FVG"}, "OB": {"OB"}}.items()},
            "linked_inventory": {name: {str(flag): stats([r for r in rows if causal(r,name) == flag])
                                 for flag in [True,False]} for name in [
                "DISPLACEMENT_AFTER_SWEEP", "DISPLACEMENT_AFTER_RECLAIM", "CONTEXTUAL_MSS_AFTER"]},
            "ancestry_depth": {level: stats([r for r in rows if r["primary_chain"] and r["primary_chain"]["ancestry_depth"] == level]) for level in ["L0","L1","L2","L3","L4","L5"]},
            "chain_type": {kind: stats([r for r in rows if r["primary_chain"] and r["primary_chain"]["chain_type"] == kind]) for kind in ["REVERSAL_CHAIN","CONTINUATION_CHAIN","PARTIAL_CHAIN","FAILED_CHAIN"]},
            "isolated_fvg": {state: stats([r for r in rows if isolated[r["episode_id"]]["families"]["FVG_QUALITY"]["state"] == state])
                             for state in sorted({r["families"]["FVG_QUALITY"]["state"] for r in isolated.values()})},
        }
    write("outcome-comparison.json", {"frozen_sha256": frozen, "unit": "independent origin only", "all": table(output),
          "decision_phase": {phase: table([r for r in output if r["phase"] == phase]) for phase in sorted({r["phase"] for r in output})},
          "retrospective_regime": {reg: table([r for r in output if outcomes[r["episode_id"]]["retrospective_regime"] == reg])
                                  for reg in sorted({outcomes[r["episode_id"]]["retrospective_regime"] for r in output})}})
    def unique_kind(kind):
        return len({k for g in output for k,e in g["events"].items() if e["kind"] == kind})
    def linked(kind):
        return len({r["child"] for g in output for r in g["relations"].values() if r["type"] == kind})
    summary = {
        "status": "PASS", "origins": 45, "liquidity_events": unique_kind("LIQUIDITY"),
        "exact_sweeps": linked("SWEEP_OF"), "reclaimed_sweeps": len({r["parent"] for g in output for r in g["relations"].values() if r["type"] == "RECLAIM_AFTER"}),
        "accepted_outside_sweeps": len({k for g in output for k,e in g["events"].items() if e["kind"] == "SWEPT" and e["evidence"].get("aftermath") == "SWEEP_ACCEPTED_OUTSIDE"}),
        "sweep_linked_displacements": linked("DISPLACEMENT_AFTER_SWEEP"),
        "reclaim_linked_displacements": linked("DISPLACEMENT_AFTER_RECLAIM"),
        "contextual_mss": unique_kind("CONTEXTUAL_MSS"),
        "contextual_bos": unique_kind("CONTEXTUAL_BOS_CONTINUATION"),
        "chain_memberships": {kind: len({c["smc_chain_id"] for g in output for c in g["chains"] if c["chain_type"] == kind})
                              for kind in ["REVERSAL_CHAIN","CONTINUATION_CHAIN","PARTIAL_CHAIN","FAILED_CHAIN"]},
        "primary_chain_counts": dict(Counter(g["primary_chain"]["chain_type"] for g in output if g["primary_chain"])),
        "primary_depth": dict(Counter(g["primary_chain"]["ancestry_depth"] for g in output if g["primary_chain"])),
        "rejection_reasons": dict(Counter(x["reason"] for g in output for x in g["rejected_links"])),
        "protected_files": len(before), "frozen_sha256": frozen,
    }
    assert sha(OUT / "frozen-origin-graphs.json") == frozen
    assert all(sha(Path(k)) == h for k,h in before.items()), "input/live state changed"
    write("baseline.json", before); write("validation.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
