from __future__ import annotations

import copy
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evaluation_summary import (  # noqa: E402
    build_evaluation_summary,
    generate_evaluation_summary,
)
from utils import load_race_json, save_race_json  # noqa: E402


def simulation_result(
    stake: int = 0,
    returned: int = 0,
    profit: int = 0,
    hit: bool = False,
) -> dict:
    return {
        "total_stake": stake,
        "total_return": returned,
        "profit": profit,
        "roi": None if not stake else profit / stake,
        "hit": hit,
    }


def evaluated_payload(
    race_id: str,
    *,
    winner_rank: int = 1,
    log_loss: float = 0.1,
    brier_score: float = 0.01,
    surface: str = "芝",
    distance: int = 1200,
    class_grade: str = "G3",
    track: str = "東京",
    field_size: int = 8,
    probabilities: list[float] | None = None,
    winner_number: int | None = None,
    market_available: bool = True,
    odds_recorded_after_start: bool = False,
    market_log_loss: float = 0.2,
    market_brier_score: float = 0.02,
    value_result: dict | None = None,
    dutching_result: dict | None = None,
) -> dict:
    if probabilities is None:
        weights = list(range(field_size, 0, -1))
        total = sum(weights)
        probabilities = [weight / total for weight in weights]
    field_size = len(probabilities)
    winner_number = winner_number or winner_rank
    ranked = sorted(
        enumerate(probabilities, 1),
        key=lambda item: (-item[1], item[0]),
    )
    actual_rank = next(index for index, item in enumerate(ranked, 1) if item[0] == winner_number)
    market = {"available": False}
    if market_available:
        market = {
            "available": True,
            "winner_probability": 0.15,
            "winner_rank": min(actual_rank + 1, field_size),
            "log_loss": market_log_loss,
            "brier_score": market_brier_score,
            "model_log_loss_difference": log_loss - market_log_loss,
            "model_brier_difference": brier_score - market_brier_score,
            "odds_recorded_after_start": odds_recorded_after_start,
            "comparison_note": None,
        }
    return {
        "meta": {"race_id": race_id, "schema_version": 5},
        "race": {
            "date": "2026-01-01",
            "track": track,
            "race_number": 11,
            "race_name": race_id,
            "surface": surface,
            "distance": distance,
            "class_grade": class_grade,
        },
        "horses": [
            {
                "horse_number": number,
                "horse_name": f"Horse {number}",
                "win_odds": float(number + 1),
            }
            for number in range(1, field_size + 1)
        ],
        "prediction": {
            "model_provider": "codex",
            "model_name": "gpt-test",
            "horses": [
                {
                    "horse_number": number,
                    "win_probability": probability,
                    "reason": "test",
                }
                for number, probability in enumerate(probabilities, 1)
            ],
        },
        "simulation": {
            "value": {"pre": None, "post": None},
            "dutching": {"pre": None, "post": None},
        },
        "result": {"horses": [{"horse_number": winner_number, "finish_position": 1}]},
        "evaluation": {
            "winner": {
                "horse_number": winner_number,
                "predicted_probability": probabilities[winner_number - 1],
                "predicted_rank": actual_rank,
            },
            "metrics": {
                "log_loss": log_loss,
                "brier_score": brier_score,
                "top1_hit": actual_rank <= 1,
                "top3_hit": actual_rank <= 3,
                "top5_hit": actual_rank <= 5,
            },
            "market_baseline": market,
            "simulation_results": {
                "value": value_result or simulation_result(),
                "dutching": dutching_result or simulation_result(),
            },
        },
    }


class EvaluationSummaryTests(unittest.TestCase):
    def test_zero_evaluations_are_not_reported_as_zero_accuracy(self) -> None:
        summary = build_evaluation_summary(
            [{"evaluation": None}, {"evaluation": {"winner": {}, "metrics": {}}}],
            "2026-01-01T00:00:00+09:00",
        )

        self.assertEqual(summary["overall"]["evaluated_races"], 0)
        self.assertIsNone(summary["overall"]["top1_hit_rate"])
        self.assertIsNone(summary["overall"]["average_log_loss"])
        self.assertEqual(summary["market_comparison"]["comparable_races"], 0)
        self.assertTrue(all(item["samples"] == 0 for item in summary["calibration"]))
        self.assertTrue(all(item["actual_win_rate"] is None for item in summary["calibration"]))
        self.assertEqual(summary["segments"]["surface"], {})
        self.assertIsNone(summary["simulation"]["value"]["overall_roi"])

    def test_overall_and_segment_metrics_use_existing_evaluations(self) -> None:
        payloads = [
            evaluated_payload("one", winner_rank=1, log_loss=0.1, brier_score=0.01, field_size=8),
            evaluated_payload(
                "two",
                winner_rank=3,
                log_loss=0.3,
                brier_score=0.03,
                surface="ダート",
                distance=1700,
                class_grade="G2",
                track="中山",
                field_size=11,
            ),
            evaluated_payload(
                "three",
                winner_rank=6,
                log_loss=0.5,
                brier_score=0.05,
                distance=2300,
                class_grade="G1",
                field_size=17,
            ),
        ]

        summary = build_evaluation_summary(payloads, "2026-01-01T00:00:00+09:00")
        overall = summary["overall"]

        self.assertEqual(overall["evaluated_races"], 3)
        self.assertEqual((overall["top1_hits"], overall["top3_hits"], overall["top5_hits"]), (1, 2, 2))
        self.assertEqual(overall["top1_hit_rate"], 0.333333)
        self.assertEqual(overall["average_winner_predicted_rank"], 3.333333)
        self.assertEqual(overall["average_log_loss"], 0.3)
        self.assertEqual(overall["average_brier_score"], 0.03)
        self.assertEqual(summary["segments"]["surface"]["芝"]["evaluated_races"], 2)
        self.assertEqual(summary["segments"]["surface"]["ダート"]["evaluated_races"], 1)
        self.assertEqual(summary["segments"]["distance_band"]["～1400m"]["top1_hits"], 1)
        self.assertEqual(summary["segments"]["distance_band"]["1401～1800m"]["top3_hits"], 1)
        self.assertEqual(summary["segments"]["distance_band"]["2201m～"]["top5_hits"], 0)
        self.assertEqual(summary["segments"]["class_grade"]["G1"]["evaluated_races"], 1)
        self.assertEqual(summary["segments"]["track"]["東京"]["evaluated_races"], 2)
        self.assertEqual(summary["segments"]["field_size_band"]["～9頭"]["evaluated_races"], 1)
        self.assertEqual(summary["segments"]["field_size_band"]["10～13頭"]["evaluated_races"], 1)
        self.assertEqual(summary["segments"]["field_size_band"]["17頭以上"]["evaluated_races"], 1)

    def test_market_comparison_excludes_unavailable_and_after_start_odds(self) -> None:
        before_one = evaluated_payload(
            "before-one",
            log_loss=0.4,
            brier_score=0.04,
            market_log_loss=0.5,
            market_brier_score=0.05,
            field_size=3,
        )
        before_two = evaluated_payload(
            "before-two",
            log_loss=0.2,
            brier_score=0.02,
            market_log_loss=0.4,
            market_brier_score=0.03,
            field_size=3,
        )
        unavailable = evaluated_payload("unavailable", market_available=False, field_size=3)
        after_start = evaluated_payload(
            "after-start",
            odds_recorded_after_start=True,
            field_size=3,
        )

        summary = build_evaluation_summary(
            [before_one, before_two, unavailable, after_start],
            "2026-01-01T00:00:00+09:00",
        )
        market = summary["market_comparison"]

        self.assertEqual(market["comparable_races"], 2)
        self.assertEqual(market["model_average_log_loss"], 0.3)
        self.assertEqual(market["market_average_log_loss"], 0.45)
        self.assertEqual(market["average_log_loss_difference"], -0.15)
        self.assertEqual(market["model_average_brier_score"], 0.03)
        self.assertEqual(market["market_average_brier_score"], 0.04)
        self.assertEqual(market["average_brier_score_difference"], -0.01)
        characteristics = summary["market_characteristics"]
        self.assertEqual(characteristics["race_count"], 2)
        self.assertEqual(
            {item["race_id"] for item in characteristics["races"]},
            {"before-one", "before-two"},
        )
        self.assertTrue(all(item["market_top1_probability"] > 0 for item in characteristics["races"]))
        self.assertTrue(all(item["market_top3_probability"] == 1.0 for item in characteristics["races"]))

    def test_calibration_probability_boundaries(self) -> None:
        payloads = [
            evaluated_payload(
                "boundaries",
                probabilities=[0.0, 0.05, 0.10, 0.20, 0.30, 0.35],
                winner_number=4,
            ),
            evaluated_payload("half", probabilities=[0.5, 0.5], winner_number=1),
            evaluated_payload("certain", probabilities=[1.0, 0.0], winner_number=1),
        ]

        summary = build_evaluation_summary(payloads, "2026-01-01T00:00:00+09:00")
        buckets = {item["range"]: item for item in summary["calibration"]}

        self.assertEqual((buckets["0-5%"]["samples"], buckets["0-5%"]["wins"]), (2, 0))
        self.assertEqual((buckets["5-10%"]["samples"], buckets["5-10%"]["wins"]), (1, 0))
        self.assertEqual((buckets["10-20%"]["samples"], buckets["10-20%"]["wins"]), (1, 0))
        self.assertEqual((buckets["20-30%"]["samples"], buckets["20-30%"]["wins"]), (1, 1))
        self.assertEqual((buckets["30-50%"]["samples"], buckets["30-50%"]["wins"]), (2, 0))
        self.assertEqual((buckets["50-100%"]["samples"], buckets["50-100%"]["wins"]), (3, 2))
        self.assertEqual(buckets["20-30%"]["average_predicted_probability"], 0.2)
        self.assertEqual(buckets["20-30%"]["actual_win_rate"], 1.0)
        self.assertEqual(buckets["50-100%"]["actual_win_rate"], 0.666667)

    def test_simulation_totals_use_cumulative_stake_and_return(self) -> None:
        payloads = [
            evaluated_payload(
                "hit",
                value_result=simulation_result(100, 300, 200, True),
                dutching_result=simulation_result(),
            ),
            evaluated_payload(
                "miss",
                value_result=simulation_result(200, 0, -200, False),
                dutching_result=simulation_result(300, 600, 300, True),
            ),
            evaluated_payload("none"),
        ]

        summary = build_evaluation_summary(payloads, "2026-01-01T00:00:00+09:00")

        self.assertEqual(
            summary["simulation"]["value"],
            {
                "simulation_races": 3,
                "purchase_races": 2,
                "hit_races": 1,
                "total_stake": 300,
                "total_return": 300,
                "cumulative_profit": 0,
                "overall_roi": 1.0,
            },
        )
        self.assertEqual(summary["simulation"]["dutching"]["purchase_races"], 1)
        self.assertEqual(summary["simulation"]["dutching"]["hit_races"], 1)
        self.assertEqual(summary["simulation"]["dutching"]["total_stake"], 300)
        self.assertEqual(summary["simulation"]["dutching"]["total_return"], 600)
        self.assertEqual(summary["simulation"]["dutching"]["overall_roi"], 2.0)

    def test_summary_file_is_regenerated_without_changing_race_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            race_path = root / "data" / "races" / "2026-01-01" / "tokyo_11r.json"
            save_race_json(race_path, evaluated_payload("saved"))
            before = race_path.read_bytes()
            config = {"data_dir": "data", "public_dir": "public"}

            output = generate_evaluation_summary(config, "test-summary", root)
            summary = load_race_json(race_path)
            generated = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(output, root / "data" / "evaluation_summary.json")
            self.assertTrue(output.exists())
            self.assertEqual(race_path.read_bytes(), before)
            self.assertIsNotNone(summary["evaluation"])
            self.assertEqual(generated["overall"]["evaluated_races"], 1)
            self.assertEqual(generated["overall"]["top1_hit_rate"], 1.0)
            self.assertEqual(
                build_evaluation_summary([copy.deepcopy(summary)])["overall"]["evaluated_races"],
                1,
            )
            logger = logging.getLogger("keiba_oracle.test-summary")
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
