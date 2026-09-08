"""Local-only reconstruction. Persist candidate graphs before a separate outcome join."""
import argparse
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from market_reviewer.historical_data import to_market_data_frame
from market_reviewer.persistence import atomic_write_json
from .raw_prefix_smc import VERSION, TF, SWEEP_TFS, STRUCTURE_TFS, reconstruct, semantic_key

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "research/historical-replay"
OUT = BASE / "smc-provenance-raw-prefix.v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def original_observations(graphs):
    wanted = {next(iter(g["events"].values()))["decision_time_context"]["origin_observation_id"] for g in graphs}
    found = {}; paths = set()
    for folder in ("phase1/pilot", "phase2/replay", "phase2_1/replay", "phase2_1b/replay"):
        for path in sorted((BASE / folder / "windows").glob("*.json")):
            for row in read(path)["observations"]:
                key = row["historical_observation_id"]
                if key in wanted:
                    if key in found and found[key] != row:
                        raise ValueError("conflicting frozen origin")
                    found[key] = row; paths.add(path)
    if set(found) != wanted:
        raise ValueError("missing original decision snapshots")
    return found, paths


def inventory_keys(review, raw_events):
    keys = set()
    for e in raw_events.values():
        kind = e["kind"]; ev = e["evidence"]; tf = e["timeframe"]
        if kind == "DISPLACEMENT":
            # The original renderer is authoritative, not the enriched provenance inventory.
            from market_reviewer import reviewer as rv
            obj = rv.DisplacementEvent(**ev)
            if rv._displacement_text(obj) == review["Displacement"]:
                keys.add(semantic_key(e))
        elif kind in {"BOS", "MSS"}:
            if any(str(e["timestamp"]) in line and kind in line
                   for line in review["Last_Structure_Events"][tf]):
                keys.add(semantic_key(e))
        elif kind in {"LIQUIDITY", "SWEPT", "RECLAIMED", "FVG", "OB"}:
            field = {"LIQUIDITY": "Liquidity", "SWEPT": "Liquidity_Events", "RECLAIMED": "Liquidity_Events",
                     "FVG": "FVG", "OB": "Order_Blocks"}[kind]
            for x in review[field]:
                if kind in {"SWEPT", "RECLAIMED"} and x["event_type"] != kind:
                    continue
                di = x.get("direction") or ("BEARISH" if "Buy-side" in x.get("type", x.get("level_type", ""))
                        or "Highs" in x.get("type", x.get("level_type", "")) else "BULLISH")
                xx = {"kind": kind, "timeframe": x["timeframe"], "direction": di,
                      "timestamp": x.get("formed_at", x.get("timestamp")), "evidence": x}
                if semantic_key(e) == semantic_key(xx):
                    keys.add(semantic_key(e)); break
    return keys


def recheck_229(old, outputs):
    occurrences = defaultdict(list)
    for g in old:
        for r in g["relations"].values():
            if r["type"] == "RECLAIM_AFTER":
                occurrences[r["parent"]].append((g, r))
    if len(occurrences) != 229:
        raise ValueError("229-sweep baseline mismatch")
    rows = []
    for swid, cases in sorted(occurrences.items()):
        g, relation = min(cases, key=lambda x: (x[0]["origin_checkpoint"], x[0]["episode_id"], x[1]["child"]))
        out = outputs[g["episode_id"]]; recid = relation["child"]
        if swid not in out["events"] or recid not in out["events"]:
            raise ValueError("exact legacy reclaim identity missing: " + recid)
        diags = [d for d in out["diagnostic_candidates"] if d["parent"] == recid]
        linked = [r for r in out["relations"] if r["parent"] == recid and r["type"] == "DISPLACEMENT_CANDIDATE"]
        plausible = {r["child"] for r in linked}
        is_ambiguous = any(r["ancestry_state"] == "AMBIGUOUS" for r in linked)
        rows.append({"sweep_id": swid, "reclaim_id": recid, "origin": g["episode_id"],
                     "raw_candidates_in_window": len(diags),
                     "expected_direction": len({d["child"] for d in diags if d["expected_direction"]}),
                     "plausible_candidates": len(plausible),
                     "structure_consequence": sum(any(r["parent"] == k and r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE"
                                                       for r in out["relations"]) for k in plausible),
                     "state": "AMBIGUOUS" if is_ambiguous else "UNAMBIGUOUS" if plausible else "NO_CANDIDATE",
                     "candidate_ids": sorted(plausible),
                     "diagnostic_classes": dict(Counter(d["tf_relation"] for d in diags)),
                     "rejections": dict(Counter(d["reason"] for d in diags if d["reason"] != "PASS"))})
    summary = {"total": 229, "old_accepted_reclaim_linked_displacements": 2,
               "with_raw_displacement_candidate": sum(r["raw_candidates_in_window"] > 0 for r in rows),
               "expected_direction": sum(r["expected_direction"] > 0 for r in rows),
               "structure_consequence": sum(r["structure_consequence"] > 0 for r in rows),
               "with_plausible_candidate": sum(r["plausible_candidates"] > 0 for r in rows),
               "dispositions": dict(Counter(r["state"] for r in rows))}
    assert sum(summary["dispositions"].values()) == 229
    return {"summary": summary, "rows": rows}


def summarize(outputs):
    events = [e for g in outputs.values() for e in g["events"].values()]
    relations = [r for g in outputs.values() for r in g["relations"]]
    chains = [c for g in outputs.values() for c in g["chains"]]
    mss = [m for g in outputs.values() for m in g["contextual_mss_candidates"]]
    return {"schema": VERSION, "origins": len(outputs), "count_unit": "origin-event memberships; not independent chains",
            "events_by_family": dict(Counter(e["kind"] for e in events)),
            "unique_event_ids_by_family": {k: len({e["event_id"] for e in events if e["kind"] == k}) for k in sorted({e["kind"] for e in events})},
            "relations_by_type": dict(Counter(r["type"] for r in relations)),
            "chains": {t: dict(Counter(c["state"] for c in chains if c["chain_type"] == t))
                       for t in ("REVERSAL", "CONTINUATION", "UNCLASSIFIED")},
            "inventory_visibility": dict(Counter(c["inventory_visibility"] for c in chains)),
            "mss": {"generic_total": len(mss), "plausible_displacement_parent": sum(bool(m["displacement_parents"]) for m in mss),
                    "plausible_liquidity_ancestry": sum(bool(m["sweep_ancestors"]) for m in mss),
                    "states": dict(Counter(m["state"] for m in mss))},
            "parent_ambiguity": dict(Counter(e["parent_state"] for e in events if e["kind"] == "DISPLACEMENT")),
            "candidate_rejections": dict(Counter(d["reason"] for g in outputs.values() for d in g["diagnostic_candidates"])),
            "tf_bridges": dict(Counter(r["proof"].get("tf_relation", "NOT_APPLICABLE") for r in relations)),
            "peak_candidates_per_parent": max(g["peak_candidates_per_parent"] for g in outputs.values()),
            "peak_origin_event_count": max(len(g["events"]) for g in outputs.values()),
            "peak_origin_relation_count": max(len(g["relations"]) for g in outputs.values())}


def reconstruct_all(output, limit=None):
    started = time.perf_counter()
    old = read(BASE / "smc-provenance-v11/frozen-origin-graphs.json")
    if len(old) != 45:
        raise ValueError("45-origin baseline required")
    observations, snapshot_paths = original_observations(old)
    protected = read(BASE / "smc-provenance-v11/baseline.json")
    paths = {Path(k) for k in protected} | snapshot_paths
    paths |= {p for name in ("smc-provenance-v1", "smc-provenance-v11") for p in (BASE / name).glob("*") if p.is_file()}
    before = {str(p): sha(p) for p in paths}
    protocol = {"schema": VERSION, "authority": "RAW_PREFIX_RESEARCH", "inventory_authority": "ORIGIN_INVENTORY",
                "sweep_matrix": SWEEP_TFS, "structure_matrix": STRUCTURE_TFS,
                "window": "inherited five-pivot-bar span; liquidity TF for reaction/D, consequence TF for structure",
                "retained_filters": ["strict two-left/two-right swings", "equal-level tolerance max(prefix close * .001, .01)",
                    "first exact sweep after reference availability", "raw reclaim retained through checkpoint; rejection and ancestry use inherited window",
                    "D previous20 medians, unchanged components and VALID/STRONG candidate requirement",
                    "three-candle FVG geometry", "OB valid structure-breaking D plus last opposite candle within10"],
                "removed_display_caps": ["structure last4", "FVG16/24", "OB8/12", "equal levels3", "liquidity24/latest-swing display cursor"],
                "no_hindsight": "closed OPEN+TF <= checkpoint; prefix-verified confirmation evidence; conservative origin upper bound otherwise",
                "locality": "reaction-range intersection or D body crosses swept reference; inherited context vetoes",
                "structure": "reference known before D; D body penetrates exact reference; bridge waits for later structure candle",
                "ancestry": "candidate DAG only; all parents retained; no nearest/first/largest selection",
                "outcomes": "not opened by reconstruction; separate join only after manifest freeze",
                "counts": "45 origins; memberships separate from globally unique event IDs"}
    atomic_write_json(output / "protocol.json", protocol)
    outputs = {}; manifest = []
    for i, g in enumerate(old[:limit]):
        first = next(iter(g["events"].values())); src = first["source"]; path = ROOT / src["file"]
        if sha(path) != src["sha256"]:
            raise ValueError("raw source hash mismatch")
        raw = read(path); payload = raw.get("market_data", raw)
        frames = {tf: to_market_data_frame(payload[first["symbol"]][tf]) for tf in TF}
        out = reconstruct(g, frames)
        review = observations[first["decision_time_context"]["origin_observation_id"]]["review"]
        keys = inventory_keys(review, out["events"])
        # Visibility comparison happens after ancestry freeze; never feeds candidate generation.
        for chain in out["chains"]:
            n = sum(semantic_key(out["events"][k]) in keys for k in chain["event_ids"])
            chain["inventory_visibility"] = "FULLY_VISIBLE_IN_ORIGIN_INVENTORY" if n == len(chain["event_ids"]) else "PARTIALLY_VISIBLE_IN_ORIGIN_INVENTORY" if n else "NOT_VISIBLE_IN_ORIGIN_INVENTORY"
        out["origin_inventory_sha256"] = canonical_hash(review)
        dest = output / "origins" / (hashlib.sha256(g["episode_id"].encode()).hexdigest()[:8] + ".json")
        atomic_write_json(dest, out)
        outputs[g["episode_id"]] = out
        manifest.append({"episode_id": g["episode_id"], "file": str(dest.relative_to(output)), "sha256": sha(dest)})
        print(f"origin {i+1}/{limit or 45}: events={len(out['events'])} relations={len(out['relations'])} elapsed={time.perf_counter()-started:.1f}s", flush=True)
    summary = summarize(outputs)
    if limit is None:
        recheck = recheck_229(old, outputs)
        atomic_write_json(output / "reclaimed-229.json", recheck)
        summary["reclaimed_229"] = recheck["summary"]
    if any(sha(Path(k)) != h for k, h in before.items()):
        raise ValueError("protected input/live file mutation")
    atomic_write_json(output / "baseline.json", before)
    atomic_write_json(output / "manifest.json", {"schema": VERSION, "origins": manifest, "status": "FROZEN",
                                               "digest": canonical_hash(manifest)})
    atomic_write_json(output / "summary.json", summary)
    atomic_write_json(output / "performance.json", {"runtime_seconds": time.perf_counter()-started,
                                                   "protected_files_unchanged": len(before)})
    print(json.dumps(summary, indent=2), flush=True)


def join_outcomes(output):
    manifest = read(output / "manifest.json")
    if manifest["status"] != "FROZEN" or len(manifest["origins"]) != 45:
        raise ValueError("complete frozen ancestry required before outcome access")
    graphs = []
    for row in manifest["origins"]:
        path = output / row["file"]
        if sha(path) != row["sha256"]:
            raise ValueError("frozen ancestry modified")
        graphs.append(read(path))
    outcomes = read(BASE / "smc-quality-v1/outcome-join.json")["records"]
    groups = defaultdict(set)
    for g in graphs:
        for c in g["chains"]:
            groups[c["chain_type"] + ":" + c["state"]].add(g["episode_id"])
    rows = {}
    for kind, ids in sorted(groups.items()):
        rows[kind] = {"independent_origins": len(ids), "overlapping_membership": True,
                      "horizons": {h: {"median_MFE_pct": statistics.median(outcomes[k]["outcomes"][h]["MFE_pct"] for k in ids),
                                        "median_MAE_pct": statistics.median(outcomes[k]["outcomes"][h]["MAE_pct"] for k in ids)}
                                   for h in ("1H", "4H", "12H", "24H")}}
    atomic_write_json(output / "outcome-comparison.json", {"frozen_digest": manifest["digest"], "descriptive_only": True,
                                                           "groups": rows, "no_linkage_feedback": True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUT))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--join-outcomes", action="store_true")
    args = parser.parse_args(); output = Path(args.output).resolve()
    if not output.is_relative_to(OUT.resolve()):
        raise ValueError("writes restricted to isolated raw-prefix namespace")
    if args.join_outcomes:
        join_outcomes(output)
    else:
        reconstruct_all(output, args.limit)


if __name__ == "__main__":
    main()
