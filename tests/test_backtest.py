from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import backtest  # noqa: E402


def make_config(
    *,
    ev_threshold: float = 1.0,
    min_coverage_probability: float = 0.0,
) -> dict:
    return {
        "data_dir": "data",
        "simulation": {
            "budget": 3000,
            "stake_unit": 100,
            "value": {
                "ev_threshold": ev_threshold,
                "kelly_fraction": 0.75,
            },
            "dutching": {
                "max_selection_count": 2,
                "min_coverage_probability": min_coverage_probability,
                "min_group_expected_value": 0.0,
                "min_profit_rate": 0.2,
                "require_profit_if_hit": True,
            },
        },
    }


def make_payload(*, include_statistical: bool = True, include_result: bool = True) -> dict:
    prediction = {
        "horses": [
            {"horse_number": 1, "win_probability": 0.6, "reason": "traditional 1"},
            {"horse_number": 2, "win_probability": 0.4, "reason": "traditional 2"},
        ],
        "variants": [],
    }
    if include_statistical:
        prediction["variants"].append(
            {
                "method": "statistical",
                "horses": [
                    {"horse_number": 1, "win_probability": 0.35, "reason": "statistical 1"},
                    {"horse_number": 2, "win_probability": 0.65, "reason": "statistical 2"},
                ],
            }
        )
    result = None
    if include_result:
        result = {
            "horses": [
                {"horse_number": 1, "finish_position": 1},
                {"horse_number": 2, "finish_position": 2},
            ],
            "payouts": {
                "win": [{"horse_number": 1, "payout_per_100": 400}],
            },
            "final_win_odds": [
                {"horse_number": 1, "win_odds": 100.0},
                {"horse_number": 2, "win_odds": 1.1},
            ],
        }
    return {
        "meta": {"race_id": "test-race", "schema_version": 8},
        "race": {"date": "2026-01-01", "track": "中山", "race_number": 11},
        "horses": [
            {"horse_number": 1, "horse_name": "Horse 1", "win_odds": 4.0},
            {"horse_number": 2, "horse_name": "Horse 2", "win_odds": 5.0},
        ],
        "prediction": prediction,
        "simulation": {
            "value": {"pre": {"saved": True}, "post": {"saved": True}},
            "dutching": {"pre": {"saved": True}, "post": {"saved": True}},
        },
        "result": result,
        "evaluation": None,
    }


def write_race(root: Path, name: str, payload: dict) -> Path:
    path = root / "data" / "races" / "2026-01-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class BacktestTests(unittest.TestCase):
    def test_reuses_simulation_functions_without_writing_race_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            race_path = write_race(root, "nakayama_11r.json", make_payload())
            before = race_path.read_bytes()
            value_pre = backtest.calculate_value_pre
            dutching_pre = backtest.calculate_dutching_pre
            calculate_post = backtest.calculate_post

            with (
                patch.object(backtest, "calculate_value_pre", wraps=value_pre) as value_mock,
                patch.object(backtest, "calculate_dutching_pre", wraps=dutching_pre) as dutching_mock,
                patch.object(backtest, "calculate_post", wraps=calculate_post) as post_mock,
            ):
                report = backtest.run_backtest(make_config(), root)

            self.assertEqual(race_path.read_bytes(), before)
            self.assertEqual(value_mock.call_count, 2)
            self.assertEqual(dutching_mock.call_count, 2)
            self.assertEqual(post_mock.call_count, 4)
            self.assertEqual(report["methods"]["traditional"]["value"]["target_races"], 1)
            self.assertEqual(report["methods"]["statistical"]["value"]["target_races"], 1)
            self.assertEqual(report["methods"]["traditional"]["dutching"]["hit_races"], 1)
            self.assertEqual(report["methods"]["statistical"]["dutching"]["hit_races"], 0)

    def test_zero_stake_is_target_but_not_purchase_and_missing_variant_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_race(
                root,
                "nakayama_11r.json",
                make_payload(include_statistical=False),
            )
            write_race(
                root,
                "tokyo_11r.json",
                make_payload(include_statistical=False, include_result=False),
            )

            report = backtest.run_backtest(
                make_config(ev_threshold=100.0, min_coverage_probability=1.1),
                root,
            )

            traditional = report["methods"]["traditional"]
            self.assertEqual(traditional["value"]["target_races"], 1)
            self.assertEqual(traditional["value"]["purchased_races"], 0)
            self.assertEqual(traditional["dutching"]["target_races"], 1)
            self.assertEqual(traditional["dutching"]["purchased_races"], 0)
            self.assertEqual(traditional["excluded"], {"result_missing": 1})

            statistical = report["methods"]["statistical"]
            self.assertEqual(statistical["value"]["target_races"], 0)
            self.assertEqual(
                statistical["excluded"],
                {"statistical_prediction_missing": 1, "result_missing": 1},
            )
            self.assertIsNone(traditional["value"]["return_rate"])
            self.assertIsNone(traditional["dutching"]["return_rate"])


if __name__ == "__main__":
    unittest.main()
