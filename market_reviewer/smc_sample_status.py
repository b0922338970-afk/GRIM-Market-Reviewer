"""Read-only monitoring of frozen SMC origin and matched-cell outputs."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

DEFAULT_ROOT = Path("research/historical-replay/matched-smc-contrast.v1-verified")
CONTRASTS = {
    "MSS": "Contextual MSS vs Generic / No Contextual MSS",
    "BOS": "Contextual BOS vs Generic / No Contextual BOS",
    "FVG_BPR": "Chain-linked FVG vs Chain-linked BPR",
    "PARENT": "UNAMBIGUOUS_SINGLE vs COHERENT_CLUSTER",
    "OB_BREAKER": "Failed OB without Breaker vs Confirmed Chain Breaker",
    "REACTION": "Reclaimed vs Accepted-outside",
}
CONTEXT_FIELDS = {"symbol", "direction", "phase", "regime", "HTF", "location", "extension", "liquidity_type", "displacement"}
NA = "N/A"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unknown(reason):
    return {"availability": NA, "reason": reason, "context": NA, "A_count": NA,
            "B_count": NA, "target_per_side": 5, "preferred_per_side": [8, 10], "READY": NA}


def _read_verified(root, name, expected):
    payload = (root / name).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"{name}: frozen hash mismatch")
    return json.loads(payload)


def _origins(rows):
    if not isinstance(rows, list):
        raise ValueError("origin list unavailable")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid origin record")
        eid = row.get("episode_id")
        if not isinstance(eid, str) or not eid or eid in result:
            raise ValueError("missing or duplicate independent origin identity")
        if row.get("direction") not in {"LONG", "SHORT"}:
            raise ValueError("origin direction unavailable")
        result[eid] = row
    return result


def _best_cell(groups, origins):
    if not isinstance(groups, list):
        raise ValueError("matched cells unavailable")
    candidates, seen_ids, seen_contexts = [], set(), set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("invalid matched cell")
        context = group.get("context")
        if not isinstance(context, dict) or not CONTEXT_FIELDS <= context.keys():
            raise ValueError("matching context incomplete")
        if any(context[k] is None or context[k] == "" for k in CONTEXT_FIELDS):
            raise ValueError("matching context unavailable")
        if context["direction"] not in {"LONG", "SHORT"} or not isinstance(context["HTF"], list) or len(context["HTF"]) != 2 or any(not isinstance(x, str) or not x for x in context["HTF"]):
            raise ValueError("matching direction/HTF unavailable")
        key = _canonical(context)
        if key in seen_contexts:
            raise ValueError("duplicate matched context")
        seen_contexts.add(key)
        arms = {}
        for arm in ("A", "B"):
            ids = group.get(arm)
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise ValueError("arm membership unavailable")
            for eid in ids:
                if eid in seen_ids:
                    raise ValueError("origin repeated within contrast")
                seen_ids.add(eid)
                row = origins.get(eid)
                if row is None or row.get("matching_context") != context:
                    raise ValueError("member origin/context mismatch")
                if row.get("direction") != context["direction"] or row.get("phase") != context["phase"] or row.get("symbol") != context["symbol"]:
                    raise ValueError("member direction/phase/symbol mismatch")
            arms[arm] = len(ids)
        if arms["A"] + arms["B"]:
            candidates.append({"availability": "AVAILABLE", "context": context,
                               "A_count": arms["A"], "B_count": arms["B"],
                               "target_per_side": 5, "preferred_per_side": [8, 10],
                               "READY": min(arms.values()) >= 5})
    if not candidates:
        return _unknown("NO_RECORDED_CELL")
    # Display selection only: bottleneck support, total support, canonical context.
    # No outcome, quality rank, changed membership or cross-cell pooling.
    return min(candidates, key=lambda c: (-min(c["A_count"], c["B_count"]),
               -(c["A_count"] + c["B_count"]), _canonical(c["context"])))


def smc_sample_status(root: Path = DEFAULT_ROOT) -> dict:
    root = Path(root)
    result = {"schema": "smc-sample-status.v1", "source_root": str(root.resolve()),
              "monitoring_only": True, "total_independent_origins": NA, "LONG": NA, "SHORT": NA,
              "best_cell_selection": "max min(A,B), then max total, then canonical context; display only",
              "contrasts": {}, "READY_CONTRAST_COUNT": NA, "TOTAL_CONTRAST_COUNT": len(CONTRASTS),
              "NEXT_REVIEW_READY": NA}
    error = None
    try:
        freeze = json.loads((root / "freeze.json").read_bytes())
        if not isinstance(freeze, dict) or freeze.get("status") != "FROZEN":
            raise ValueError("frozen output unavailable")
        origins = _origins(_read_verified(root, "frozen-states.json", freeze.get("states_sha256")))
        counts = Counter(r["direction"] for r in origins.values())
        result.update(total_independent_origins=len(origins), LONG=counts["LONG"], SHORT=counts["SHORT"])
        membership = _read_verified(root, "membership.json", freeze.get("membership_sha256"))
        if not isinstance(membership, dict):
            raise ValueError("membership map unavailable")
    except (OSError, ValueError, TypeError) as exc:
        error = str(exc)
    for key, label in CONTRASTS.items():
        try:
            cell = _unknown(error) if error else _best_cell(membership.get(key), origins)
        except (ValueError, TypeError) as exc:
            cell = _unknown(str(exc))
        result["contrasts"][key] = {"label": label, **cell}
    flags = [c["READY"] for c in result["contrasts"].values()]
    ready = sum(flag is True for flag in flags)
    unknown = sum(flag == NA for flag in flags)
    result["READY_CONTRAST_COUNT"] = NA if unknown else ready
    result["NEXT_REVIEW_READY"] = True if ready >= 2 else NA if ready + unknown >= 2 else False
    return result
