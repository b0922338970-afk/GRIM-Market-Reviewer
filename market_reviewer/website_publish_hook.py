"""Forward-only, post-COMPLETE website publishing; never a transaction gate."""
from __future__ import annotations

from hashlib import sha256
import json

from .website_read_model import build_website_read_model
from .website_supabase_publisher import publish_supabase, supabase_status

SAFE_REASONS = {
    "HTTP_ERROR", "TIMEOUT", "NETWORK_ERROR", "TRANSPORT_ERROR",
    "NOT_CONFIGURED_OR_INVALID", "INVALID_PUBLIC_SNAPSHOT", "SNAPSHOT_BUILD_FAILED",
    "HOOK_ERROR", "SOURCE_MISMATCH", "NOT_COMPLETE",
}


def _boundary(config, observation_number, checkpoint):
    # Capture the exact persisted boundary, not mutable caller-owned reviews.
    paths = (config.commit_journal_path, config.production_head_path,
             config.state_path, config.research_tracker_path)
    raw = [path.read_bytes() for path in paths]
    journal, head, production, _ = [json.loads(value) for value in raw]
    transactions = journal.get("transactions", [])
    matches = [tx for tx in transactions if tx.get("observation_number") == observation_number]
    if len(matches) != 1:
        return None
    tx = matches[0]
    if tx.get("status") != "COMPLETE" or tx.get("research_status") != "COMPLETE":
        return None
    if (head.get("observation_number") != observation_number or
            head.get("canonical_checkpoint") != checkpoint or tx.get("canonical_checkpoint") != checkpoint):
        return None
    production_hash = sha256(raw[2]).hexdigest()
    if (head.get("production_state_sha256") != production_hash or
            tx.get("production_hash") != production_hash or
            tx.get("research_state_sha256") != sha256(raw[3]).hexdigest()):
        return None
    if any(production.get("symbols", {}).get(s, {}).get("previous_review_timestamp") != checkpoint - 300
           for s in ("BTC", "ETH")):
        return None
    return tuple(sha256(value).hexdigest() for value in raw)


def publish_latest_after_complete(config, observation_number, checkpoint, *, clock):
    """Return safe non-canonical status. No filesystem writes or retries here."""
    try:
        if config.dry_run or type(observation_number) is not int or type(checkpoint) is not int:
            return {"status": "SKIPPED_NOT_COMPLETE", "reason": "NOT_COMPLETE"}
        before = _boundary(config, observation_number, checkpoint)
        if before is None:
            return {"status": "SKIPPED_NOT_COMPLETE", "reason": "NOT_COMPLETE"}
        settings = supabase_status()
        if not settings["url_configured"] or not settings["credential_configured"]:
            return {"status": "NOT_CONFIGURED", "reason": "NOT_CONFIGURED_OR_INVALID"}
        if not settings["ready"]:
            return {"status": "PUBLISH_FAILED", "reason": "NOT_CONFIGURED_OR_INVALID"}
        model = build_website_read_model(
            review_state_path=config.state_path, runner_state_path=config.runner_state_path,
            research_store_path=config.research_tracker_path, liquidation_root=config.liquidation_root,
            notification_journal_path=config.output_dir / "notification-delivery.json",
            commit_journal_path=config.commit_journal_path, clock=clock,
        )
        if (model.get("source", {}).get("review_observation") != observation_number or
                model.get("runtime", {}).get("production_latest_observation") != observation_number or
                model.get("runtime", {}).get("research_latest_observation") != observation_number or
                any(model.get("symbols", {}).get(s, {}).get("generated_at") != checkpoint
                    for s in ("BTC", "ETH")) or
                _boundary(config, observation_number, checkpoint) != before):
            return {"status": "SKIPPED_SOURCE_MISMATCH", "reason": "SOURCE_MISMATCH"}
        # Reuse manual publisher with the already captured model. It must not reread
        # the journal after the consistency check and accidentally publish N+1.
        result = publish_supabase(builder=lambda: model, now=int(clock()))
        if result.get("status") == "PUBLISHED":
            return {"status": "PUBLISHED", "reason": None}
        reason = result.get("error")
        return {"status": "PUBLISH_FAILED", "reason": reason if reason in SAFE_REASONS else "HOOK_ERROR"}
    except Exception:
        return {"status": "PUBLISH_FAILED", "reason": "HOOK_ERROR"}
