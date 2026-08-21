from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from market_reviewer.persistence import PERSISTENCE_VERSION, STATE_SCHEMA, atomic_write_json, load_review_state
from market_reviewer.pipeline import review_snapshot


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW3 = FIXTURES / "review3" / "market-data-v1.json"
REVIEW4 = FIXTURES / "review4" / "market-data-v1.json"


def persisted_v2_state() -> dict:
    return {
        "persistence_version": PERSISTENCE_VERSION,
        "state_schema": STATE_SCHEMA,
        "symbols": {
            "BTC": {
                "previous_bias": "BULLISH",
                "sequence_state": "SEEKING_LIQUIDITY",
                "active_tactical_draw": {
                    "price": 63981.0,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787058000,
                    "selected_at": 1787076000,
                    "status": "NONE",
                },
            },
            "ETH": {
                "previous_bias": "BULLISH",
                "sequence_state": "SEEKING_LIQUIDITY",
                "active_tactical_draw": {
                    "price": 1891.81,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787011200,
                    "selected_at": 1787076000,
                    "status": "NONE",
                },
                "candidate_tactical_draw": {
                    "price": 1909.66,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787076000,
                    "detected_at": 1787076000,
                    "status": "APPROACHED",
                },
            },
        },
    }


class PersistenceV411Tests(unittest.TestCase):
    def test_legacy_state_migrates_and_marks_active_draw_initialization(self) -> None:
        legacy = {
            "BTC": {"previous_bias": "BULLISH", "previous_state": "WAIT"},
            "ETH": {"previous_bias": "BULLISH", "previous_state": "WAIT"},
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            reviews = review_snapshot(REVIEW3, state_path)
            state, loaded_from = load_review_state(state_path)
        self.assertEqual(loaded_from, STATE_SCHEMA)
        self.assertEqual(reviews["BTC"]["State_Loaded_From"], "LEGACY_MIGRATED")
        self.assertTrue(any(item.get("reason") == "ACTIVE_DRAW_INITIALIZED_FROM_LEGACY_STATE" for item in state["BTC"].get("target_transition_history", [])))
        self.assertEqual(state["BTC"]["persistence_version"], PERSISTENCE_VERSION)
        self.assertIn("active_tactical_draw", state["BTC"])

    def test_restart_regression_uses_persisted_review3_state_for_review4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, persisted_v2_state())
            review_snapshot(REVIEW3, state_path)
            restarted_reviews = review_snapshot(REVIEW4, state_path)
        btc = restarted_reviews["BTC"]
        eth = restarted_reviews["ETH"]
        self.assertIn("63981.00", btc["Active_Tactical_Draw"])
        self.assertIn("1891.81", eth["Active_Tactical_Draw"])
        self.assertIn("1909.77", eth["Candidate_Tactical_Draw"])
        self.assertEqual(eth["Candidate_Draw_Status"], "APPROACHED")
        self.assertEqual(btc["Target_Changed"], "NO")
        self.assertEqual(eth["Target_Changed"], "NO")
        self.assertEqual(btc["Sequence_State"], "SEEKING_LIQUIDITY")
        self.assertEqual(eth["Sequence_State"], "SEEKING_LIQUIDITY")
        self.assertEqual(btc["State"], "WAIT")
        self.assertEqual(eth["State"], "WAIT")
        self.assertEqual(eth["Persistence_Version"], STATE_SCHEMA)
        self.assertEqual(eth["State_Loaded_From"], STATE_SCHEMA)

    def test_historical_sweep_after_restart_is_rejected_by_selected_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, persisted_v2_state())
            review_snapshot(REVIEW3, state_path)
            reviews = review_snapshot(REVIEW4, state_path)
        eth = reviews["ETH"]
        self.assertEqual(eth["Active_Draw_Status"], "NONE")
        self.assertEqual(eth["Liquidity_Event"], "NONE")
        self.assertEqual(eth["Sequence_State"], "SEEKING_LIQUIDITY")

    def test_atomic_write_json_replaces_complete_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"persistence_version": 1, "symbols": {}})
            atomic_write_json(path, persisted_v2_state())
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["persistence_version"], PERSISTENCE_VERSION)
        self.assertIn("BTC", loaded["symbols"])
        self.assertIn("ETH", loaded["symbols"])


if __name__ == "__main__":
    unittest.main()
