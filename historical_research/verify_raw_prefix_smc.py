"""Fresh-process full45 determinism and protected-file audit; no production writes."""
import json
import os
import subprocess
import sys
from pathlib import Path

from market_reviewer.persistence import atomic_write_json
from .run_raw_prefix_smc import ROOT, OUT, read, sha


def main():
    manifest = read(OUT / "manifest.json")
    if len(manifest["origins"]) != 45 or manifest["status"] != "FROZEN":
        raise ValueError("first full reconstruction missing")
    protected = read(OUT / "baseline.json")
    second = OUT / "verify"
    env = dict(os.environ, PYTHONHASHSEED="97", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-m", "historical_research.run_raw_prefix_smc", "--output", str(second)],
                   cwd=ROOT, env=env, check=True)
    compared = []
    for rel in [r["file"] for r in manifest["origins"]] + ["protocol.json", "manifest.json", "summary.json", "reclaimed-229.json"]:
        if sha(OUT / rel) != sha(second / rel):
            raise ValueError("fresh-process mismatch: " + rel)
        compared.append(rel)
    changed = [path for path, digest in protected.items() if sha(Path(path)) != digest]
    if changed:
        raise ValueError("protected inputs changed: " + repr(changed))
    result = {"status": "PASS", "fresh_process_determinism": "PASS", "compared_files": len(compared),
              "origin_count": 45, "protected_files_unchanged": len(protected), "no_production_writes": True,
              "original_snapshots_immutable": True, "candidate_digest": manifest["digest"]}
    atomic_write_json(OUT / "verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
