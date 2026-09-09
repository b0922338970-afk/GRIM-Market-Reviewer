"""Frozen 45-origin cluster backfill. Writes only a new research namespace."""
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
import json
import os
import subprocess
import sys
import time

from market_reviewer.historical_data import to_market_data_frame
from market_reviewer.persistence import atomic_write_json
from .run_raw_prefix_smc import ROOT, BASE, read, sha, canonical_hash
from .raw_prefix_smc import TF
from .liquidity_cluster import VERSION, reconstruct

RAW = BASE / "smc-provenance-raw-prefix.v1"
PARENTS = BASE / "smc-parent-attribution.v1"
OUT = BASE / VERSION


def run(output):
    start = time.perf_counter()
    raw = read(RAW / "manifest.json")
    parent = read(PARENTS / "manifest.json")
    if raw["status"] != "FROZEN" or parent["status"] != "FROZEN" or len(raw["origins"]) != 45:
        raise ValueError("45 frozen origins required")
    protected = read(PARENTS / "baseline.json")
    protected.update({str(p): sha(p) for p in PARENTS.glob("*.json")})
    protected.update({str(PARENTS / r["file"]): r["sha256"] for r in parent["origins"]})
    if any(sha(Path(p)) != h for p, h in protected.items()):
        raise ValueError("protected baseline mismatch")
    parent_rows = {r["episode_id"]: r for r in parent["origins"]}
    counts = Counter()
    resolutions = Counter()
    lifecycle = Counter()
    sizes = []
    states = Counter()
    comparison = Counter()
    reduction_basis = Counter()
    contextual = Counter()
    chains = defaultdict(Counter)
    manifest = []
    examples = {}
    peak = 0
    excluded_membership = 0
    nested_depth = 0
    causal_resolution = Counter()
    for i, row in enumerate(raw["origins"], 1):
        if sha(RAW / row["file"]) != row["sha256"]:
            raise ValueError("raw graph hash mismatch")
        graph = read(RAW / row["file"])
        first = next(iter(graph["events"].values()))
        path = ROOT / first["source"]["file"]
        if sha(path) != first["source"]["sha256"]:
            raise ValueError("raw candle source mismatch")
        payload = read(path)
        payload = payload.get("market_data", payload)
        frames = {tf: to_market_data_frame(payload[first["symbol"]][tf]) for tf in TF}
        prior = read(PARENTS / parent_rows[row["episode_id"]]["file"])
        result = reconstruct(graph, prior, frames)
        dest = output / row["file"]
        atomic_write_json(dest, result)
        manifest.append({"episode_id": row["episode_id"], "file": row["file"], "sha256": sha(dest)})
        for c in result["clusters"]:
            counts[c["cluster_type"]] += 1
            resolutions[c["resolution"]] += 1
            lifecycle[c["lifecycle_state"]] += 1
            sizes.append(len(c["member_sweep_ids"]))
            excluded_membership += bool(c["unresolved_membership_alternatives"])
            nested_depth = max(nested_depth, max(c["nested_depth"].values()))
        for did, d in result["displacements"].items():
            states[d["state"]] += 1
            comparison[d["previous_single_sweep_state"] + " -> " + d["state"]] += 1
            if d["previous_single_sweep_state"] == "AMBIGUOUS_PARENT" and d["state"] == "UNAMBIGUOUS_CLUSTER_PARENT":
                old_parents = {u["sweep_id"] for u in prior["displacements"][did]["parents"] if not u["contradicted"]}
                chosen = result["parent_cluster_views"][d["resolved_cluster_view_id"]]
                reduction_basis["ALL_PRIOR_VALID_PARENTS_IN_ONE_CLUSTER" if old_parents <= set(chosen["member_sweep_ids"]) else "REQUIRES_ADDITIONAL_CONTEXT_EXCLUSIONS"] += 1
            category = ("AMBIGUITY_REDUCED" if d["previous_single_sweep_state"] == "AMBIGUOUS_PARENT" and d["state"] == "UNAMBIGUOUS_CLUSTER_PARENT" else
                        "AMBIGUITY_PRESERVED" if d["state"] == "AMBIGUOUS_CLUSTER_PARENT" else "NO_CLUSTER_PARENT" if d["state"] == "NO_CLUSTER_PARENT" else "ALREADY_UNAMBIGUOUS")
            examples.setdefault(category, {"origin": graph["episode_id"], "displacement_id": did,
                                           "displacement_open": graph["events"][did]["timestamp"], "result": d,
                                           "cluster_views": {r["cluster_view_id"]: result["parent_cluster_views"][r["cluster_view_id"]] for r in d["relations"]}})
            if d["resolved_cluster_view_id"]:
                c = result["parent_cluster_views"][d["resolved_cluster_view_id"]]
                causal_resolution[c["resolution"]] += 1
        contextual.update(s["classification"] for s in result["structure_consequences"])
        for c in result["chains"]:
            chains[c["profile"]][c["state"]] += 1
        peak = max(peak, result["peak_membership_pair_checks"])
        print(f"origin {i}/45: clusters={len(result['clusters'])}; displacement candidates={len(result['displacements'])}", flush=True)
    summary = {"schema": VERSION, "origins": len(manifest), "count_unit": "origin memberships, not independent episodes",
               "clusters": sum(counts.values()), "cluster_types": dict(counts),
               "sweeps_per_cluster": {"mean": mean(sizes), "median": median(sizes), "max": max(sizes), "total_sweep_memberships": sum(sizes)},
               "origin_resolution_counts": dict(resolutions), "origin_lifecycle_counts": dict(lifecycle),
               "resolution_scope": "known reaction outcomes within existing sweep deadline; later break separately INVALIDATED",
               "displacement_parent_states": dict(states), "single_vs_cluster": dict(comparison),
               "ambiguity_reduction_basis": dict(reduction_basis),
               "unambiguous_parent_resolution_at_displacement": dict(causal_resolution),
               "structure_consequences": dict(contextual), "chain_candidates": dict(chains),
               "non_clique_singletons": excluded_membership, "max_nested_depth": nested_depth,
               "peak_membership_pair_checks": peak, "protected_files": len(protected), "outcome_access": False}
    atomic_write_json(output / "summary.json", summary)
    atomic_write_json(output / "examples.json", examples)
    atomic_write_json(output / "manifest.json", {"schema": VERSION, "status": "FROZEN", "origins": manifest, "digest": canonical_hash(manifest)})
    if any(sha(Path(p)) != h for p, h in protected.items()):
        raise ValueError("protected state mutation")
    atomic_write_json(output / "baseline.json", protected)
    atomic_write_json(output / "performance.json", {"runtime_seconds": time.perf_counter() - start})
    print(json.dumps(summary, indent=2))


def verify():
    subprocess.run([sys.executable, "-m", "historical_research.run_liquidity_cluster", "--second"], cwd=ROOT,
                   env=dict(os.environ, PYTHONHASHSEED="97", PYTHONDONTWRITEBYTECODE="1"), check=True)
    manifest = read(OUT / "manifest.json")
    files = [r["file"] for r in manifest["origins"]] + ["summary.json", "manifest.json", "examples.json"]
    if any(sha(OUT / p) != sha(OUT / "verify" / p) for p in files):
        raise ValueError("fresh-process determinism failure")
    baseline = read(OUT / "baseline.json")
    if any(sha(Path(p)) != h for p, h in baseline.items()):
        raise ValueError("protected baseline mutation")
    result = {"status": "PASS", "fresh_process_determinism": "PASS", "compared_files": len(files), "protected_files_unchanged": len(baseline)}
    atomic_write_json(OUT / "verification.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        run(OUT / "verify" if "--second" in sys.argv else OUT)
