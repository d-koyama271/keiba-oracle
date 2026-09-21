from __future__ import annotations

import copy
import logging
import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evaluation import build_evaluation, evaluate_file  # noqa: E402
from utils import ensure_race_payload, load_race_json, save_race_json  # noqa: E402


def make_payload(*, missing_odds: bool = False) -> dict:
    return {
        "meta": {
            "race_id": "202606010111",
            "schema_version": 4,
            "created_at": "2026-01-01T00:00:00+09:00",
            "updated_at": "2026-01-01T00:00:00+09:00",
            "pre_status": "published",
            "post_status": "awaiting_result",
        },
        "race": {
            "date": "2026-01-01",
            "track": "中山",
            "race_number": 11,
            "race_name": "評価テスト",
            "start_time": "15:30",
            "odds_captured_at": "2026-01-01T15:35:00+09:00",
            "source_url": "https://example.invalid/race",
        },
        "horses": [
            {"horse_number": 1, "horse_name": "A", "win_odds": 2.0},
            {"horse_number": 2, "horse_name": "B", "win_odds": None if missing_odds else 4.0},
            {"horse_number": 3, "horse_name": "C", "win_odds": 5.0},
        ],
        "prediction": {
            "horses": [
                {"horse_number": 1, "win_probability": 0.4, "reason": "A"},
                {"horse_number": 2, "win_probability": 0.4, "reason": "B"},
                {"horse_number": 3, "win_probability": 0.2, "reason": "C"},
            ]
        },
        "simulation": {
            "value": {
                "pre": {"selections": []},
                "post": {
                    "total_stake": 0,
                    "total_return": 0,
                    "profit": 0,
                    "roi": 0.0,
                    "selections": [],
                },
            },
            "dutching": {
                "pre": {"selections": [{"horse_number": 2, "stake": 1000}]},
                "post": {
                    "total_stake": 1000,
                    "total_return": 4000,
                    "profit": 3000,
                    "roi": 3.0,
                    "selections": [
                        {"horse_number": 2, "stake": 1000, "hit": True, "return": 4000}
                    ],
                },
            },
        },
        "result": {
            "finish_order": [2, 1, 3],
            "horses": [
                {"horse_number": 2, "finish_position": 1},
                {"horse_number": 1, "finish_position": 2},
                {"horse_number": 3, "finish_position": 3},
            ],
            "payouts": {"win": [{"horse_number": 2, "payout_per_100": 400}]},
        },
        "evaluation": None,
    }


def test_config(root: Path) -> dict:
    return {
        "data_dir": str(root / "data"),
        "public_dir": str(root / "public"),
    }


def close_logger(name: str) -> None:
    logger = logging.getLogger(f"keiba_oracle.{name}")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


class EvaluationMetricTests(unittest.TestCase):
    def test_metrics_market_simulation_and_tie_ranking(self) -> None:
        evaluation = build_evaluation(make_payload())

        self.assertEqual(
            evaluation[0]["general"]["winner"],
            {"horse_number": 2, "predicted_probability": 0.4, "predicted_rank": 2},
        )
        self.assertAlmostEqual(evaluation[0]["general"]["metrics"]["log_loss"], -math.log(0.4), places=6)
        self.assertEqual(evaluation[0]["general"]["metrics"]["brier_score"], 0.186667)
        self.assertFalse(evaluation[0]["general"]["metrics"]["top1_hit"])
        self.assertTrue(evaluation[0]["general"]["metrics"]["top3_hit"])
        self.assertTrue(evaluation[0]["general"]["metrics"]["top5_hit"])

        market = evaluation[0]["general"]["market_baseline"]
        self.assertTrue(market["available"])
        self.assertEqual(market["winner_rank"], 2)
        self.assertEqual(market["winner_probability"], 0.263158)
        self.assertEqual(
            market["model_log_loss_difference"],
            round(evaluation[0]["general"]["metrics"]["log_loss"] - market["log_loss"], 6),
        )
        self.assertEqual(
            market["model_brier_difference"],
            round(evaluation[0]["general"]["metrics"]["brier_score"] - market["brier_score"], 6),
        )
        self.assertTrue(market["odds_recorded_after_start"])
        self.assertIsNotNone(market["comparison_note"])

        self.assertEqual(
            evaluation[0]["general"]["simulation_results"]["value"],
            {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None, "hit": False},
        )
        self.assertEqual(
            evaluation[0]["general"]["simulation_results"]["dutching"],
            {"total_stake": 1000, "total_return": 4000, "profit": 3000, "roi": 3.0, "hit": True},
        )

    def test_market_is_unavailable_when_any_odds_are_missing(self) -> None:
        evaluation = build_evaluation(make_payload(missing_odds=True))

        self.assertEqual(evaluation[0]["general"]["market_baseline"], {"available": False})

    def test_statistical_variant_uses_same_metrics_without_simulation_results(self) -> None:
        payload = make_payload()
        payload["prediction"]["variants"] = [
            {
                "method": "statistical",
                "model_provider": "codex",
                "model_name": "gpt-test",
                "predicted_at": "2025-12-31T15:00:00+09:00",
                "prompt_sha256": "prompt-hash",
                "prediction_input_sha256": "input-hash",
                "horses": [
                    {"horse_number": 1, "win_probability": 0.2, "reason": "A"},
                    {"horse_number": 2, "win_probability": 0.6, "reason": "B"},
                    {"horse_number": 3, "win_probability": 0.2, "reason": "C"},
                ],
            }
        ]

        evaluation = build_evaluation(payload)
        statistical = evaluation[-1]["statistical"]

        self.assertEqual(evaluation[-1]["prediction_id"], "p2")
        self.assertEqual(
            statistical["winner"],
            {"horse_number": 2, "predicted_probability": 0.6, "predicted_rank": 1},
        )
        self.assertTrue(statistical["metrics"]["top1_hit"])
        self.assertTrue(statistical["metrics"]["top3_hit"])
        self.assertTrue(statistical["metrics"]["top5_hit"])
        self.assertAlmostEqual(statistical["metrics"]["log_loss"], -math.log(0.6), places=6)
        self.assertEqual(statistical["metrics"]["brier_score"], 0.08)
        self.assertTrue(statistical["market_baseline"]["available"])
        self.assertNotIn("simulation_results", statistical)
        self.assertIn("simulation_results", evaluation[0]["general"])

    def test_evaluate_file_preserves_prediction_result_and_pre(self) -> None:
        payload = make_payload()
        prediction_before = copy.deepcopy(payload["prediction"])
        result_before = copy.deepcopy(payload["result"])
        pre_before = {
            "value": copy.deepcopy(payload["simulation"]["value"]["pre"]),
            "dutching": copy.deepcopy(payload["simulation"]["dutching"]["pre"]),
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "race.json"
            save_race_json(path, payload)
            try:
                self.assertTrue(evaluate_file(path, test_config(root), "test-evaluation", root))
                loaded = load_race_json(path)
            finally:
                close_logger("test-evaluation")

        self.assertEqual(loaded["prediction"], ensure_race_payload({"prediction": prediction_before})["prediction"])
        self.assertEqual(loaded["result"], result_before)
        self.assertEqual(loaded["simulation"][0]["general"]["win"]["value"]["pre"], pre_before["value"])
        self.assertEqual(loaded["simulation"][0]["general"]["win"]["dutching"]["pre"], pre_before["dutching"])
        self.assertIsNotNone(loaded["evaluation"])
        self.assertNotIn("feedback", loaded)


if __name__ == "__main__":
    unittest.main()
