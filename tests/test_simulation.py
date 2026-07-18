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
    calculate_value_post,
    calculate_value_pre,
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
    def test_app_config_default_ev_threshold_is_one(self) -> None:
        config = load_config(ROOT / "config" / "app.yaml")

        self.assertEqual(float(config["simulation"]["value"]["ev_threshold"]), 1.0)

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
        self.assertEqual([(item["horse_number"], item["stake"]) for item in result["selections"]], [(1, 600), (2, 400)])
        self.assertEqual(result["total_stake"], 1000)
        self.assertTrue(all(item["stake"] >= 100 for item in result["selections"]))
        self.assertLessEqual(
            max(item["estimated_payout"] for item in result["selections"])
            - min(item["estimated_payout"] for item in result["selections"]),
            400,
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

        self.assertEqual(set(payload["simulation"]), {"value", "dutching"})
        self.assertNotIn("pre", payload["simulation"])
        self.assertNotIn("post", payload["simulation"])
        self.assertEqual(payload["simulation"]["value"]["pre"], pre_before["value"]["pre"])
        self.assertEqual(payload["simulation"]["dutching"]["pre"], pre_before["dutching"]["pre"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.json"
            save_race_json(path, payload)
            loaded = load_race_json(path)
        self.assertEqual(set(loaded["simulation"]), {"value", "dutching"})
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

    def test_html_contains_both_methods_and_minimal_public_data(self) -> None:
        payload = self.full_payload()
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))

        for text in (
            "期待値重視方式",
            "上位予測ダッチング方式",
            "頭数別比較",
            "カスタム購入シミュレーション",
            "期待値重視方式の購入結果",
            "ダッチング方式の購入結果",
            "予測評価",
            "予想順位",
            "レース結果",
        ):
            self.assertIn(text, rendered)
        self.assertLess(rendered.index("上位予測ダッチング方式"), rendered.index("<h2>期待値重視方式</h2>"))
        self.assertLess(
            rendered.index("<h2>期待値重視方式</h2>"),
            rendered.index("<h2>カスタム購入シミュレーション</h2>"),
        )
        self.assertIn('<option value="dutching" selected>上位予測ダッチング</option>', rendered)
        self.assertIn('<div class="simulator-field value-field" hidden>', rendered)
        self.assertIn('<label class="simulator-field dutching-field">最大対象頭数', rendered)
        self.assertIn('name="budget" type="number" min="100" step="100" value="1000"', rendered)
        self.assertIn('name="ev_threshold" type="number" min="0" step="0.01" value="1.0"', rendered)
        self.assertIn("最低EVは0以上で入力してください。", rendered)
        self.assertIn('<div class="status race-status status-result">結果公開</div>', rendered)
        self.assertIn("全馬期待値一覧", rendered)
        self.assertIn("EV順位", rendered)
        self.assertIn("最低EV判定", rendered)
        self.assertNotIn("予想生成", rendered)
        self.assertNotIn("結果生成", rendered)
        self.assertIn('<section class="result-section">', rendered)
        self.assertIn('<div class="panel result-panel">', rendered)
        self.assertNotIn("result_published", rendered)
        self.assertNotIn("フィードバック要約", rendered)
        for description in (
            "AIの予測上位馬を複数選び、どの馬が勝っても払戻額が近くなるよう購入額を配分する方式です。",
            "選択した馬の1着確率を合計した値です。",
            "選択馬全体の期待払戻額を合計購入額で割った値です。1.0が損益分岐の目安です。",
            "1着確率と単勝オッズから計算した期待値について、購入対象とする最低ラインです。1.0が損益分岐の目安です。1.0未満も入力できますが、Kelly基準で購入割合が0以下になる馬には購入額を割り当てません。",
            "Kelly基準は、予測確率とオッズから、資金を長期的に効率よく増やすための購入割合を算出する方法です。Kelly係数は、その算出額を実際に何割使うかを示します。0.5なら算出額の半分を使用する「ハーフケリー」、0.25なら4分の1を使用する「クォーターケリー」です。",
            "1着確率×単勝オッズで計算する期待値です。1.0が損益分岐の目安です。",
            "予測確率とオッズからKelly基準で算出した、予算に対する購入割合です。Kelly係数を掛ける前の値で、係数1.0に相当します。",
            "Full KellyにKelly係数を掛けて抑制した購入割合です。係数0.5ならハーフケリーとなります。",
            "選択した馬のうち、最も払戻額が低い馬が的中した場合の払戻額です。",
            "選択した馬のうち、最も利益が低い馬が的中した場合の利益です。",
            "各馬の予測確率を考慮した、平均的な払戻見込み額です。",
        ):
            self.assertIn(description, rendered)
        self.assertIn('class="tooltip-trigger" aria-label=', rendered)
        self.assertIn('.term-tooltip:hover .tooltip-content', rendered)
        self.assertIn('.tooltip-trigger:focus-visible + .tooltip-content', rendered)
        self.assertIn('.term-tooltip[data-open="true"] .tooltip-content', rendered)
        self.assertIn('trigger.addEventListener("click"', rendered)
        match = re.search(
            r'<script type="application/json" id="custom-simulator-data">(.*?)</script>',
            rendered,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(html.unescape(match.group(1)))
        self.assertEqual(set(embedded), {"stake_unit", "horses", "display"})
        self.assertTrue(all(set(item) == {"horse_number", "win_probability", "win_odds"} for item in embedded["horses"]))
        self.assertEqual(
            embedded["display"]["rejection_reason_labels"]["coverage_probability_below_threshold"],
            "カバー確率が最低基準未満",
        )
        self.assertNotIn("localStorage", rendered)
        self.assertNotIn("document.cookie", rendered)
        self.assertNotIn("fetch(", rendered)

    def test_all_horse_expected_values_are_rendered_without_changing_simulation(self) -> None:
        payload = make_payload(
            [(1, 0.3333334, 3.0), (2, 0.25, 4.0), (3, 0.2, 5.0), (4, 0.1, None)]
        )
        payload["simulation"] = calculate_pre_simulation(payload, make_config())
        simulation_before = copy.deepcopy(payload["simulation"])

        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        soup = BeautifulSoup(rendered, "html.parser")
        table = soup.find("h3", string="全馬期待値一覧").find_next("table")
        rows = [[cell.get_text(strip=True) for cell in row.select("td")] for row in table.select("tbody tr")]

        self.assertEqual(len(rows), 4)
        self.assertEqual([row[1] for row in rows], ["1", "2", "3", "4"])
        self.assertEqual([row[0] for row in rows], ["1位", "2位", "3位", "-"])
        self.assertEqual([row[5] for row in rows], ["1.000", "1.000", "1.000", "-"])
        self.assertEqual([row[6] for row in rows], ["基準以上", "基準以上", "基準以上", "算出不可"])
        self.assertEqual(payload["simulation"], simulation_before)

    def test_rejection_reasons_are_localized_for_normal_and_custom_tables(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        payload["simulation"] = calculate_pre_simulation(
            payload,
            make_config(
                budget=100,
                min_coverage_probability=1.0,
                min_group_expected_value=10.0,
            ),
        )
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        normal_section = rendered.split('<div class="panel" id="custom-simulator">', 1)[0]

        for label in (
            "カバー確率が最低基準未満",
            "グループ期待値が最低基準未満",
            "的中時の最低利益を確保できない",
            "予算が購入単位または選択頭数に対して不足",
        ):
            self.assertIn(label, normal_section)
            self.assertIn(label, rendered)
        for internal_value in (
            "coverage_probability_below_threshold",
            "group_expected_value_below_threshold",
            "minimum_profit_not_positive",
            "insufficient_budget_units",
        ):
            self.assertNotIn(internal_value, normal_section)
        self.assertIn("rejectionReasonText(item.rejection_reasons)", rendered)

    def test_result_table_compares_prediction_rank_and_finish(self) -> None:
        payload = make_payload([(1, 0.40, 3.0), (2, 0.35, 4.0), (3, 0.25, 5.0)])
        payload["result"] = make_result(2, 500, [1, 2, 3])
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        soup = BeautifulSoup(rendered, "html.parser")
        table = soup.find("h2", string="実結果").find_next("table")
        headers = [cell.get_text(strip=True) for cell in table.select("thead th")]
        rows = {
            int(cells[0].get_text(strip=True)): (row, [cell.get_text(strip=True) for cell in cells])
            for row in table.select("tbody tr")
            if (cells := row.select("td"))
        }

        self.assertEqual(
            headers,
            ["馬番", "馬名", "予測順位", "1着確率", "実着順", "予想との差", "単勝払戻"],
        )
        self.assertEqual(rows[1][1], ["1", "Horse 1", "1位", "40.0%", "2着", "1着下", "-"])
        self.assertEqual(rows[2][1], ["2", "Horse 2", "2位", "35.0%", "1着", "1着上", "500円"])
        self.assertEqual(rows[3][1], ["3", "Horse 3", "3位", "25.0%", "3着", "差なし", "-"])
        self.assertIn("prediction-top", rows[1][0].get("class", []))
        self.assertIn("result-winner", rows[2][0].get("class", []))
        self.assertNotIn("prediction-hit", rows[1][0].get("class", []))
        self.assertNotIn("prediction-top", rows[2][0].get("class", []))
        self.assertIn("background: #f2f2f0", rendered)
        self.assertIn(".prediction-top { background: #e7f1ec; }", rendered)
        self.assertIn(".result-winner { background: #e4eef3; }", rendered)
        self.assertIn("background: #fff1c9", rendered)
        self.assertIn('class="table-scroll"', str(table.parent))

    def test_prediction_hit_uses_amber_only_in_result_table(self) -> None:
        payload = make_payload([(1, 0.40, 3.0), (2, 0.35, 4.0), (3, 0.25, 5.0)])
        payload["result"] = make_result(1, 300, [1, 2, 3])
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        soup = BeautifulSoup(rendered, "html.parser")
        tables = soup.select("table")
        prediction_row = tables[0].select_one("tbody tr")
        result_table = soup.find("h2", string="実結果").find_next("table")
        result_row = result_table.select_one("tbody tr")

        self.assertEqual(prediction_row.get("class"), ["prediction-top"])
        self.assertEqual(result_row.get("class"), ["prediction-hit"])
        self.assertNotIn("result-winner", result_row.get("class", []))

    def test_no_purchase_post_hides_roi_and_detail_tables(self) -> None:
        payload = make_payload([(1, 0.5, 1.5), (2, 0.5, 1.5)])
        payload["simulation"] = calculate_pre_simulation(
            payload,
            make_config(min_group_expected_value=2.0),
        )
        payload["result"] = make_result(1, 150, [1, 2])
        payload["simulation"]["value"]["post"] = calculate_value_post(payload)
        payload["simulation"]["dutching"]["post"] = calculate_dutching_post(payload)
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))

        value_section = rendered.split("<h2>期待値重視方式の購入結果</h2>", 1)[1].split(
            '<div class="panel result-panel">', 1
        )[0]
        dutching_section = rendered.split("<h2>ダッチング方式の購入結果</h2>", 1)[1].split(
            "</section>", 1
        )[0]
        for section in (value_section, dutching_section):
            self.assertIn("購入なし", section)
            self.assertNotIn("ROI", section)
            self.assertNotIn("<table", section)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript parity")
    def test_python_and_javascript_results_match(self) -> None:
        payload = make_payload(DUTCHING_ROWS)
        config = make_config(budget=1000)
        python_result = {
            "value": calculate_value_pre(payload, config),
            "dutching": calculate_dutching_pre(payload, config),
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
const output = {{
  value: calculateValueSimulation(horses, 1000, 100, {{ev_threshold: 1.0, kelly_fraction: 0.5}}),
  value_below_one: calculateValueSimulation(horses, 1000, 100, {{ev_threshold: 0.5, kelly_fraction: 0.5}}),
  dutching: calculateDutchingSimulation(horses, 1000, 100, {{
    max_selection_count: 5,
    min_coverage_probability: 0.4,
    min_group_expected_value: 0.0,
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
            capture_output=True,
            check=True,
        )
        javascript_result = json.loads(completed.stdout)
        self.assertEqual(javascript_result.pop("minimum_ev_valid"), [0, 0.5, 0.99, 1.0, 1.05])
        self.assertEqual(javascript_result.pop("minimum_ev_invalid"), [None, None, None, None])
        python_result["value_below_one"] = calculate_value_pre(
            payload,
            make_config(budget=1000, ev_threshold=0.5),
        )
        self.assertEqual(javascript_result, python_result)


if __name__ == "__main__":
    unittest.main()
