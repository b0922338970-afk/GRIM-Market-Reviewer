"""Local research driver; all inputs frozen, no outcome join or live writes."""
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from market_reviewer.historical_data import to_market_data_frame
from market_reviewer.persistence import atomic_write_json
from .run_raw_prefix_smc import ROOT, BASE, read, sha, canonical_hash
from .raw_prefix_smc import TF
from .parent_attribution import VERSION, resolve_graph

SOURCE=BASE/"smc-provenance-raw-prefix.v1"
OUT=BASE/"smc-parent-attribution.v1"


def run(output):
    start=time.perf_counter(); manifest=read(SOURCE/"manifest.json")
    if manifest["status"]!="FROZEN" or len(manifest["origins"])!=45:
        raise ValueError("45 frozen origins required")
    protected=read(SOURCE/"baseline.json")
    protected.update({str(p):sha(p) for p in SOURCE.glob("*.json")})
    protected.update({str(SOURCE/r["file"]):r["sha256"] for r in manifest["origins"]})
    if any(sha(Path(p))!=h for p,h in protected.items()):
        raise ValueError("protected baseline changed")
    results={}; emitted=[]; examples={}; counts=Counter(); failures=Counter(); primary=Counter(); chain_counts={}; mss=Counter()
    for i,row in enumerate(manifest["origins"]):
        g=read(SOURCE/row["file"]); first=next(iter(g["events"].values())); source=first["source"]
        path=ROOT/source["file"]
        if sha(path)!=source["sha256"]: raise ValueError("raw prefix source changed")
        payload=read(path); payload=payload.get("market_data",payload)
        frames={tf:to_market_data_frame(payload[first["symbol"]][tf]) for tf in TF}
        result=resolve_graph(g,frames); results[g["episode_id"]]=result
        dest=output/"origins"/Path(row["file"]).name
        atomic_write_json(dest,result)
        emitted.append({"file":str(dest.relative_to(output)),"sha256":sha(dest),"episode_id":g["episode_id"]})
        for did,r in result["displacements"].items():
            counts[r["resolution_state"]]+=1
            failures.update(r["ambiguity_causes"])
            if r["ambiguity_causes"]:
                ordered=("MULTIPLE_SAME_CLUSTER_SWEEPS","NESTED_SWEEPS","CROSS_TIMEFRAME_PARENT_COLLISION","TIMING_OVERLAP","NO_PRICE_LOCALITY_SEPARATION","NO_STRUCTURE_SEPARATION","OTHER")
                primary[next(k for k in ordered if k in r["ambiguity_causes"])]+=1
            for p in r["parents"]:
                counts[p["supersession_state"]]+=1
            categories=[]
            if r["resolution_state"]=="EXACT_PARENT":categories.append("EXACT_PARENT")
            if r["resolution_state"]=="UNAMBIGUOUS_PARENT" and any(p["supersession_state"]=="SUPERSEDED_PARENT" for p in r["parents"]): categories.append("UNAMBIGUOUS_AFTER_SUPERSESSION")
            if r["resolution_state"]=="AMBIGUOUS_PARENT":
                categories.append("UNRESOLVED_AMBIGUITY")
                if "NESTED_SWEEPS" in r["ambiguity_causes"]:categories.append("NESTED_AMBIGUITY")
                if "CROSS_TIMEFRAME_PARENT_COLLISION" in r["ambiguity_causes"]:categories.append("CROSS_TF_AMBIGUITY")
            if r["resolution_state"] in {"EXACT_PARENT","UNAMBIGUOUS_PARENT"} and any("STRUCTURE_CONSEQUENCE" in route["contradictions"] for p in r["parents"] for route in p["routes"]):categories.append("STRUCTURE_RESOLVES")
            for category in categories:
                examples.setdefault(category,{"origin":g["episode_id"],"checkpoint":g["origin_checkpoint"],
                    "displacement_id":did,"displacement_open":g["events"][did]["timestamp"],"resolution":r["resolution_state"],
                    "resolved_parent_id":r["resolved_parent_id"],
                    "parents":[{"sweep_id":p["sweep_id"],"sweep_open":g["events"][p["sweep_id"]]["timestamp"],
                                "sweep_available_at":g["events"][p["sweep_id"]]["available_at"],
                                "liquidity_id":p["liquidity_id"],"supersession_state":p["supersession_state"],"contradicted":p["contradicted"]} for p in r["parents"]]})
        mss.update(m["state"] for m in result["contextual_mss"])
        for c in result["chains"]:
            chain_counts.setdefault(c["chain_type"],Counter())[c["state"]]+=1
        print(f"origin {i+1}/45: D candidates={len(result['displacements'])}",flush=True)
    old=read(SOURCE/"reclaimed-229.json"); rows=[]
    for row in old["rows"]:
        result=results[row["origin"]]; valid=[]
        for did in row["candidate_ids"]:
            r=result["displacements"][did]
            parent=next((p for p in r["parents"] if p["sweep_id"]==row["sweep_id"] and not p["contradicted"]),None)
            if parent and any(x["reaction_id"]==row["reclaim_id"] and not x["contradictions"] for x in parent["routes"]):valid.append((did,r))
        state="NO_VALID_PARENT" if not valid else "UNAMBIGUOUS_PARENT" if len(valid)==1 and valid[0][1]["resolved_parent_id"]==row["sweep_id"] else "AMBIGUOUS_PARENT"
        rows.append({"origin":row["origin"],"sweep_id":row["sweep_id"],"reclaim_id":row["reclaim_id"],"baseline":row["state"],
                     "state":state,"valid_displacement_ids":[d for d,_ in valid],"child_ambiguity":len(valid)>1})
    assert len(rows)==229
    for key in ("EXACT_PARENT","UNAMBIGUOUS_AFTER_SUPERSESSION","NESTED_AMBIGUITY","CROSS_TF_AMBIGUITY","STRUCTURE_RESOLVES","UNRESOLVED_AMBIGUITY"):
        examples.setdefault(key,{"status":"NO_REAL_EXAMPLE","note":"Not manufactured; contract tested with synthetic evidence only"})
    counts.setdefault("EXACT_PARENT",0); counts.setdefault("SUPERSEDED_PARENT",0)
    summary={"schema":VERSION,"origins":45,"unit":"origin memberships; still45 independent origins",
             "reclaimed_229":dict(Counter(r["state"] for r in rows)),"baseline_229":old["summary"]["dispositions"],
             "displacements_and_supersession":dict(counts),"contextual_mss":dict(mss),"chains":chain_counts,
             "mss_basis":"unique structure displacement parent + resolved sweep parent; not sweep-child uniqueness",
             "mss_comparison":dict(Counter(m["raw_joint_ancestry_state"]+" -> "+m["state"] for g in results.values() for m in g["contextual_mss"])),
             "ambiguity_primary_causes":dict(primary),"ambiguity_multilabel_causes":dict(failures),
             "outcome_access":False,"protected_files":len(protected)}
    atomic_write_json(output/"recheck-229.json",rows)
    atomic_write_json(output/"examples.json",examples)
    atomic_write_json(output/"summary.json",summary)
    atomic_write_json(output/"manifest.json",{"schema":VERSION,"status":"FROZEN","origins":emitted,"digest":canonical_hash(emitted)})
    if any(sha(Path(p))!=h for p,h in protected.items()): raise ValueError("protected input mutation")
    atomic_write_json(output/"baseline.json",protected)
    atomic_write_json(output/"performance.json",{"runtime_seconds":time.perf_counter()-start})
    print(json.dumps(summary,indent=2))


def verify():
    subprocess.run([sys.executable,"-m","historical_research.run_parent_attribution","--second"],cwd=ROOT,
                   env=dict(os.environ,PYTHONHASHSEED="97",PYTHONDONTWRITEBYTECODE="1"),check=True)
    manifest=read(OUT/"manifest.json")
    files=[r["file"] for r in manifest["origins"]]+["manifest.json","recheck-229.json","examples.json","summary.json"]
    if any(sha(OUT/p)!=sha(OUT/"verify"/p) for p in files): raise ValueError("fresh-process mismatch")
    baseline=read(OUT/"baseline.json")
    if any(sha(Path(p))!=h for p,h in baseline.items()):raise ValueError("protected baseline changed")
    result={"status":"PASS","fresh_process_determinism":"PASS","compared_files":len(files),"protected_files_unchanged":len(baseline)}
    atomic_write_json(OUT/"verification.json",result); print(json.dumps(result))


if __name__=="__main__":
    if "--verify" in sys.argv:verify()
    else:run(OUT/"verify" if "--second" in sys.argv else OUT)
