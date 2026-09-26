from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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
)
from evaluation import simulation_summary  # noqa: E402
from utils import ensure_race_payload, load_race_json, save_race_json  # noqa: E402


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
            "model_provider": "codex",
            "model_name": "gpt-test",
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
        saved_detail = calculate_value_pre(payload, make_config())["details"][0]
        self.assertEqual(saved_detail, {**detail, "minimum_budget": minimum_budget})
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
        self.assertFalse(below_boundary["eligible"])
        self.assertEqual(sum(item["stake"] for item in smaller_selections), 1500)
        self.assertEqual(smaller_purchase["minimum_profit"], 300.0)
        self.assertTrue(smaller_purchase["eligible"])
        self.assertFalse(stricter_rate["eligible"])

    def test_min_profit_rate_can_exclude_every_candidate(self) -> None:
        result = calculate_dutching_pre(
            make_payload(DUTCHING_ROWS),
            make_config(budget=1000, min_profit_rate=10.0),
        )

        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["selections"], [])
        self.assertTrue(all(not item["eligible"] for item in result["evaluated_counts"]))

    def test_probability_tie_uses_horse_number(self) -> None:
        rows = [(2, 0.40, 3.0), (1, 0.40, 4.0), (3, 0.20, 8.0)]
        result = calculate_dutching_pre(
            make_payload(rows),
            make_config(budget=1000, min_coverage_probability=0.0),
        )

        self.assertEqual(result["evaluated_counts"][0]["horse_numbers"], [1])
        self.assertEqual(result["evaluated_counts"][1]["horse_numbers"], [1, 2])

    def test_group_threshold_and_insufficient_budget_exclude_candidates(self) -> None:
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
            ),
        )

        self.assertEqual(group_rejected["selected_count"], 0)
        self.assertTrue(all(not item["eligible"] for item in group_rejected["evaluated_counts"]))
        self.assertEqual(insufficient["selected_count"], 0)
        self.assertTrue(all(not item["eligible"] for item in insufficient["evaluated_counts"]))

    def test_minimum_profit_rate_allows_break_even_at_zero(self) -> None:
        payload = make_payload([(1, 0.5, 2.0), (2, 0.5, 2.0)])
        for rate, eligible in ((0, True), (0.2, False)):
            with self.subTest(rate=rate):
                config = make_config(budget=1000, min_profit_rate=rate)
                result = calculate_dutching_pre(payload, config)
                candidate = result["evaluated_counts"][1]
                self.assertEqual(candidate["minimum_profit"], 0)
                self.assertEqual(candidate["eligible"], eligible)
                config["simulation"]["dutching"]["require_profit_if_hit"] = True
                self.assertEqual(calculate_dutching_pre(payload, config), result)

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
        self.assertEqual(hit["total_refund"], 0)
        self.assertEqual(hit["profit"], 1400)
        self.assertEqual(hit["roi"], 1.4)
        self.assertTrue(hit["selections"][0]["hit"])
        self.assertEqual(hit["selections"][0]["refund"], 0)
        self.assertEqual(hit["selections"][0]["return"], 2400)
        self.assertEqual(hit["selections"][1]["return"], 0)
        self.assertEqual(miss["total_return"], 0)
        self.assertEqual(miss["total_refund"], 0)
        self.assertEqual(miss["profit"], -1000)
        self.assertEqual(empty, {"total_stake": 0, "total_refund": 0, "total_return": 0, "profit": 0, "roi": 0.0, "selections": []})

        mixed_pre = {"selections": [{"horse_number": number, "stake": number * 100} for number in range(1, 7)]}
        mixed_result = make_result(1, 400, list(range(1, 7)))
        for horse in mixed_result["horses"]:
            horse["finish_position"] = {3: "取消", 4: "除外", 5: "中止", 6: "失格"}.get(horse["horse_number"], horse["finish_position"])
        mixed = calculate_post(mixed_pre, mixed_result)
        self.assertEqual(
            [(item["horse_number"], item["hit"], item["refund"], item["return"]) for item in mixed["selections"]],
            [(1, True, 0, 400), (2, False, 0, 0), (3, False, 300, 300),
             (4, False, 400, 400), (5, False, 0, 0), (6, False, 0, 0)],
        )
        self.assertEqual((mixed["total_stake"], mixed["total_refund"], mixed["total_return"], mixed["profit"], mixed["roi"]),
                         (2100, 700, 1100, -1000, round(-1000 / 2100, 6)))
        refund_only = calculate_post({"selections": mixed_pre["selections"][2:4]}, mixed_result)
        self.assertEqual((refund_only["total_refund"], refund_only["total_return"], refund_only["profit"], refund_only["roi"]),
                         (700, 700, 0, 0.0))
        self.assertFalse(any(item["hit"] for item in refund_only["selections"]))
        self.assertFalse(simulation_summary(refund_only)["hit"])
        self.assertEqual(pre, pre_before)

    def test_new_json_structure_post_and_reload(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        payload = ensure_race_payload(payload)
        payload["simulation"] = calculate_pre_simulation(payload, make_config(budget=1000))
        pre_before = copy.deepcopy(payload["simulation"])
        payload["result"] = make_result(1, 400, [1, 2, 3, 4, 5])
        payload["simulation"][0]["general"]["win"]["value"]["post"] = calculate_value_post(payload)
        payload["simulation"][0]["general"]["win"]["dutching"]["post"] = calculate_dutching_post(payload)

        self.assertEqual(set(payload["simulation"][0]), {"prediction_id", "general"})
        self.assertNotIn("pre", payload["simulation"])
        self.assertNotIn("post", payload["simulation"])
        self.assertEqual(payload["simulation"][0]["general"]["win"]["value"]["pre"], pre_before[0]["general"]["win"]["value"]["pre"])
        self.assertEqual(payload["simulation"][0]["general"]["win"]["dutching"]["pre"], pre_before[0]["general"]["win"]["dutching"]["pre"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.json"
            save_race_json(path, payload)
            loaded = load_race_json(path)
        self.assertEqual(set(loaded["simulation"][0]["general"]["win"]), {"value", "dutching"})
        self.assertIsNotNone(loaded["simulation"][0]["general"]["win"]["value"]["post"])
        self.assertIsNotNone(loaded["simulation"][0]["general"]["win"]["dutching"]["post"])

class JavaScriptParityTests(unittest.TestCase):
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
            "dutching_break_even": calculate_dutching_pre(
                make_payload([(1, 0.5, 2.0), (2, 0.5, 2.0)]),
                make_config(budget=1000, min_profit_rate=0),
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
  dutching_break_even: calculateDutchingSimulation(
    [{{horse_number: 1, win_probability: 0.5, win_odds: 2.0}}, {{horse_number: 2, win_probability: 0.5, win_odds: 2.0}}],
    1000, 100, {json.dumps(make_config(min_profit_rate=0)["simulation"]["dutching"])}
  ),
  value: calculateValueSimulation(horses, 1000, 100, valueSettings),
  value_details: calculateValueDetails(horses, 1000, 100, valueSettings),
  value_below_one: calculateValueSimulation(horses, 1000, 100, {{ev_threshold: 0.5, kelly_fraction: 0.5}}),
  value_no_purchase_reason: valueNoPurchaseReason(tinyDetails, 100, 0.5),
  dutching: calculateDutchingSimulation(horses, 1000, 100, {{
    max_selection_count: 5,
    min_coverage_probability: 0.4,
    min_group_expected_value: 0.0,
    min_profit_rate: 0.2,
  }}),
  dutching_strict: calculateDutchingSimulation(horses, 1000, 100, {{
    max_selection_count: 5,
    min_coverage_probability: 0.4,
    min_group_expected_value: 0.0,
    min_profit_rate: 10.0,
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
        javascript_result.pop("value_no_purchase_reason")
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
        # Persisted render snapshots are not part of the interactive calculator's response.
        for key in ("value", "value_below_one"):
            python_result[key].pop("details")
        self.assertEqual(javascript_result, python_result)


if __name__ == "__main__":
    unittest.main()
