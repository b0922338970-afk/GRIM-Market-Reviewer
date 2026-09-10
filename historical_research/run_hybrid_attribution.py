"""Isolated, outcome-blind replay of frozen single/cluster attribution."""
from collections import Counter, defaultdict
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from market_reviewer.persistence import atomic_write_json
from .run_raw_prefix_smc import BASE, ROOT, read, sha, canonical_hash
from .hybrid_attribution import VERSION, AUTHORITATIVE, resolve

RAW = BASE / "smc-provenance-raw-prefix.v1"
SINGLE = BASE / "smc-parent-attribution.v1"
CLUSTER = BASE / "smc-liquidity-cluster.v1"
OUT = BASE / VERSION


def bucket(authority):
    return "UNAMBIGUOUS" if authority in AUTHORITATIVE else "NONE" if authority == "NO_VALID_PARENT" else "AMBIGUOUS"


def run(output):
    started = time.perf_counter()
    manifests = {p: read(p / "manifest.json") for p in (RAW, SINGLE, CLUSTER)}
    if any(m["status"] != "FROZEN" or len(m["origins"]) != 45 for m in manifests.values()):
        raise ValueError("45 frozen origins required")
    protected = read(CLUSTER / "baseline.json")
    protected.update({str(p): sha(p) for p in CLUSTER.rglob("*.json")})
    if any(sha(Path(p)) != h for p, h in protected.items()):
        raise ValueError("protected baseline mismatch")
    rows = {p: {r["episode_id"]: r for r in m["origins"]} for p, m in manifests.items()}
    counts, ambiguity, comparisons = Counter(), Counter(), defaultdict(Counter)
    structure_counts, chain_counts = defaultdict(Counter), defaultdict(Counter)
    regression, grouping, differences = [], [], []
    origins, peak = [], 0
    divergent_displacements, divergent_views = set(), set()
    for index, raw_row in enumerate(manifests[RAW]["origins"], 1):
        episode = raw_row["episode_id"]
        inputs = []
        for path in (RAW, SINGLE, CLUSTER):
            r = rows[path][episode]
            source = path / r["file"]
            if sha(source) != r["sha256"]:
                raise ValueError("frozen input hash mismatch")
            inputs.append(read(source))
        graph, single, cluster = inputs
        result = resolve(graph, single, cluster)
        dest = output / raw_row["file"]
        atomic_write_json(dest, result)
        origins.append({"episode_id": episode, "file": raw_row["file"], "sha256": sha(dest)})
        peak = max(peak, len(result["parents"]))
        for did, p in result["parents"].items():
            s, c = single["displacements"][did], cluster["displacements"][did]
            sb = "UNAMBIGUOUS" if s["resolution_state"] in {"EXACT_PARENT", "UNAMBIGUOUS_PARENT"} else "NONE" if s["resolution_state"] == "NO_VALID_PARENT" else "AMBIGUOUS"
            cb = "UNAMBIGUOUS" if c["state"] == "UNAMBIGUOUS_CLUSTER_PARENT" else "NONE" if c["state"] == "NO_CLUSTER_PARENT" else "AMBIGUOUS"
            hb = bucket(p["authority_type"])
            counts[p["authority_type"]] += 1
            for name, b in (("SINGLE", sb), ("CLUSTER", cb), ("HYBRID", hb)):
                comparisons[name][b] += 1
            assessments = p["relationship_evidence"]["cluster_assessments"]
            for a in assessments:
                if "REACTION_DIVERGENCE" in a["reasons"]:
                    divergent_displacements.add((episode, did))
                    divergent_views.add((episode, a["cluster_view_id"]))
            ambiguity.update({reason: 1 for a in assessments for reason in a["reasons"]} if hb == "AMBIGUOUS" else {})
            chosen = cluster["parent_cluster_views"].get(c["resolved_cluster_view_id"])
            pure = sb == "AMBIGUOUS" and cb == "UNAMBIGUOUS" and chosen is not None and {u["sweep_id"] for u in s["parents"] if not u["contradicted"]} <= set(chosen["member_sweep_ids"])
            detail = {"episode_id": episode, "displacement_id": did, "single": sb, "cluster": cb,
                      "hybrid": hb, "authority_type": p["authority_type"], "evidence": p["relationship_evidence"]}
            if sb == "UNAMBIGUOUS" and cb == "AMBIGUOUS":
                detail["regression_result"] = "PRESERVED" if hb == "UNAMBIGUOUS" else "LEGITIMATE_DOWNGRADE" if p["relationship_evidence"]["excluded_parents"] else "UNEXPECTED_DOWNGRADE"
                regression.append(detail)
            if pure:
                detail["grouping_result"] = "ESCALATED" if p["authority_type"] == "COHERENT_CLUSTER_PARENT" else "INVALIDATED" if hb == "NONE" else "REMAINED_AMBIGUOUS"
                grouping.append(detail)
            diagnostic = "UNAMBIGUOUS" if pure else sb
            if diagnostic != hb:
                differences.append(dict(detail, diagnostic=diagnostic))
        structure_counts["HYBRID"].update(x["classification"] for x in result["structure_consequences"])
        structure_counts["CLUSTER"].update(x["classification"] for x in cluster["structure_consequences"])
        # Reuse the raw structural DAG to compare BOS and MSS on one denominator.
        for st in result["structure_consequences"]:
            dids = st["displacement_ids"]
            if len(dids) == 1 and single["displacements"].get(dids[0], {}).get("resolution_state") in {"EXACT_PARENT", "UNAMBIGUOUS_PARENT"}:
                structure_counts["SINGLE"]["CONTEXTUAL_" + st["kind"]] += 1
        for c in result["chains"]:
            chain_counts[c["chain_type"]][c["state"]] += 1
        print(f"origin {index}/45; hybrid parents={len(result['parents'])}", flush=True)
    regression_counts = Counter(r["regression_result"] for r in regression)
    grouping_counts = Counter(r["grouping_result"] for r in grouping)
    if len(regression) != 268 or len(grouping) != 161 or regression_counts["UNEXPECTED_DOWNGRADE"]:
        raise ValueError("historical acceptance cohort mismatch")
    summary = {"schema": VERSION, "origins": len(origins), "authority_counts": dict(counts),
               "comparison": dict(comparisons), "268_regression": dict(regression_counts),
               "161_grouping": dict(grouping_counts), "diagnostic_difference_count": len(differences),
               "diagnostic_differences": dict(Counter(r["diagnostic"] + " -> " + r["hybrid"] + " / " + r["authority_type"] for r in differences)),
               "reaction_divergence": {"displacements": len(divergent_displacements), "as_of_views": len(divergent_views)},
               "contextual_structure": dict(structure_counts), "chains": dict(chain_counts),
               "remaining_ambiguity_reasons": dict(ambiguity), "count_unit": "origin memberships, not independent chains",
               "peak_parents_per_origin": peak, "outcome_access": False, "protected_files": len(protected)}
    atomic_write_json(output / "summary.json", summary)
    atomic_write_json(output / "cohorts.json", {"regressions": regression, "groupings": grouping, "diagnostic_differences": differences})
    atomic_write_json(output / "manifest.json", {"schema": VERSION, "status": "FROZEN", "origins": origins, "digest": canonical_hash(origins)})
    if any(sha(Path(p)) != h for p, h in protected.items()):
        raise ValueError("protected input mutation")
    atomic_write_json(output / "baseline.json", protected)
    atomic_write_json(output / "performance.json", {"runtime_seconds": time.perf_counter() - started})
    print(json.dumps(summary, indent=2))


def verify():
    subprocess.run([sys.executable, "-m", "historical_research.run_hybrid_attribution", "--second"], cwd=ROOT,
                   env=dict(os.environ, PYTHONHASHSEED="97", PYTHONDONTWRITEBYTECODE="1"), check=True)
    manifest = read(OUT / "manifest.json")
    files = [r["file"] for r in manifest["origins"]] + ["summary.json", "cohorts.json", "manifest.json"]
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
