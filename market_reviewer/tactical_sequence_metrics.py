"""Read-only counters over a caller's validated durable ledger cohort."""

import hashlib
import json
from collections import Counter


def sequence_metrics(records):
    transitions = [t for r in records for t in r["transitions"]]
    identities = {(symbol, state["sequence_id"]) for r in records
                  for symbol, state in r["states"].items() if state["sequence_id"]}
    identities.update((t["symbol"], t["sequence_id"]) for t in transitions if t["sequence_id"])
    invalidations = [t for t in transitions if t["to_state"] == "INVALIDATED"]
    invalidated = {(t["symbol"], t["sequence_id"]) for t in invalidations}
    heads = records[-1]["states"] if records else {}
    active = {(symbol, state["sequence_id"]) for symbol, state in heads.items()
              if state["sequence_id"] and state["sequence_state"] != "INVALIDATED"}
    reasons = Counter(t["reason"] for t in invalidations)
    starts = sum(t["to_state"] == "FORMING" for t in transitions)
    withdrawn = reasons["TACTICAL_DIRECTION_WITHDRAWN"]
    reversal = reasons["TACTICAL_DIRECTION_REVERSAL"]
    checks = {
        "starts_equal_unique_ids": starts == len(identities),
        "unique_equal_invalidated_plus_active": identities == invalidated | active and not invalidated & active,
        "invalidation_reasons_reconcile": len(invalidations) == withdrawn + reversal + sum(
            n for reason, n in reasons.items()
            if reason not in {"TACTICAL_DIRECTION_WITHDRAWN", "TACTICAL_DIRECTION_REVERSAL"}),
        "one_invalidation_per_sequence": len(invalidations) == len(invalidated),
    }
    return {
        "sequence_started": starts,
        "unique_sequence_ids": len(identities),
        "invalidated_unique_sequences": len(invalidated),
        "invalidation_transitions": len(invalidations),
        "withdrawn_transitions": withdrawn,
        "reversal_transitions": reversal,
        "other_invalidation_transitions": len(invalidations) - withdrawn - reversal,
        "still_active_sequences": len(active),
        "current_invalidated_sequences": sum(bool(s["sequence_id"]) and s["sequence_state"] == "INVALIDATED"
                                             for s in heads.values()),
        "metric_semantics": {
            "sequence_started": "FORMING transition count in supplied cohort",
            "unique_sequence_ids": "Distinct symbol/sequence_id pairs observed in supplied cohort",
            "invalidated_unique_sequences": "Distinct symbol/sequence_id pairs with an INVALIDATED transition",
            "invalidation_transitions": "All transitions into INVALIDATED, without deduplication",
            "still_active_sequences": "Non-invalidated sequence IDs at the final cohort receipt",
            "current_invalidated_sequences": "INVALIDATED sequence IDs at the final cohort receipt",
        },
        "source": {
            "authority": "DURABLE_SHADOW_SEQUENCE_LEDGER_RECORDS",
            "first_observation": records[0]["observation_number"] if records else None,
            "latest_observation": records[-1]["observation_number"] if records else None,
            "record_count": len(records),
            "cohort_sha256": hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"),
                                                     ensure_ascii=True).encode("utf-8")).hexdigest(),
        },
        "conservation": {"status": "PASS" if records and all(checks.values()) else "UNAVAILABLE" if not records else "FAIL",
                         "checks": checks, "diagnostic_only": True},
    }
