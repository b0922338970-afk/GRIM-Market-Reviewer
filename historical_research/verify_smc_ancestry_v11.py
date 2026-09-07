"""Fresh-process verification for the local, immutable 45-origin dataset."""
import json
import os
import subprocess
import sys
from pathlib import Path

from .run_smc_ancestry_v11 import ROOT, OUT, read, sha, write


def main():
    protected = read(OUT / "baseline.json")
    assert all(sha(Path(k)) == v for k, v in protected.items())
    outputs = ["frozen-origin-graphs.json", "outcome-comparison.json", "protocol.json"]
    hashes = []
    for seed in ["11", "97"]:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        subprocess.run([sys.executable, "-m", "historical_research.run_smc_ancestry_v11"],
                       cwd=ROOT, env=env, check=True, capture_output=True, text=True)
        hashes.append({name: sha(OUT / name) for name in outputs})
    assert hashes[0] == hashes[1], "fresh-process output mismatch"
    assert all(sha(Path(k)) == v for k, v in protected.items()), "protected input mutation"
    graphs = read(OUT / "frozen-origin-graphs.json")
    assert len({g["episode_id"] for g in graphs}) == 45
    for g in graphs:
        for r in g["relations"].values():
            assert r["parent"] in g["events"] and r["child"] in g["events"]
            assert r["available_at"] <= g["origin_checkpoint"]
            assert r["direction_compatibility"] and r["timeframe_compatibility"]
            assert r["relationship_reason"] and r["source_evidence"]
        for c in g["chains"]:
            if c["break_available_at"] is not None:
                assert all(g["events"][k]["available_at"] <= c["break_available_at"] for k in c["event_ids"])
    result = {"fresh_process_determinism": "PASS", "hash_seeds": [11, 97],
              "protected_files_unchanged": len(protected), "independent_origins": 45,
              "relationship_contract": "PASS", "no_post_break_extension": "PASS",
              "hashes": hashes[0]}
    write("fresh-process-verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
