from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.missed_opportunity import backfill_46_49, empty_store, load_tracker_store, persist_tracker_store
from market_reviewer.missed_opportunity_live import apply_missed_opportunity_observation, dry_run_missed_opportunity_observation
from tests.test_missed_opportunity_live_v426a import SNAPSHOT_TS, external, frame, frames, genesis_review, opportunity, terminal_review


def obs_payload(symbol: str = "BTC", obs: int = 70, price: float = 100.0, htf: int = 2, momentum: str = "MATURE_CONTINUATION", positioning: bool = True, liquidity: bool = False, short: bool = False):
    opp = opportunity(symbol, htf=htf, momentum=momentum)
    opp["snapshot_timestamp"] = SNAPSHOT_TS + obs * 300
    opp["truth"]["swing_bias"] = "BEARISH" if short else "BULLISH"
    if liquidity:
        opp["raw_metrics"]["LIQUIDITY"]["liquidity_distance_pct"] = 1.0
    ext = external(symbol, SNAPSHOT_TS + obs * 300)
    if not positioning:
        ext["domain_classification"]["POSITIONING"]["classification"] = "NEUTRAL"
    review = terminal_review(symbol)
    review["Review_Timestamp"] = str(SNAPSHOT_TS + obs * 300)
    if short:
        review["Swing_Bias"] = "BEARISH"
    return review, {tf: frame(symbol, start=SNAPSHOT_TS + obs * 300 - 3000, count=80, price=price) for tf in ("D1", "H4", "H1", "M15", "M5")}, opp, ext


def apply_dry(store, *, obs: int, price: float = 100.0, htf: int = 2, momentum: str = "MATURE_CONTINUATION", positioning: bool = True, liquidity: bool = False, short: bool = False):
    review, fr, opp, ext = obs_payload(obs=obs, price=price, htf=htf, momentum=momentum, positioning=positioning, liquidity=liquidity, short=short)
    return dry_run_missed_opportunity_observation(store=store, reviews={"BTC": review}, frames={"BTC": fr}, opportunity_snapshots={"BTC": opp}, external_evidence={"BTC": ext}, observation_number=obs)[0]


def synthetic_break_reentry_store():
    store = empty_store("fixed")
    store = apply_dry(store, obs=70, price=100)
    store = apply_dry(store, obs=71, price=101)
    store = apply_dry(store, obs=72, price=102, htf=0)
    store = apply_dry(store, obs=73, price=103, htf=0)
    store = apply_dry(store, obs=74, price=104)
    return store


class MissedOpportunityLiveV426a2Tests(unittest.TestCase):
    def test_matching_requires_symbol_and_direction(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=1, price=100)
        store = apply_dry(store, obs=2, price=99, short=True)
        self.assertEqual(len(store["records"]), 2)

    def test_long_tracker_never_absorbs_short_tracker(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=1, price=100)
        long_id = store["records"][0]["tracker_id"]
        store = apply_dry(store, obs=2, price=99, short=True)
        self.assertEqual([len(r["snapshots"]) for r in store["records"]], [1, 1])
        self.assertEqual(store["records"][0]["tracker_id"], long_id)

    def test_eligible_deterioration_remains_same_tracker(self) -> None:
        store = empty_store("fixed")
        for obs, price in [(60, 100), (61, 99), (62, 101)]:
            store = apply_dry(store, obs=obs, price=price)
        self.assertEqual(len(store["records"]), 1)
        self.assertEqual([s["observation_number"] for s in store["records"][0]["snapshots"]], [60, 61, 62])
        self.assertEqual(store["records"][0]["episode_status"], "OPEN")

    def test_first_noneligible_observation_is_final_break_snapshot(self) -> None:
        store = synthetic_break_reentry_store()
        first = store["records"][0]
        self.assertEqual([s["observation_number"] for s in first["snapshots"]], [70, 71, 72])

    def test_break_metadata_persisted(self) -> None:
        store = synthetic_break_reentry_store()
        first = store["records"][0]
        self.assertEqual(first["episode_status"], "BROKEN")
        self.assertEqual(first["context_break_observation"], 72)
        self.assertIn("STRUCTURE_LOST", first["context_break_reasons"])

    def test_next_noneligible_observation_not_appended(self) -> None:
        store = synthetic_break_reentry_store()
        first = store["records"][0]
        self.assertNotIn(73, [s["observation_number"] for s in first["snapshots"]])

    def test_eligible_reentry_creates_tracker_b(self) -> None:
        store = synthetic_break_reentry_store()
        self.assertEqual(len(store["records"]), 2)

    def test_tracker_b_id_differs(self) -> None:
        store = synthetic_break_reentry_store()
        self.assertNotEqual(store["records"][0]["tracker_id"], store["records"][1]["tracker_id"])

    def test_tracker_b_origin_is_reentry_observation(self) -> None:
        store = synthetic_break_reentry_store()
        self.assertEqual(store["records"][1]["origin_observation"], 74)
        self.assertEqual([s["observation_number"] for s in store["records"][1]["snapshots"]], [74])

    def test_old_tracker_outcomes_continue_after_break(self) -> None:
        store = synthetic_break_reentry_store()
        future_frame = frame("BTC", start=SNAPSHOT_TS + 71 * 300, count=400, price=100, latest_closed=SNAPSHOT_TS + 71 * 300 + 399 * 300)
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"ETH": terminal_review("ETH")}, frames={"ETH": {tf: future_frame for tf in ("D1", "H4", "H1", "M15", "M5")}}, opportunity_snapshots={"ETH": opportunity("ETH")}, external_evidence={"ETH": external("ETH")}, observation_number=80)
        self.assertIn(updated["records"][0]["outcomes"]["1H"]["horizon_status"], {"COMPLETE", "DATA_GAP", "PENDING"})

    def test_production_conversion_precedence(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=80, price=100)
        review, fr, opp, ext = obs_payload(obs=81, price=101)
        review = genesis_review("BTC")
        review["Review_Timestamp"] = str(SNAPSHOT_TS + 81 * 300)
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": review}, frames={"BTC": fr}, opportunity_snapshots={"BTC": opp}, external_evidence={"BTC": ext}, observation_number=81)
        self.assertEqual(updated["records"][0]["status"], "CONVERTED")
        self.assertEqual(updated["records"][0]["episode_status"], "CLOSED")
        self.assertEqual(updated["records"][0]["context_break_reason"], "PRODUCTION_CONVERSION")

    def test_no_append_after_conversion(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=80, price=100)
        review, fr, opp, ext = obs_payload(obs=81, price=101)
        review = genesis_review("BTC")
        review["Review_Timestamp"] = str(SNAPSHOT_TS + 81 * 300)
        store, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": review}, frames={"BTC": fr}, opportunity_snapshots={"BTC": opp}, external_evidence={"BTC": ext}, observation_number=81)
        store = apply_dry(store, obs=82, price=102)
        self.assertEqual(len(store["records"][0]["snapshots"]), 1)

    def test_later_independent_post_conversion_tracker_allowed(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=80, price=100)
        review, fr, opp, ext = obs_payload(obs=81, price=101)
        review = genesis_review("BTC")
        store, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": review}, frames={"BTC": fr}, opportunity_snapshots={"BTC": opp}, external_evidence={"BTC": ext}, observation_number=81)
        store = apply_dry(store, obs=82, price=102)
        self.assertEqual(len(store["records"]), 2)

    def test_46_49_remains_one_tracker(self) -> None:
        store = backfill_46_49("fixed")
        self.assertEqual(len([r for r in store["records"] if r["symbol"] == "BTC"]), 1)
        self.assertEqual(len([r for r in store["records"] if r["symbol"] == "ETH"]), 1)

    def test_dry_50_preserves_same_tracker(self) -> None:
        store = backfill_46_49("fixed")
        btc_id = next(r["tracker_id"] for r in store["records"] if r["symbol"] == "BTC")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=80350)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertEqual(btc["tracker_id"], btc_id)
        self.assertEqual(len(btc["snapshots"]), 5)

    def test_restart_preserves_broken_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            persist_tracker_store(path, synthetic_break_reentry_store())
            loaded = load_tracker_store(path)
            self.assertEqual(loaded["records"][0]["episode_status"], "BROKEN")

    def test_rerun_reentry_does_not_duplicate_tracker_b(self) -> None:
        store = synthetic_break_reentry_store()
        store = apply_dry(store, obs=74, price=104)
        self.assertEqual(len(store["records"]), 2)
        self.assertEqual([s["observation_number"] for s in store["records"][1]["snapshots"]], [74])

    def test_rerun_break_observation_does_not_duplicate_snapshot(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=70, price=100)
        store = apply_dry(store, obs=72, price=102, htf=0)
        store = apply_dry(store, obs=72, price=102, htf=0)
        self.assertEqual([s["observation_number"] for s in store["records"][0]["snapshots"]], [70, 72])

    def test_missing_episode_fields_backward_compatible(self) -> None:
        store = backfill_46_49("fixed")
        for record in store["records"]:
            record.pop("episode_status", None)
            record.pop("context_break_observation", None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(store), encoding="utf-8")
            loaded = load_tracker_store(path)
            self.assertEqual({r["episode_status"] for r in loaded["records"]}, {"OPEN"})

    def test_no_hindsight_boundary_ignores_future_outcomes(self) -> None:
        store = empty_store("fixed")
        store = apply_dry(store, obs=70, price=100)
        store["records"][0]["outcomes"]["1H"]["horizon_status"] = "COMPLETE"
        store["records"][0]["outcomes"]["1H"]["MFE_pct"] = 999
        store = apply_dry(store, obs=72, price=102, htf=0)
        self.assertEqual(store["records"][0]["context_break_observation"], 72)

    def test_production_isolation_input_review_unchanged(self) -> None:
        review, fr, opp, ext = obs_payload(obs=70)
        before = copy.deepcopy(review)
        dry_run_missed_opportunity_observation(store=empty_store("fixed"), reviews={"BTC": review}, frames={"BTC": fr}, opportunity_snapshots={"BTC": opp}, external_evidence={"BTC": ext}, observation_number=70)
        self.assertEqual(review, before)

    def test_no_execution_fields_in_episode_records(self) -> None:
        payload = json.dumps(synthetic_break_reentry_store()).lower()
        for key in ('"entry"', '"score"', '"grade"', '"sl"', '"tp"'):
            self.assertNotIn(key, payload)


if __name__ == "__main__":
    unittest.main()