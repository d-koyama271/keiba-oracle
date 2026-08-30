from __future__ import annotations

import copy
import html
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evaluation import build_evaluation  # noqa: E402
from render import build_environment, build_race_context  # noqa: E402
from simulate import (  # noqa: E402
    calculate_dutching_post,
    calculate_dutching_pre,
    calculate_post,
    calculate_pre_simulation,
    calculate_value_details,
    calculate_value_post,
    calculate_value_pre,
    evaluate_dutching_count,
    minimum_budget_for_value_stake,
    select_best_dutching,
    simulate_file,
)
from utils import load_config, load_race_json, save_race_json  # noqa: E402


def make_config(
    *,
    budget: int = 3000,
    stake_unit: int = 100,
    ev_threshold: float = 1.0,
    kelly_fraction: float = 0.5,
    max_selection_count: int = 5,
    min_coverage_probability: float = 0.4,
    min_group_expected_value: float = 0.0,
    min_profit_rate: float = 0.20,
    require_profit_if_hit: bool = True,
) -> dict:
    return {
        "simulation": {
            "budget": budget,
            "stake_unit": stake_unit,
            "value": {
                "ev_threshold": ev_threshold,
                "kelly_fraction": kelly_fraction,
            },
            "dutching": {
                "max_selection_count": max_selection_count,
                "min_coverage_probability": min_coverage_probability,
                "min_group_expected_value": min_group_expected_value,
                "min_profit_rate": min_profit_rate,
                "require_profit_if_hit": require_profit_if_hit,
            },
        }
    }


def make_payload(rows: list[tuple[int, float, float]]) -> dict:
    return {
        "meta": {
            "race_id": "test-race",
            "schema_version": 4,
            "created_at": "2026-01-01T00:00:00+09:00",
            "updated_at": "2026-01-01T00:00:00+09:00",
        },
        "race": {
            "date": "2026-01-01",
            "track": "中山",
            "race_number": 11,
            "race_name": "検証レース",
            "start_time": "15:30",
            "source_url": "https://example.invalid/race",
        },
        "horses": [
            {
                "horse_number": number,
                "horse_name": f"Horse {number}",
                "jockey": f"Jockey {number}",
                "weight_carried": 54.0 + number,
                "running_style_summary": f"Style {number}",
                "win_odds": odds,
                "popularity": number,
            }
            for number, _, odds in rows
        ],
        "prediction": {
            "horses": [
                {
                    "horse_number": number,
                    "win_probability": probability,
                    "reason": f"reason {number}",
                }
                for number, probability, _ in rows
            ]
        },
        "simulation": {
            "value": {"pre": None, "post": None},
            "dutching": {"pre": None, "post": None},
        },
        "result": None,
        "evaluation": None,
    }


def make_result(winner: int, payout_per_100: int, horse_numbers: list[int]) -> dict:
    ordered = [winner] + [number for number in horse_numbers if number != winner]
    return {
        "finish_order": ordered,
        "horses": [
            {"horse_number": number, "finish_position": index + 1}
            for index, number in enumerate(ordered)
        ],
        "payouts": {
            "win": [{"horse_number": winner, "payout_per_100": payout_per_100}],
        },
    }


DUTCHING_ROWS = [
    (1, 0.30, 4.0),
    (2, 0.25, 5.0),
    (3, 0.20, 6.0),
    (4, 0.15, 8.0),
    (5, 0.10, 12.0),
]


class ValueSimulationTests(unittest.TestCase):
    def test_app_config_value_defaults_apply_to_both_prediction_methods(self) -> None:
        config = load_config(ROOT / "config" / "app.yaml")

        self.assertEqual(float(config["simulation"]["value"]["ev_threshold"]), 1.0)
        self.assertEqual(float(config["simulation"]["value"]["kelly_fraction"]), 0.75)

        payload = make_payload(DUTCHING_ROWS)
        payload["prediction"]["variants"] = [
            {
                "method": "statistical",
                "model_provider": "codex",
                "model_name": "gpt-test",
                "horses": [
                    {
                        "horse_number": number,
                        "win_probability": probability,
                        "reason": f"statistical reason {number}",
                    }
                    for number, probability, _ in DUTCHING_ROWS
                ],
            }
        ]
        payload["simulation"] = calculate_pre_simulation(payload, config)

        self.assertEqual(
            payload["simulation"]["value"]["pre"]["settings"]["kelly_fraction"],
            0.75,
        )
        self.assertEqual(
            payload["simulation"]["variants"][0]["value"]["pre"]["settings"]["kelly_fraction"],
            0.75,
        )

        context = build_race_context(payload)
        context.update(
            {
                "page_kind": "prediction",
                "prediction_page_name": "test_11r.html",
                "result_page_name": None,
                "status_label": "予想公開",
                "status_class": "status-prediction",
            }
        )
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**context)
        custom_kelly = BeautifulSoup(rendered, "html.parser").select_one(
            '#custom-kelly-fraction'
        )
        self.assertEqual(custom_kelly["value"], "0.75")

    def test_ev_boundary_and_single_candidate_are_included(self) -> None:
        payload = make_payload([(1, 0.35, 3.0), (2, 0.10, 2.0)])
        result = calculate_value_pre(payload, make_config(budget=10000, ev_threshold=1.05))

        self.assertIsNotNone(result)
        self.assertEqual([item["horse_number"] for item in result["selections"]], [1])
        self.assertEqual(result["selections"][0]["expected_value"], 1.05)
        self.assertEqual(result["selections"][0]["stake"], 100)

    def test_below_threshold_zero_kelly_and_no_purchase(self) -> None:
        below = calculate_value_pre(make_payload([(1, 0.30, 3.0)]), make_config())
        zero_kelly = calculate_value_pre(
            make_payload([(1, 0.40, 3.0)]),
            make_config(kelly_fraction=0.0),
        )

        self.assertEqual(below["selections"], [])
        self.assertEqual(below["total_stake"], 0)
        self.assertEqual(below["unused_budget"], below["budget"])
        self.assertEqual(zero_kelly["selections"], [])

    def test_ev_thresholds_below_one_keep_non_positive_kelly_unselected(self) -> None:
        payload = make_payload([(1, 0.20, 4.0), (2, 0.40, 3.0)])
        for threshold in (0, 0.5, 0.99, 1.0, 1.05):
            result = calculate_value_pre(payload, make_config(ev_threshold=threshold))

            self.assertEqual([item["horse_number"] for item in result["selections"]], [2])
            self.assertTrue(all(item["full_kelly"] >= 0 for item in result["selections"]))
            self.assertTrue(all(item["fractional_kelly"] >= 0 for item in result["selections"]))
            self.assertTrue(all(item["stake"] >= 0 for item in result["selections"]))

        no_edge = calculate_value_pre(make_payload([(1, 0.20, 4.0)]), make_config(ev_threshold=0))
        self.assertEqual(no_edge["selections"], [])
        self.assertEqual(no_edge["total_stake"], 0)

    def test_kelly_changes_stake_without_forcing_full_budget(self) -> None:
        totals = []
        for fraction in (0.25, 0.5, 1.0):
            result = calculate_value_pre(
                make_payload([(1, 0.40, 3.0)]),
                make_config(budget=10000, kelly_fraction=fraction),
            )
            totals.append(result["total_stake"])

        self.assertEqual(totals, [200, 500, 1000])
        self.assertLess(totals[-1], 10000)

    def test_scaling_units_zero_stakes_and_budget_cap(self) -> None:
        under = calculate_value_pre(
            make_payload([(1, 0.40, 3.0), (2, 0.35, 4.0)]),
            make_config(),
        )
        over = calculate_value_pre(
            make_payload([(1, 0.60, 100.0), (2, 0.40, 100.0)]),
            make_config(kelly_fraction=2.0),
        )
        below_unit = calculate_value_pre(
            make_payload([(1, 0.02, 60.0)]),
            make_config(),
        )

        self.assertEqual(under["total_stake"], 300)
        self.assertEqual(under["unused_budget"], 2700)
        self.assertLessEqual(over["total_stake"], over["budget"])
        self.assertEqual(below_unit["selections"], [])
        for result in (under, over):
            self.assertTrue(all(item["stake"] > 0 for item in result["selections"]))
            self.assertTrue(all(item["stake"] % 100 == 0 for item in result["selections"]))

    def test_theoretical_stake_and_minimum_budget_boundary(self) -> None:
        payload = make_payload([(1, 0.02, 60.0)])
        config = make_config()
        settings = config["simulation"]["value"]

        detail = calculate_value_details(payload, 3000, 100, settings)[0]
        doubled = calculate_value_details(payload, 6000, 100, settings)[0]
        minimum_budget = minimum_budget_for_value_stake(payload, 100, settings, 1)

        self.assertAlmostEqual(detail["full_kelly"], 0.0033898305084745753)
        self.assertAlmostEqual(detail["fractional_kelly"], 0.0016949152542372877)
        self.assertAlmostEqual(detail["theoretical_stake"], 5.084745762711863)
        self.assertAlmostEqual(doubled["theoretical_stake"], detail["theoretical_stake"] * 2)
        self.assertEqual(detail["stake"], 0)
        self.assertEqual(minimum_budget, 59000)
        self.assertEqual(
            calculate_value_pre(payload, make_config(budget=minimum_budget))["selections"][0]["stake"],
            100,
        )
        self.assertEqual(
            calculate_value_pre(payload, make_config(budget=minimum_budget - 1))["selections"],
            [],
        )

    def test_minimum_budget_uses_scaled_multi_candidate_result(self) -> None:
        payload = make_payload([(1, 0.60, 100.0), (2, 0.40, 100.0)])
        config = make_config(kelly_fraction=2.0)
        settings = config["simulation"]["value"]
        details = calculate_value_details(payload, 3000, 100, settings)

        self.assertAlmostEqual(sum(item["theoretical_stake"] for item in details), 3000.0)
        self.assertEqual([item["stake"] for item in details], [1800, 1100])
        for horse_number, minimum_budget in ((1, 167), (2, 252)):
            self.assertEqual(
                minimum_budget_for_value_stake(payload, 100, settings, horse_number),
                minimum_budget,
            )
            at_boundary = calculate_value_pre(
                payload,
                make_config(budget=minimum_budget, kelly_fraction=2.0),
            )
            below_boundary = calculate_value_pre(
                payload,
                make_config(budget=minimum_budget - 1, kelly_fraction=2.0),
            )
            self.assertIn(horse_number, [item["horse_number"] for item in at_boundary["selections"]])
            self.assertNotIn(horse_number, [item["horse_number"] for item in below_boundary["selections"]])

        self.assertIsNone(
            minimum_budget_for_value_stake(
                make_payload([(1, 0.25, 4.0)]),
                100,
                settings,
                1,
            )
        )
        exact_break_even = make_payload([(1, 0.025, 40.0)])
        exact_break_even_detail = calculate_value_details(
            exact_break_even,
            3000,
            100,
            settings,
        )[0]
        self.assertEqual(exact_break_even_detail["expected_value"], 1.0)
        self.assertEqual(exact_break_even_detail["full_kelly"], 0.0)
        self.assertIsNone(
            minimum_budget_for_value_stake(
                exact_break_even,
                100,
                settings,
                1,
            )
        )
        self.assertIsNone(
            minimum_budget_for_value_stake(
                make_payload([(1, 0.40, 3.0)]),
                100,
                make_config(kelly_fraction=0.0)["simulation"]["value"],
                1,
            )
        )
        self.assertIsNone(
            minimum_budget_for_value_stake(
                make_payload([(1, 0.30, 3.0)]),
                100,
                settings,
                1,
            )
        )


class DutchingSimulationTests(unittest.TestCase):
    def test_app_config_default_min_profit_rate_is_twenty_percent(self) -> None:
        config = load_config(ROOT / "config" / "app.yaml")

        self.assertEqual(float(config["simulation"]["dutching"]["min_profit_rate"]), 0.20)

    def test_counts_order_metrics_allocation_and_best_candidate(self) -> None:
        result = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS),
            make_config(budget=1000),
        )

        self.assertEqual([item["selection_count"] for item in result["evaluated_counts"]], [1, 2, 3, 4, 5])
        self.assertEqual(result["evaluated_counts"][1]["horse_numbers"], [1, 2])
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["coverage_probability"], 0.55)
        self.assertEqual(result["expected_return"], 1220.0)
        self.assertEqual(result["group_expected_value"], 1.22)
        self.assertEqual(result["minimum_payout"], 2000.0)
        self.assertEqual(result["minimum_profit"], 1000.0)
        self.assertEqual(result["settings"]["min_profit_rate"], 0.20)
        self.assertEqual([(item["horse_number"], item["stake"]) for item in result["selections"]], [(1, 600), (2, 400)])
        self.assertEqual(result["total_stake"], 1000)
        self.assertTrue(all(item["stake"] >= 100 for item in result["selections"]))
        self.assertLessEqual(
            max(item["estimated_payout"] for item in result["selections"])
            - min(item["estimated_payout"] for item in result["selections"]),
            400,
        )

    def test_min_profit_rate_boundary_uses_actual_total_stake(self) -> None:
        settings = make_config(
            min_coverage_probability=0.0,
            min_group_expected_value=0.0,
            min_profit_rate=0.20,
            require_profit_if_hit=False,
        )["simulation"]["dutching"]
        at_boundary, at_boundary_selections = evaluate_dutching_count(
            [{"horse_number": 1, "predicted_probability": 1.0, "win_odds": 1.2}],
            3050,
            100,
            settings,
        )
        below_boundary, _ = evaluate_dutching_count(
            [
                {
                    "horse_number": 1,
                    "predicted_probability": 1.0,
                    "win_odds": 3599 / 3000,
                }
            ],
            3050,
            100,
            settings,
        )
        smaller_purchase, smaller_selections = evaluate_dutching_count(
            [{"horse_number": 1, "predicted_probability": 1.0, "win_odds": 1.2}],
            1550,
            100,
            settings,
        )
        stricter_settings = dict(settings, min_profit_rate=0.2001)
        stricter_rate, _ = evaluate_dutching_count(
            [{"horse_number": 1, "predicted_probability": 1.0, "win_odds": 1.2}],
            3050,
            100,
            stricter_settings,
        )

        self.assertEqual(sum(item["stake"] for item in at_boundary_selections), 3000)
        self.assertEqual(at_boundary["minimum_profit"], 600.0)
        self.assertTrue(at_boundary["eligible"])
        self.assertEqual(below_boundary["minimum_profit"], 599.0)
        self.assertIn(
            "minimum_profit_rate_below_threshold",
            below_boundary["rejection_reasons"],
        )
        self.assertEqual(sum(item["stake"] for item in smaller_selections), 1500)
        self.assertEqual(smaller_purchase["minimum_profit"], 300.0)
        self.assertTrue(smaller_purchase["eligible"])
        self.assertIn(
            "minimum_profit_rate_below_threshold",
            stricter_rate["rejection_reasons"],
        )

    def test_min_profit_rate_can_exclude_every_candidate(self) -> None:
        result = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS),
            make_config(budget=1000, min_profit_rate=10.0),
        )

        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["selections"], [])
        self.assertTrue(
            all(
                "minimum_profit_rate_below_threshold" in item["rejection_reasons"]
                for item in result["evaluated_counts"]
            )
        )

    def test_probability_tie_uses_horse_number(self) -> None:
        rows = [(2, 0.40, 3.0), (1, 0.40, 4.0), (3, 0.20, 8.0)]
        result = calculate_dutching_pre(
            make_payload(rows),
            make_config(budget=1000, min_coverage_probability=0.0),
        )

        self.assertEqual(result["evaluated_counts"][0]["horse_numbers"], [1])
        self.assertEqual(result["evaluated_counts"][1]["horse_numbers"], [1, 2])

    def test_all_rejection_reasons_and_no_eligible_candidate(self) -> None:
        group_rejected = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS),
            make_config(budget=1000, min_group_expected_value=2.0),
        )
        insufficient = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS[:3]),
            make_config(
                budget=100,
                max_selection_count=3,
                min_coverage_probability=0.5,
                require_profit_if_hit=False,
            ),
        )

        self.assertEqual(group_rejected["selected_count"], 0)
        self.assertTrue(
            all(
                "group_expected_value_below_threshold" in item["rejection_reasons"]
                for item in group_rejected["evaluated_counts"]
            )
        )
        self.assertEqual(insufficient["selected_count"], 0)
        self.assertIn("coverage_probability_below_threshold", insufficient["evaluated_counts"][0]["rejection_reasons"])
        self.assertIn("insufficient_budget_units", insufficient["evaluated_counts"][1]["rejection_reasons"])

    def test_profit_requirement_is_applied(self) -> None:
        required = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS),
            make_config(budget=1000, require_profit_if_hit=True),
        )
        optional = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS),
            make_config(budget=1000, require_profit_if_hit=False),
        )

        self.assertIn("minimum_profit_not_positive", required["evaluated_counts"][4]["rejection_reasons"])
        self.assertNotIn("minimum_profit_not_positive", optional["evaluated_counts"][4]["rejection_reasons"])

    def test_best_candidate_tie_breaking(self) -> None:
        evaluations = [
            ({"eligible": True, "group_expected_value": 1.1, "coverage_probability": 0.6, "selection_count": 3}, []),
            ({"eligible": True, "group_expected_value": 1.2, "coverage_probability": 0.5, "selection_count": 4}, []),
        ]
        self.assertEqual(select_best_dutching(evaluations)[0]["selection_count"], 4)

        coverage_tie = [
            ({"eligible": True, "group_expected_value": 1.2, "coverage_probability": 0.5, "selection_count": 2}, []),
            ({"eligible": True, "group_expected_value": 1.2, "coverage_probability": 0.6, "selection_count": 4}, []),
        ]
        self.assertEqual(select_best_dutching(coverage_tie)[0]["selection_count"], 4)

        count_tie = [
            ({"eligible": True, "group_expected_value": 1.2, "coverage_probability": 0.6, "selection_count": 3}, []),
            ({"eligible": True, "group_expected_value": 1.2, "coverage_probability": 0.6, "selection_count": 2}, []),
        ]
        self.assertEqual(select_best_dutching(count_tie)[0]["selection_count"], 2)


class PostAndStructureTests(unittest.TestCase):
    def test_simulate_file_generates_both_pre_and_post(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        config = make_config(budget=1000)
        config["data_dir"] = "data"
        logger_name = "test-simulate-file"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "race.json"
            save_race_json(path, payload)
            try:
                self.assertTrue(simulate_file(path, config, "pre", logger_name, root))
                pre_payload = load_race_json(path)
                self.assertIsNotNone(pre_payload["simulation"]["value"]["pre"])
                self.assertIsNotNone(pre_payload["simulation"]["dutching"]["pre"])
                self.assertIsNone(pre_payload["simulation"]["value"]["post"])
                self.assertIsNone(pre_payload["simulation"]["dutching"]["post"])

                pre_payload["result"] = make_result(1, 400, [1, 2, 3, 4, 5])
                save_race_json(path, pre_payload)
                self.assertTrue(simulate_file(path, config, "post", logger_name, root))
                post_payload = load_race_json(path)
                self.assertIsNotNone(post_payload["simulation"]["value"]["post"])
                self.assertIsNotNone(post_payload["simulation"]["dutching"]["post"])
            finally:
                logger = logging.getLogger(f"keiba_oracle.{logger_name}")
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_statistical_variant_generates_independent_pre_and_post_without_mutating_pre(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        payload["prediction"]["variants"] = [
            {
                "method": "statistical",
                "model_provider": "codex",
                "model_name": "gpt-test",
                "predicted_at": "2026-01-01T12:00:00+09:00",
                "horses": [
                    {"horse_number": 1, "win_probability": 0.10, "reason": "stats 1"},
                    {"horse_number": 2, "win_probability": 0.40, "reason": "stats 2"},
                    {"horse_number": 3, "win_probability": 0.25, "reason": "stats 3"},
                    {"horse_number": 4, "win_probability": 0.15, "reason": "stats 4"},
                    {"horse_number": 5, "win_probability": 0.10, "reason": "stats 5"},
                ],
            }
        ]
        config = make_config(budget=1000)
        config["data_dir"] = "data"
        logger_name = "test-simulate-statistical"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "race.json"
            save_race_json(path, payload)
            try:
                self.assertTrue(simulate_file(path, config, "pre", logger_name, root))
                pre_payload = load_race_json(path)
                variants = pre_payload["simulation"]["variants"]
                self.assertEqual(len(variants), 1)
                self.assertEqual(variants[0]["method"], "statistical")
                self.assertIsNotNone(variants[0]["value"]["pre"])
                self.assertIsNotNone(variants[0]["dutching"]["pre"])
                self.assertIsNone(variants[0]["value"]["post"])
                self.assertIsNone(variants[0]["dutching"]["post"])
                all_pre_before = {
                    "traditional_value": copy.deepcopy(pre_payload["simulation"]["value"]["pre"]),
                    "traditional_dutching": copy.deepcopy(pre_payload["simulation"]["dutching"]["pre"]),
                    "statistical_value": copy.deepcopy(variants[0]["value"]["pre"]),
                    "statistical_dutching": copy.deepcopy(variants[0]["dutching"]["pre"]),
                }

                pre_payload["result"] = make_result(2, 500, [1, 2, 3, 4, 5])
                save_race_json(path, pre_payload)
                self.assertTrue(simulate_file(path, config, "post", logger_name, root))
                post_payload = load_race_json(path)
                statistical = post_payload["simulation"]["variants"][0]
                self.assertIsNotNone(statistical["value"]["post"])
                self.assertIsNotNone(statistical["dutching"]["post"])
                self.assertEqual(post_payload["simulation"]["value"]["pre"], all_pre_before["traditional_value"])
                self.assertEqual(post_payload["simulation"]["dutching"]["pre"], all_pre_before["traditional_dutching"])
                self.assertEqual(statistical["value"]["pre"], all_pre_before["statistical_value"])
                self.assertEqual(statistical["dutching"]["pre"], all_pre_before["statistical_dutching"])
            finally:
                logger = logging.getLogger(f"keiba_oracle.{logger_name}")
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_method_specific_post_inside_outside_and_no_purchase(self) -> None:
        payload = make_payload([(1, 0.5, 3.0), (2, 0.3, 5.0), (3, 0.2, 8.0)])
        payload["simulation"] = {
            "value": {
                "pre": {"selections": [{"horse_number": 1, "stake": 200}]},
                "post": None,
            },
            "dutching": {
                "pre": {
                    "selections": [
                        {"horse_number": 1, "stake": 300},
                        {"horse_number": 2, "stake": 200},
                    ]
                },
                "post": None,
            },
        }

        payload["result"] = make_result(2, 500, [1, 2, 3])
        value_outside = calculate_value_post(payload)
        dutching_inside = calculate_dutching_post(payload)
        self.assertEqual(value_outside["total_return"], 0)
        self.assertEqual(dutching_inside["total_return"], 1000)

        payload["result"] = make_result(1, 300, [1, 2, 3])
        self.assertEqual(calculate_value_post(payload)["total_return"], 600)
        self.assertEqual(calculate_dutching_post(payload)["total_return"], 900)

        payload["result"] = make_result(3, 800, [1, 2, 3])
        self.assertEqual(calculate_value_post(payload)["total_return"], 0)
        self.assertEqual(calculate_dutching_post(payload)["total_return"], 0)

        payload["simulation"]["value"]["pre"] = {"selections": []}
        payload["simulation"]["dutching"]["pre"] = {"selections": []}
        self.assertEqual(calculate_value_post(payload)["total_stake"], 0)
        self.assertEqual(calculate_dutching_post(payload)["total_stake"], 0)

    def test_post_hit_miss_empty_and_pre_immutability(self) -> None:
        pre = {
            "selections": [
                {"horse_number": 1, "stake": 600},
                {"horse_number": 2, "stake": 400},
            ]
        }
        pre_before = copy.deepcopy(pre)
        hit = calculate_post(pre, make_result(1, 400, [1, 2, 3]))
        miss = calculate_post(pre, make_result(3, 700, [1, 2, 3]))
        empty = calculate_post({"selections": []}, make_result(1, 400, [1, 2, 3]))

        self.assertEqual(hit["total_return"], 2400)
        self.assertEqual(hit["profit"], 1400)
        self.assertEqual(hit["roi"], 1.4)
        self.assertEqual(hit["selections"][0]["return"], 2400)
        self.assertEqual(hit["selections"][1]["return"], 0)
        self.assertEqual(miss["total_return"], 0)
        self.assertEqual(miss["profit"], -1000)
        self.assertEqual(empty, {"total_stake": 0, "total_return": 0, "profit": 0, "roi": 0.0, "selections": []})
        self.assertEqual(pre, pre_before)

    def test_new_json_structure_post_and_reload(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        payload["simulation"] = calculate_pre_simulation(payload, make_config(budget=1000))
        pre_before = copy.deepcopy(payload["simulation"])
        payload["result"] = make_result(1, 400, [1, 2, 3, 4, 5])
        payload["simulation"]["value"]["post"] = calculate_value_post(payload)
        payload["simulation"]["dutching"]["post"] = calculate_dutching_post(payload)

        self.assertEqual(set(payload["simulation"]), {"value", "dutching", "variants"})
        self.assertNotIn("pre", payload["simulation"])
        self.assertNotIn("post", payload["simulation"])
        self.assertEqual(payload["simulation"]["value"]["pre"], pre_before["value"]["pre"])
        self.assertEqual(payload["simulation"]["dutching"]["pre"], pre_before["dutching"]["pre"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.json"
            save_race_json(path, payload)
            loaded = load_race_json(path)
        self.assertEqual(set(loaded["simulation"]), {"value", "dutching", "variants"})
        self.assertIsNotNone(loaded["simulation"]["value"]["post"])
        self.assertIsNotNone(loaded["simulation"]["dutching"]["post"])

class HtmlAndJavaScriptTests(unittest.TestCase):
    def full_payload(self) -> dict:
        payload = make_payload(DUTCHING_ROWS)
        payload["simulation"] = calculate_pre_simulation(payload, make_config(budget=1000))
        payload["result"] = make_result(1, 400, [1, 2, 3, 4, 5])
        payload["simulation"]["value"]["post"] = calculate_value_post(payload)
        payload["simulation"]["dutching"]["post"] = calculate_dutching_post(payload)
        payload["evaluation"] = build_evaluation(payload)
        return payload

    def render_page(self, payload: dict, page_kind: str = "prediction") -> str:
        context = build_race_context(payload)
        status = "prediction" if page_kind == "prediction" else "result"
        context.update(
            {
                "page_kind": page_kind,
                "prediction_page_name": "test_11r.html",
                "result_page_name": "test_11r_result.html",
                "status_label": "予想公開" if status == "prediction" else "結果公開",
                "status_class": f"status-{status}",
            }
        )
        return build_environment(ROOT).get_template("race.html.j2").render(**context)

    def test_ai_tabs_and_custom_simulator_use_method_specific_probabilities(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        statistical_probabilities = [0.10, 0.15, 0.20, 0.25, 0.30]
        payload["prediction"]["variants"] = [
            {
                "method": "statistical",
                "model_provider": "codex",
                "model_name": "gpt-test",
                "optional_summary": "Statistical summary",
                "horses": [
                    {
                        "horse_number": number,
                        "win_probability": probability,
                        "reason": f"statistical reason {number}",
                    }
                    for (number, _, _), probability in zip(
                        DUTCHING_ROWS,
                        statistical_probabilities,
                    )
                ],
            }
        ]
        payload["simulation"] = calculate_pre_simulation(payload, make_config(budget=1000))

        rendered = self.render_page(payload)
        soup = BeautifulSoup(rendered, "html.parser")
        prediction_panels = soup.select('[id^="prediction-"][data-ai-panel]')
        simulation_panels = soup.select('[id^="simulation-"][data-ai-panel]')
        self.assertEqual(
            [panel["data-ai-method"] for panel in prediction_panels],
            ["traditional", "statistical"],
        )
        self.assertEqual(
            [panel["data-ai-method"] for panel in simulation_panels],
            ["traditional", "statistical"],
        )
        self.assertFalse(prediction_panels[0].has_attr("hidden"))
        self.assertTrue(prediction_panels[1].has_attr("hidden"))
        prediction_section = soup.select_one(".prediction-section")
        self.assertIn("has-ai-tabs", prediction_section.get("class", []))
        self.assertIsNotNone(prediction_section.select_one(".prediction-method-tabs"))
        self.assertTrue(
            all("prediction-method-content" in panel.get("class", []) for panel in prediction_panels)
        )
        self.assertTrue(all(panel.find("h3", recursive=False) is None for panel in prediction_panels))
        traditional_headers = [
            header.get_text(" ", strip=True).replace(" ↕", "")
            for header in prediction_panels[0].select("table.prediction-table thead th")
        ]
        statistical_headers = [
            header.get_text(" ", strip=True).replace(" ↕", "")
            for header in prediction_panels[1].select("table.prediction-table thead th")
        ]
        self.assertEqual(
            traditional_headers,
            ["馬番", "馬名", "騎手", "単勝オッズ", "人気", "1着確率", "予想順位", "理由"],
        )
        self.assertEqual(
            statistical_headers,
            ["馬番", "馬名", "騎手", "1着確率", "予想順位", "理由"],
        )
        self.assertEqual(
            len(prediction_panels[1].select("table.prediction-table thead button[data-sort-column]")),
            5,
        )
        self.assertEqual(
            [
                int(button["data-sort-column"])
                for button in prediction_panels[1].select(
                    "table.prediction-table thead button[data-sort-column]"
                )
            ],
            [0, 1, 2, 3, 4],
        )
        statistical_first_row = [
            cell.get_text(" ", strip=True)
            for cell in prediction_panels[1].select_one("table.prediction-table tbody tr").select("td")
        ]
        self.assertEqual(statistical_first_row[:3], ["1", "Horse 1", "Jockey 1"])
        self.assertTrue(
            all(
                "reason-cell" in cell.get("class", [])
                for cell in prediction_panels[1].select(
                    "table.prediction-table tbody td:nth-child(6)"
                )
            )
        )
        self.assertNotIn("枠番", statistical_headers)
        self.assertNotIn("斤量", statistical_headers)
        self.assertNotIn("脚質", statistical_headers)
        self.assertNotIn("単勝オッズ", statistical_headers)
        self.assertNotIn("人気", statistical_headers)
        self.assertFalse(simulation_panels[0].has_attr("hidden"))
        self.assertTrue(simulation_panels[1].has_attr("hidden"))
        simulation_section = soup.select_one("section.simulation-section")
        self.assertIn("has-ai-tabs", simulation_section.get("class", []))
        self.assertIsNotNone(simulation_section.select_one(".simulation-method-tabs"))
        self.assertTrue(
            all("simulation-method-content" in panel.get("class", []) for panel in simulation_panels)
        )

        payload["result"] = make_result(1, 400, [1, 2, 3, 4, 5])
        result_rendered = self.render_page(payload, "result")
        result_soup = BeautifulSoup(result_rendered, "html.parser")
        result_section = result_soup.select_one("section.result-section.has-ai-tabs")
        result_panels = result_section.select(".result-method-content[data-ai-panel]")
        self.assertIsNotNone(result_section.select_one(".result-method-tabs"))
        self.assertEqual(
            [panel["data-ai-method"] for panel in result_panels],
            ["traditional", "statistical"],
        )
        self.assertIsNotNone(result_panels[0].select_one(".hit-badge"))
        self.assertIsNone(result_panels[1].select_one(".hit-badge"))
        self.assertIn("panel", result_section.get("class", []))

        embedded = json.loads(soup.select_one("#custom-simulator-data").string)
        self.assertEqual(set(embedded["methods"]), {"traditional", "statistical"})
        self.assertEqual(
            [row["win_probability"] for row in embedded["methods"]["statistical"]["horses"]],
            statistical_probabilities,
        )
        self.assertIn('document.addEventListener("ai-method-change"', rendered)
        self.assertIn("calculateValueDetails(activeHorses()", rendered)
        self.assertIn("calculateDutchingSimulation(activeHorses()", rendered)
        self.assertNotIn("localStorage", rendered)
        self.assertNotIn("document.cookie", rendered)

    def test_prediction_and_result_pages_expose_required_sections_and_custom_data(self) -> None:
        payload = self.full_payload()
        rendered = self.render_page(payload)
        result_rendered = self.render_page(payload, "result")

        for text in (
            "購入シミュレーション",
            "期待値重視方式",
            "単勝分配方式",
            "頭数別比較",
            "カスタム購入シミュレーション",
            "予想順位",
        ):
            self.assertIn(text, rendered)
        for text in (
            "期待値重視方式のシミュレーション結果",
            "単勝分配方式のシミュレーション結果",
            "予測評価",
            "レース結果",
        ):
            self.assertIn(text, result_rendered)
        self.assertNotIn("シミュレーション収支", result_rendered)
        self.assertNotIn("レース結果", rendered)
        self.assertNotIn("カスタム購入シミュレーション", result_rendered)
        soup = BeautifulSoup(rendered, "html.parser")
        simulation_section = soup.select_one("section.simulation-section")
        self.assertIsNotNone(simulation_section)
        self.assertEqual(
            simulation_section.find("h2", recursive=False).get_text(strip=True),
            "購入シミュレーション",
        )
        simulation_panels = simulation_section.select_one(".ai-method-panel").find_all(
            "div",
            class_="simulation-panel",
            recursive=False,
        )
        self.assertEqual(len(simulation_panels), 2)
        self.assertIn("単勝分配方式", simulation_panels[0].find("h3").get_text())
        self.assertIn("期待値重視方式", simulation_panels[1].find("h3").get_text())
        dutching_metric_labels = [
            card.find("strong", recursive=False).get_text(" ", strip=True)
            for card in simulation_panels[0].select_one(".metric-grid").find_all(
                "div", recursive=False
            )
        ]
        value_metric_labels = [
            card.find("strong", recursive=False).get_text(" ", strip=True)
            for card in simulation_panels[1].select_one(".metric-grid").find_all(
                "div", recursive=False
            )
        ]
        self.assertFalse(any(label.startswith("購入単位") for label in dutching_metric_labels))
        self.assertFalse(any(label.startswith("購入単位") for label in value_metric_labels))
        self.assertFalse(any(label.startswith("期待払戻額") for label in dutching_metric_labels))
        self.assertIsNone(simulation_section.select_one("#custom-simulator"))
        self.assertIsNone(
            soup.select_one("#custom-simulator").find_parent("section", class_="simulation-section")
        )
        value_heading = '<span>期待値重視方式</span>'
        dutching_heading = '<span>単勝分配方式</span>'
        self.assertLess(rendered.index(dutching_heading), rendered.index(value_heading))
        self.assertLess(
            rendered.index(value_heading),
            rendered.index("<h2>カスタム購入シミュレーション</h2>"),
        )
        self.assertIn('<option value="dutching" selected>単勝分配方式</option>', rendered)
        self.assertNotIn("ダッチング", rendered)
        self.assertIn('value="dutching"', rendered)
        self.assertIn('<div class="simulator-field value-field" hidden>', rendered)
        self.assertIn('<label class="simulator-field dutching-field">最大対象頭数', rendered)
        self.assertIn('name="budget" type="number" min="100" step="100" value="1000"', rendered)
        self.assertIn('name="ev_threshold" type="number" min="0" step="0.01" value="1.0"', rendered)
        self.assertIn('name="min_profit_rate" type="number" min="0" step="1" value="20"', rendered)
        self.assertIn("全馬期待値一覧", rendered)
        self.assertIn("EV順位", rendered)
        self.assertIn("EV基準", rendered)
        self.assertIn("頭数別比較の詳細を見る", rendered)
        self.assertIn("全馬期待値の詳細を見る", rendered)
        self.assertNotIn('<details class="simulation-details" open', rendered)
        self.assertNotIn("予想生成", rendered)
        self.assertNotIn("結果生成", rendered)
        self.assertNotIn('<section class="panel result-section', rendered)
        self.assertIn('<section class="panel result-section">', result_rendered)
        self.assertIn('<div class="panel result-panel">', result_rendered)
        self.assertNotIn("result_published", rendered)
        self.assertNotIn("フィードバック要約", rendered)
        self.assertIn('class="tooltip-trigger" aria-label=', rendered)
        self.assertIn('trigger.addEventListener("click"', rendered)
        match = re.search(
            r'<script type="application/json" id="custom-simulator-data">(.*?)</script>',
            rendered,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(html.unescape(match.group(1)))
        self.assertEqual(set(embedded), {"stake_unit", "horses", "methods", "display"})
        self.assertTrue(all(set(item) == {"horse_number", "win_probability", "win_odds"} for item in embedded["horses"]))
        self.assertEqual(set(embedded["methods"]), {"traditional"})
        self.assertNotIn("localStorage", rendered)
        self.assertNotIn("document.cookie", rendered)
        self.assertNotIn("fetch(", rendered)
        self.assertIn('id="custom-simulator-empty-reason"', rendered)
        self.assertIn("valueNoPurchaseReason(details, stakeUnit, settings.kelly_fraction)", rendered)
        self.assertNotIn('id="custom-dutching-return"', rendered)
        custom_value_headers = [
            header.get_text(" ", strip=True)
            for header in soup.select_one("#custom-value-table thead").select("th")
        ]
        self.assertTrue(any(header.startswith("期待払戻額") for header in custom_value_headers))

    def test_value_purchase_table_displays_expected_return_from_saved_selection(self) -> None:
        payload = make_payload([(1, 0.40, 3.0), (2, 0.60, 1.0)])
        payload["simulation"] = calculate_pre_simulation(
            payload,
            make_config(budget=10000, kelly_fraction=0.5),
        )
        rendered = self.render_page(payload)
        soup = BeautifulSoup(rendered, "html.parser")
        value_panel = next(
            panel
            for panel in soup.select(".simulation-panel")
            if "期待値重視方式" in panel.find("h3").get_text()
        )
        purchase_table = value_panel.find("h4", string="購入対象").find_next("table")
        headers = [header.get_text(" ", strip=True) for header in purchase_table.select("thead th")]
        cells = [cell.get_text(" ", strip=True) for cell in purchase_table.select_one("tbody tr").select("td")]

        self.assertTrue(headers[-1].startswith("期待払戻額 ⓘ"))
        self.assertEqual(cells[-2:], ["500円", "600円"])
        self.assertNotIn(
            "expected_return",
            payload["simulation"]["value"]["pre"]["selections"][0],
        )

    def test_result_page_uses_result_conditions_and_jst_fetch_time(self) -> None:
        payload = self.full_payload()
        payload["race"].update({"going": "良", "weather": "晴"})
        payload["result"].update(
            {
                "going": "重",
                "weather": "雨",
                "fetched_at": "2026-08-30T07:10:11+00:00",
            }
        )

        rendered = self.render_page(payload, "result")
        basic_info = BeautifulSoup(rendered, "html.parser").select_one(".meta").get_text(
            " ", strip=True
        )

        self.assertIn("馬場 重", basic_info)
        self.assertIn("天候 雨", basic_info)
        self.assertIn("結果取得日時 2026-08-30 16:10:11", basic_info)
        self.assertNotIn("馬場 良", basic_info)
        self.assertNotIn("天候 晴", basic_info)

    def test_legacy_dutching_settings_render_without_recalculation(self) -> None:
        payload = self.full_payload()
        del payload["simulation"]["dutching"]["pre"]["settings"]["min_profit_rate"]
        simulation_before = copy.deepcopy(payload["simulation"])

        rendered = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )
        soup = BeautifulSoup(rendered, "html.parser")

        self.assertIn("最低利益率", rendered)
        self.assertEqual(
            soup.select_one('input[name="min_profit_rate"]')["value"],
            "20",
        )
        self.assertEqual(payload["simulation"], simulation_before)

    def test_tables_keep_scroll_containers_cell_roles_and_zebra_scope(self) -> None:
        payload = self.full_payload()
        rendered = self.render_page(payload)
        result_rendered = self.render_page(payload, "result")
        soup = BeautifulSoup(rendered, "html.parser")
        result_soup = BeautifulSoup(result_rendered, "html.parser")

        self.assertIn(".prediction-table tbody tr:nth-child(even),", rendered)
        self.assertIn(".result-table tbody tr:nth-child(even)", rendered)
        self.assertNotIn("\n    table tbody tr:nth-child(even)", rendered)
        self.assertNotIn(".simulation-table tbody tr:nth-child(even)", rendered)
        self.assertNotIn(".value-detail-table tbody tr:nth-child(even)", rendered)

        prediction_table = soup.select_one("table.prediction-table")
        self.assertIsNotNone(prediction_table)
        self.assertIn("table-scroll", prediction_table.parent.get("class", []))
        self.assertTrue(all("horse-name" in cell.get("class", []) for cell in prediction_table.select("tbody td:nth-child(2)")))
        self.assertTrue(all("reason-cell" in cell.get("class", []) for cell in prediction_table.select("tbody td:nth-child(8)")))
        self.assertTrue(all("nowrap" in cell.get("class", []) for cell in prediction_table.select("tbody td:nth-child(1)")))

        value_table = soup.select_one("table.value-detail-table")
        result_table = result_soup.select_one("table.result-table[data-sortable]")
        self.assertIn("table-scroll", value_table.parent.get("class", []))
        self.assertIn("table-scroll", result_table.parent.get("class", []))
        self.assertTrue(all("horse-name" in cell.get("class", []) for cell in value_table.select("tbody td:nth-child(3)")))
        self.assertTrue(all("horse-name" in cell.get("class", []) for cell in result_table.select("tbody td:nth-child(2)")))
        self.assertTrue(all("nowrap" in cell.get("class", []) for cell in value_table.select("tbody td:not(:nth-child(3))")))

    def test_only_prediction_and_result_tables_are_sortable_with_raw_values(self) -> None:
        payload = self.full_payload()
        rendered = self.render_page(payload)
        result_rendered = self.render_page(payload, "result")
        soup = BeautifulSoup(rendered, "html.parser")
        result_soup = BeautifulSoup(result_rendered, "html.parser")
        sortable_tables = soup.select("table[data-sortable]") + result_soup.select("table[data-sortable]")

        self.assertEqual(len(sortable_tables), 2)
        self.assertEqual(
            [table.get("class") for table in sortable_tables],
            [["prediction-table"], ["result-table"]],
        )
        self.assertFalse(
            any(
                table.has_attr("data-sortable")
                for table in soup.select(
                    ".simulation-table, .evaluation-table, .value-detail-table"
                )
            )
        )

        def header_specs(table):
            return [
                (
                    button.get_text(" ", strip=True).replace(" ↕", ""),
                    button["data-sort-type"],
                    button["data-sort-first"],
                    int(button["data-sort-column"]),
                    button.parent["aria-sort"],
                )
                for button in table.select("thead .sort-button")
            ]

        prediction_table, result_table = sortable_tables
        self.assertEqual(
            header_specs(prediction_table),
            [
                ("馬番", "number", "ascending", 0, "none"),
                ("馬名", "text", "ascending", 1, "none"),
                ("騎手", "text", "ascending", 2, "none"),
                ("単勝オッズ", "number", "ascending", 3, "none"),
                ("人気", "number", "ascending", 4, "none"),
                ("1着確率", "number", "descending", 5, "none"),
                ("予想順位", "number", "ascending", 6, "none"),
            ],
        )
        self.assertEqual(
            header_specs(result_table),
            [
                ("馬番", "number", "ascending", 0, "none"),
                ("馬名", "text", "ascending", 1, "none"),
                ("1着確率", "number", "descending", 2, "none"),
                ("予測順位", "number", "ascending", 3, "none"),
                ("実着順", "number", "ascending", 4, "none"),
                ("予想との差", "number", "ascending", 5, "none"),
                ("確定オッズ", "number", "ascending", 6, "none"),
                ("単勝払戻", "number", "descending", 7, "none"),
            ],
        )
        self.assertIsNone(prediction_table.select("thead th")[-1].find("button"))
        self.assertTrue(
            all(button.get("type") == "button" for button in soup.select(".sort-button"))
        )

        prediction_first = prediction_table.select_one("tbody tr")
        self.assertEqual(
            [cell.get("data-sort-value") for cell in prediction_first.select("td")],
            ["1", None, None, "4.0", "1", "0.3", "1", None],
        )
        result_first = result_table.select_one("tbody tr")
        self.assertEqual(
            [cell.get("data-sort-value") for cell in result_first.select("td")],
            ["1", None, "0.3", "1", "1", "0", "", "400"],
        )
        self.assertIn("const initializeSortableTable = (table) =>", rendered)
        self.assertIn(
            '".prediction-table[data-sortable], .result-table[data-sortable]"',
            rendered,
        )
        self.assertIn('valueA.localeCompare(valueB, "ja")', rendered)
        self.assertIn('row.dataset.originalIndex = String(index)', rendered)

    def test_all_horse_expected_values_are_rendered_without_changing_simulation(self) -> None:
        payload = make_payload(
            [(1, 0.02, 60.0), (2, 0.39, 3.0), (3, 0.2, 4.0), (4, 0.1, None)]
        )
        payload["simulation"] = calculate_pre_simulation(payload, make_config())
        simulation_before = copy.deepcopy(payload["simulation"])

        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        soup = BeautifulSoup(rendered, "html.parser")
        table = soup.find("h4", string="全馬期待値一覧").find_next("table")
        table_rows = table.select("tbody tr")
        rows = [[cell.get_text(strip=True) for cell in row.select("td")] for row in table_rows]

        self.assertEqual(len(rows), 4)
        self.assertEqual([row[1] for row in rows], ["1", "2", "3", "4"])
        self.assertEqual([row[0] for row in rows], ["1位", "2位", "3位", "-"])
        self.assertEqual([row[5] for row in rows], ["1.200", "1.170", "0.800", "-"])
        self.assertEqual([row[6] for row in rows], ["基準以上", "基準以上", "基準未満", "算出不可"])
        self.assertEqual(rows[0][7:], ["0.3390%", "0.1695%", "5.08円", "約59,000円", "購入単位未満"])
        self.assertEqual(rows[1][7:], ["8.5000%", "4.2500%", "127.50円", "約2,353円", "購入：100円"])
        self.assertEqual(rows[2][7:], ["0.0000%", "0.0000%", "-", "-", "EV基準未満"])
        self.assertEqual(rows[3][7:], ["-", "-", "-", "-", "算出不可"])
        self.assertEqual(table_rows[0].get("class"), ["ev-above-threshold"])
        self.assertEqual(table_rows[1].get("class"), ["simulation-selected"])
        self.assertEqual(table_rows[2].get("class"), None)
        self.assertIn('class="table-scroll"', str(table.parent))
        self.assertEqual(payload["simulation"], simulation_before)

    def test_value_no_purchase_reason_matches_cause(self) -> None:
        cases = (
            (
                make_payload([(1, 0.02, 60.0)]),
                make_config(),
                "EV基準以上の馬はありますが、現在の予算ではKelly基準の購入額が100円未満となるため、購入対象はありません。",
            ),
            (
                make_payload([(1, 0.30, 3.0)]),
                make_config(),
                "最低EVを満たす馬がありません。",
            ),
            (
                make_payload([(1, 0.40, 3.0)]),
                make_config(kelly_fraction=0.0),
                "Kelly係数が0のため、購入対象はありません。",
            ),
        )
        for payload, config, expected in cases:
            with self.subTest(expected=expected):
                payload["simulation"] = calculate_pre_simulation(payload, config)
                rendered = build_environment(ROOT).get_template("race.html.j2").render(
                    **build_race_context(payload)
                )
                self.assertIn("<strong>購入なし</strong>", rendered)
                self.assertIn(expected, rendered)

    def test_rejection_reasons_are_localized_for_normal_and_custom_tables(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        payload["simulation"] = calculate_pre_simulation(
            payload,
            make_config(
                budget=100,
                min_coverage_probability=1.0,
                min_group_expected_value=10.0,
                min_profit_rate=10.0,
            ),
        )
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        normal_section = rendered.split('<div class="panel" id="custom-simulator">', 1)[0]

        for label in (
            "カバー確率が最低基準未満",
            "グループ期待値が最低基準未満",
            "最低利益率が最低基準未満",
            "的中時の最低利益を確保できない",
            "予算が購入単位または選択頭数に対して不足",
        ):
            self.assertIn(label, normal_section)
            self.assertIn(label, rendered)
        for internal_value in (
            "coverage_probability_below_threshold",
            "group_expected_value_below_threshold",
            "minimum_profit_rate_below_threshold",
            "minimum_profit_not_positive",
            "insufficient_budget_units",
        ):
            self.assertNotIn(internal_value, normal_section)
        self.assertIn("rejectionReasonText(item.rejection_reasons)", rendered)

    def test_result_table_compares_prediction_rank_and_finish(self) -> None:
        payload = make_payload([(1, 0.40, 3.0), (2, 0.35, 4.0), (3, 0.25, 5.0)])
        payload["result"] = make_result(2, 500, [1, 2, 3])
        rendered = self.render_page(payload, "result")
        soup = BeautifulSoup(rendered, "html.parser")
        table = soup.select_one("table.result-table[data-sortable]")
        headers = [
            cell.get_text(" ", strip=True).replace(" ↕", "")
            for cell in table.select("thead th")
        ]
        rows = {
            int(cells[0].get_text(strip=True)): (row, [cell.get_text(strip=True) for cell in cells])
            for row in table.select("tbody tr")
            if (cells := row.select("td"))
        }

        self.assertEqual(
            headers,
            [
                "馬番",
                "馬名",
                "1着確率",
                "予測順位",
                "実着順",
                "予想との差",
                "確定オッズ",
                "単勝払戻",
            ],
        )
        self.assertEqual(rows[1][1], ["1", "Horse 1", "40.0%", "1位", "2着", "1着下", "-", "-"])
        self.assertEqual(rows[2][1], ["2", "Horse 2", "35.0%", "2位", "1着", "1着上", "-", "500円"])
        self.assertEqual(rows[3][1], ["3", "Horse 3", "25.0%", "3位", "3着", "差なし", "-", "-"])
        self.assertEqual(
            [cell.get("data-sort-value") for cell in rows[1][0].select("td")],
            ["1", None, "0.4", "1", "2", "1", "", ""],
        )
        self.assertEqual(
            [cell.get("data-sort-value") for cell in rows[2][0].select("td")],
            ["2", None, "0.35", "2", "1", "-1", "", "500"],
        )
        self.assertIsNone(rows[1][0].get("class"))
        self.assertIsNone(rows[2][0].get("class"))
        self.assertIn("rank-prediction-top", rows[1][0].select("td")[1].get("class", []))
        self.assertIn("rank-prediction-top", rows[1][0].select("td")[3].get("class", []))
        self.assertIn("rank-result-winner", rows[2][0].select("td")[1].get("class", []))
        self.assertIn("rank-result-winner", rows[2][0].select("td")[4].get("class", []))
        comparison_classes = {
            horse_number: rows[horse_number][0].select("td")[5].select_one("span")
            for horse_number in rows
        }
        self.assertIn("comparison-down", comparison_classes[1].get("class", []))
        self.assertIn("comparison-up", comparison_classes[2].get("class", []))
        self.assertIn("comparison-neutral", comparison_classes[3].get("class", []))
        self.assertTrue(
            all(item.find(["strong", "b"]) is None for item in comparison_classes.values())
        )
        self.assertIsNone(soup.select_one(".hit-badge"))
        self.assertIn('class="table-scroll"', str(table.parent))

    def test_prediction_hit_uses_green_only_in_result_table(self) -> None:
        payload = make_payload([(1, 0.40, 3.0), (2, 0.35, 4.0), (3, 0.25, 5.0)])
        payload["result"] = make_result(1, 300, [1, 2, 3])
        rendered = self.render_page(payload)
        result_rendered = self.render_page(payload, "result")
        soup = BeautifulSoup(rendered, "html.parser")
        result_soup = BeautifulSoup(result_rendered, "html.parser")
        prediction_row = soup.select_one("table.prediction-table tbody tr")
        result_table = result_soup.select_one("table.result-table[data-sortable]")
        result_row = result_table.select_one("tbody tr")

        self.assertIsNone(prediction_row.get("class"))
        self.assertIsNone(result_row.get("class"))
        self.assertIn("rank-prediction-top", prediction_row.select("td")[1].get("class", []))
        self.assertIn("rank-prediction-top", prediction_row.select("td")[6].get("class", []))
        self.assertIn("rank-prediction-hit", result_row.select("td")[1].get("class", []))
        self.assertIn("rank-prediction-hit", result_row.select("td")[3].get("class", []))
        self.assertIn("rank-prediction-hit", result_row.select("td")[4].get("class", []))
        self.assertIsNotNone(result_soup.select_one(".hit-badge"))

    def test_result_highlight_ignores_simulation_selections(self) -> None:
        payload = make_payload(
            [(1, 0.40, 3.0), (2, 0.30, 4.0), (3, 0.20, 5.0), (4, 0.10, 6.0)]
        )
        payload["simulation"]["value"]["pre"] = {
            "budget": 3000,
            "stake_unit": 100,
            "settings": {"ev_threshold": 1.0, "kelly_fraction": 0.5},
            "selections": [{"horse_number": 2, "stake": 100}],
        }
        payload["simulation"]["dutching"]["pre"] = {
            "selections": [
                {"horse_number": 3, "stake": 100},
                {"horse_number": 4, "stake": 0},
            ]
        }
        payload["result"] = make_result(2, 400, [1, 2, 3, 4])

        rows = {
            row["horse_number"]: row
            for row in build_race_context(payload)["result_rows"]
        }

        self.assertEqual(rows[1]["prediction_rank_class"], "rank-prediction-top")
        self.assertEqual(rows[2]["finish_rank_class"], "rank-result-winner")
        self.assertEqual(rows[3]["prediction_rank_class"], "")
        self.assertEqual(rows[4]["finish_rank_class"], "")
        self.assertTrue(all("row_class" not in row for row in rows.values()))
        self.assertTrue(all("simulation_selected" not in row for row in rows.values()))

    def test_no_purchase_post_hides_roi_and_detail_tables(self) -> None:
        payload = make_payload([(1, 0.5, 1.5), (2, 0.5, 1.5)])
        payload["simulation"] = calculate_pre_simulation(
            payload,
            make_config(min_group_expected_value=2.0),
        )
        payload["result"] = make_result(1, 150, [1, 2])
        payload["simulation"]["value"]["post"] = calculate_value_post(payload)
        payload["simulation"]["dutching"]["post"] = calculate_dutching_post(payload)
        rendered = self.render_page(payload, "result")

        value_section = rendered.split("<h3>期待値重視方式のシミュレーション結果</h3>", 1)[1].split(
            '<div class="panel result-panel">', 1
        )[0]
        dutching_section = rendered.split("<h3>単勝分配方式のシミュレーション結果</h3>", 1)[1].split(
            "</section>", 1
        )[0]
        for section in (value_section, dutching_section):
            self.assertIn("購入なし", section)
            self.assertNotIn("ROI", section)
            self.assertNotIn("<table", section)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for table sorting")
    def test_sortable_table_javascript_cycles_stably_and_keeps_missing_last(self) -> None:
        template = (ROOT / "templates" / "race.html.j2").read_text(encoding="utf-8")
        start = template.index("    const initializeSortableTable")
        end = template.index("    (() => {", start)
        functions = template[start:end]
        node_script = functions + r"""
const makeCell = (text, sortValue) => ({
  textContent: text,
  dataset: sortValue === undefined ? {} : {sortValue: String(sortValue)}
});
const makeRow = (id, number, probability, name, className, reason) => ({
  id,
  className,
  reason,
  dataset: {},
  cells: [
    makeCell(String(number), number),
    makeCell(probability === undefined ? "-" : `${probability * 100}%`, probability),
    makeCell(name)
  ]
});
const rows = [
  makeRow("two", 2, 0.4, "カ", "prediction-top", "reason two"),
  makeRow("ten", 10, 0.4, "ア", "", "reason ten"),
  makeRow("one", 1, undefined, "-", "", "reason one"),
  makeRow("three", 3, 0.6, "イ", "result-winner", "reason three")
];
const body = {
  rows,
  appendChild(row) {
    const index = this.rows.indexOf(row);
    if (index >= 0) this.rows.splice(index, 1);
    this.rows.push(row);
  }
};
const makeButton = (column, type, first) => {
  const indicator = {textContent: "↕"};
  const header = {
    attributes: {"aria-sort": "none"},
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; }
  };
  const button = {
    dataset: {sortColumn: String(column), sortType: type, sortFirst: first},
    closest() { return header; },
    querySelector() { return indicator; },
    addEventListener(typeName, handler) { if (typeName === "click") this.handler = handler; },
    click() { this.handler(); },
    header,
    indicator
  };
  return button;
};
const numberButton = makeButton(0, "number", "ascending");
const probabilityButton = makeButton(1, "number", "descending");
const nameButton = makeButton(2, "text", "ascending");
const buttons = [numberButton, probabilityButton, nameButton];
const table = {
  tBodies: [body],
  querySelectorAll() { return buttons; }
};
const ids = () => body.rows.map((row) => row.id);
initializeSortableTable(table);
const output = {initial: ids()};
numberButton.click();
output.numberAscending = ids();
numberButton.click();
output.numberDescending = ids();
numberButton.click();
output.numberRestored = ids();
probabilityButton.click();
output.probabilityDescending = ids();
probabilityButton.click();
output.probabilityAscending = ids();
nameButton.click();
output.nameAscending = ids();
output.switchedHeaders = {
  probability: probabilityButton.header.getAttribute("aria-sort"),
  probabilityIndicator: probabilityButton.indicator.textContent,
  name: nameButton.header.getAttribute("aria-sort"),
  nameIndicator: nameButton.indicator.textContent
};
nameButton.click();
output.nameDescending = ids();
nameButton.click();
output.nameRestored = ids();
output.preserved = {
  className: body.rows[0].className,
  reason: body.rows[0].reason
};
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run(
            [shutil.which("node"), "-"],
            input=node_script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["initial"], ["two", "ten", "one", "three"])
        self.assertEqual(result["numberAscending"], ["one", "two", "three", "ten"])
        self.assertEqual(result["numberDescending"], ["ten", "three", "two", "one"])
        self.assertEqual(result["numberRestored"], result["initial"])
        self.assertEqual(result["probabilityDescending"], ["three", "two", "ten", "one"])
        self.assertEqual(result["probabilityAscending"], ["two", "ten", "three", "one"])
        self.assertEqual(result["nameAscending"], ["ten", "three", "two", "one"])
        self.assertEqual(result["nameDescending"], ["two", "three", "ten", "one"])
        self.assertEqual(result["nameRestored"], result["initial"])
        self.assertEqual(
            result["switchedHeaders"],
            {
                "probability": "none",
                "probabilityIndicator": "↕",
                "name": "ascending",
                "nameIndicator": "▲",
            },
        )
        self.assertEqual(
            result["preserved"],
            {"className": "prediction-top", "reason": "reason two"},
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript parity")
    def test_python_and_javascript_results_match(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        config = make_config(budget=1000)
        python_result = {
            "value": calculate_value_pre(payload, config),
            "dutching": calculate_dutching_pre(payload, config),
            "dutching_strict": calculate_dutching_pre(
                payload,
                make_config(budget=1000, min_profit_rate=10.0),
            ),
        }
        horses = [
            {
                "horse_number": number,
                "win_probability": probability,
                "win_odds": odds,
            }
            for number, probability, odds in DUTCHING_ROWS
        ]
        template = (ROOT / "templates" / "race.html.j2").read_text(encoding="utf-8")
        start = template.index("      const SIMULATION_EPSILON")
        end = template.index("      (() => {", start)
        script_end = template.index("    </script>", start)
        functions = template[start:end]
        full_script = template[start:script_end]
        node_script = f"""
new Function({json.dumps(full_script)});
{functions}
const horses = {json.dumps(horses)};
const valueSettings = {{ev_threshold: 1.0, kelly_fraction: 0.5}};
const tinyDetails = calculateValueDetails(
  [{{horse_number: 1, win_probability: 0.02, win_odds: 60.0}}],
  3000,
  100,
  valueSettings
);
const output = {{
  value: calculateValueSimulation(horses, 1000, 100, valueSettings),
  value_details: calculateValueDetails(horses, 1000, 100, valueSettings),
  value_below_one: calculateValueSimulation(horses, 1000, 100, {{ev_threshold: 0.5, kelly_fraction: 0.5}}),
  value_no_purchase_reason: valueNoPurchaseReason(tinyDetails, 100, 0.5),
  dutching: calculateDutchingSimulation(horses, 1000, 100, {{
    max_selection_count: 5,
    min_coverage_probability: 0.4,
    min_group_expected_value: 0.0,
    min_profit_rate: 0.2,
    require_profit_if_hit: true
  }}),
  dutching_strict: calculateDutchingSimulation(horses, 1000, 100, {{
    max_selection_count: 5,
    min_coverage_probability: 0.4,
    min_group_expected_value: 0.0,
    min_profit_rate: 10.0,
    require_profit_if_hit: true
  }}),
  minimum_ev_valid: ["0", "0.5", "0.99", "1.0", "1.05"].map(parseMinimumEv),
  minimum_ev_invalid: ["-0.01", "", "NaN", "Infinity"].map(parseMinimumEv)
}};
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run(
            [shutil.which("node"), "-"],
            input=node_script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        javascript_result = json.loads(completed.stdout)
        self.assertEqual(javascript_result.pop("minimum_ev_valid"), [0, 0.5, 0.99, 1.0, 1.05])
        self.assertEqual(javascript_result.pop("minimum_ev_invalid"), [None, None, None, None])
        self.assertEqual(
            javascript_result.pop("value_no_purchase_reason"),
            "現在の予算では全候補の購入額が100円未満です。",
        )
        python_result["value_details"] = calculate_value_details(
            payload,
            1000,
            100,
            config["simulation"]["value"],
        )
        python_result["value_below_one"] = calculate_value_pre(
            payload,
            make_config(budget=1000, ev_threshold=0.5),
        )
        self.assertEqual(javascript_result, python_result)


if __name__ == "__main__":
    unittest.main()
