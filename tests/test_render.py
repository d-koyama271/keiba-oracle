from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from render import (  # noqa: E402
    PREDICTION_METHOD_DESCRIPTIONS,
    build_environment,
    build_expected_value_rows,
    build_race_context,
    build_value_selection_rows,
    format_jst_datetime,
    index_row_sort_key,
    is_created_this_week,
    rank_comparison,
    rejection_reason_text,
    render_site,
    status_class,
    status_label,
)


def make_payload(*, predicted: bool, track: str, date: str, name: str) -> dict:
    horses = [
        {
            "horse_number": number,
            "horse_name": f"Horse {number}",
            "jockey": f"Jockey {number}",
            "weight_carried": 54.0 + number,
            "running_style_summary": f"Style {number}",
            "win_odds": 3.0 + number,
            "popularity": number,
        }
        for number in (3, 1, 2)
    ]
    prediction = None
    if predicted:
        prediction = {
            "horses": [
                {"horse_number": 2, "win_probability": 0.4, "reason": "reason 2"},
                {"horse_number": 1, "win_probability": 0.4, "reason": "reason 1"},
                {"horse_number": 3, "win_probability": 0.2, "reason": "reason 3"},
            ]
        }
    return {
        "meta": {"race_id": f"{date}-{track}", "schema_version": 4},
        "race": {
            "date": date,
            "track": track,
            "race_number": 11,
            "race_name": name,
            "start_time": "15:30",
            "source_url": "https://example.invalid/race",
        },
        "horses": horses,
        "prediction": prediction,
        "simulation": {
            "value": {"pre": None, "post": None},
            "dutching": {"pre": None, "post": None},
        },
        "result": None,
        "evaluation": None,
        "feedback": None,
    }


class RenderTests(unittest.TestCase):
    def test_status_labels_do_not_expose_internal_values(self) -> None:
        self.assertEqual(status_label("prediction_only"), "予想公開")
        self.assertEqual(status_label("result_published"), "結果公開")
        self.assertEqual(status_label("unknown"), "処理中")
        self.assertEqual(status_class("prediction_only"), "status-prediction")
        self.assertEqual(status_class("result_published"), "status-result")
        self.assertEqual(status_class("unknown"), "status-pending")

    def test_index_sort_prioritizes_status_then_latest_start(self) -> None:
        rows = [
            {"name": "result", "status": "result_published", "date": "2026-07-20", "start_time": "16:00", "track": "東京", "href": "result"},
            {"name": "ongoing", "status": "awaiting_result", "date": "2026-07-21", "start_time": "16:00", "track": "中山", "href": "ongoing"},
            {"name": "prediction_old", "status": "prediction_only", "date": "2026-07-18", "start_time": "15:45", "track": "福島", "href": "prediction-old"},
            {"name": "prediction_early", "status": "prediction_only", "date": "2026-07-19", "start_time": "15:20", "track": "函館", "href": "prediction-early"},
            {"name": "prediction_late", "status": "prediction_only", "date": "2026-07-19", "start_time": "15:45", "track": "小倉", "href": "prediction-late"},
        ]

        ordered = sorted(rows, key=index_row_sort_key)

        self.assertEqual(
            [row["name"] for row in ordered],
            ["prediction_late", "prediction_early", "prediction_old", "ongoing", "result"],
        )

    def test_datetime_is_displayed_in_jst_without_changing_source(self) -> None:
        self.assertEqual(format_jst_datetime("2026-07-18T18:07:48+09:00"), "2026-07-18 18:07:48")
        self.assertEqual(format_jst_datetime("2026-07-18T09:07:48+00:00"), "2026-07-18 18:07:48")
        self.assertEqual(format_jst_datetime("2026-07-18T12:07:48+03:00"), "2026-07-18 18:07:48")
        self.assertEqual(format_jst_datetime(None), "-")

        payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        saved_value = "2026-07-18T09:07:48+00:00"
        payload["race"]["odds_captured_at"] = saved_value
        context = build_race_context(payload)

        self.assertEqual(context["odds_captured_at_label"], "2026-07-18 18:07:48")
        self.assertEqual(payload["race"]["odds_captured_at"], saved_value)

    def test_created_this_week_uses_jst_monday_to_sunday(self) -> None:
        jst = timezone(timedelta(hours=9))
        reference = datetime(2026, 8, 30, 12, 0, tzinfo=jst)

        self.assertTrue(is_created_this_week("2026-08-24T00:00:00+09:00", reference))
        self.assertTrue(is_created_this_week("2026-08-30T23:59:59+09:00", reference))
        self.assertTrue(is_created_this_week("2026-08-23T15:00:00+00:00", reference))
        self.assertFalse(is_created_this_week("2026-08-23T23:59:59+09:00", reference))
        self.assertFalse(is_created_this_week("2026-08-31T00:00:00+09:00", reference))
        self.assertFalse(is_created_this_week(None, reference))

    def test_prediction_method_descriptions_match_fixed_spec(self) -> None:
        self.assertEqual(
            PREDICTION_METHOD_DESCRIPTIONS,
            {
                "traditional": "過去成績や今回のレース条件、市場評価などを総合して1着確率を推定しています。",
                "statistical": "市場情報を使用せず、過去成績や今回のレース条件などの客観データから1着確率を推定しています。",
            },
        )

    def test_recorded_odds_requires_timestamp_and_at_least_one_odds_value(self) -> None:
        payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        payload["race"]["odds_captured_at"] = "2026-01-01T14:30:00+09:00"

        self.assertTrue(build_race_context(payload)["has_recorded_odds"])

        for horse in payload["horses"]:
            horse["win_odds"] = None
        self.assertFalse(build_race_context(payload)["has_recorded_odds"])

    def test_race_title_avoids_duplicate_ai_label_and_escapes_description(self) -> None:
        payload = make_payload(
            predicted=True,
            track="中山",
            date="2026-01-01",
            name="A&B AI予想",
        )
        rendered = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )

        self.assertIn("<title>A&amp;B AI予想 | keiba-oracle</title>", rendered)
        self.assertNotIn("AI予想 AI予想", rendered)

    def test_expected_value_rows_use_raw_values_sort_and_handle_missing_odds(self) -> None:
        horse_rows = [
            {"horse_number": 3, "horse_name": "Horse 3", "win_odds": 5.0, "prediction": {"win_probability": 0.2}},
            {"horse_number": 1, "horse_name": "Horse 1", "win_odds": 3.0, "prediction": {"win_probability": 0.3333334}},
            {"horse_number": 4, "horse_name": "Horse 4", "win_odds": None, "prediction": {"win_probability": 0.1}},
            {"horse_number": 2, "horse_name": "Horse 2", "win_odds": 4.0, "prediction": {"win_probability": 0.25}},
        ]
        payload = {
            "horses": [
                {
                    "horse_number": horse["horse_number"],
                    "horse_name": horse["horse_name"],
                    "win_odds": horse["win_odds"],
                }
                for horse in horse_rows
            ],
            "prediction": {
                "horses": [
                    {
                        "horse_number": horse["horse_number"],
                        "win_probability": horse["prediction"]["win_probability"],
                    }
                    for horse in horse_rows
                ]
            },
        }
        value_pre = {
            "budget": 3000,
            "stake_unit": 100,
            "settings": {"ev_threshold": 1.0, "kelly_fraction": 0.5},
            "selections": [],
        }
        rows = build_expected_value_rows(
            payload,
            horse_rows,
            value_pre,
        )

        self.assertEqual([row["horse_number"] for row in rows], [1, 2, 3, 4])
        self.assertEqual([row["ev_rank"] for row in rows], [1, 2, 3, None])
        self.assertEqual(rows[0]["expected_value"], 0.3333334 * 3.0)
        self.assertEqual([row["meets_threshold"] for row in rows], [True, True, True, None])

    def test_value_selection_rows_derive_expected_return_without_mutating_simulation(self) -> None:
        value_pre = {
            "selections": [
                {
                    "horse_number": 3,
                    "stake": 200,
                    "expected_value": 1.234567,
                }
            ]
        }

        rows = build_value_selection_rows(value_pre)

        self.assertEqual(rows[0]["expected_return"], 246.9134)
        self.assertNotIn("expected_return", value_pre["selections"][0])

    def test_rejection_reason_labels_do_not_expose_internal_values(self) -> None:
        self.assertEqual(
            rejection_reason_text(
                [
                    "coverage_probability_below_threshold",
                    "minimum_profit_not_positive",
                ]
            ),
            "カバー確率が最低基準未満、的中時の最低利益を確保できない",
        )
        self.assertEqual(rejection_reason_text(["unknown_reason"]), "条件を満たしていません")

    def test_rank_comparison_uses_japanese_labels_and_ignores_non_numeric_finish(self) -> None:
        self.assertEqual(rank_comparison(3, 1), ("2着上", "comparison-up"))
        self.assertEqual(rank_comparison(2, 10), ("8着下", "comparison-down"))
        self.assertEqual(rank_comparison(5, 5), ("差なし", "comparison-neutral"))
        self.assertEqual(rank_comparison(2, "中止"), ("-", "comparison-neutral"))

    def test_prediction_rank_ties_use_horse_number_without_reordering_rows(self) -> None:
        context = build_race_context(
            make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        )

        self.assertEqual([row["horse_number"] for row in context["horse_rows"]], [1, 2, 3])
        self.assertEqual([row["prediction_rank"] for row in context["horse_rows"]], [1, 2, 3])

    def test_statistical_prediction_and_evaluation_render_separately(self) -> None:
        payload = make_payload(
            predicted=True,
            track="中山",
            date="2026-01-01",
            name="方式比較レース",
        )
        payload["prediction"]["variants"] = [
            {
                "method": "statistical",
                "model_provider": "codex",
                "model_name": "gpt-test",
                "optional_summary": "客観データの比較では3番を上位評価。",
                "horses": [
                    {"horse_number": 1, "win_probability": 0.2, "reason": "条件実績。"},
                    {"horse_number": 2, "win_probability": 0.3, "reason": "近走内容。"},
                    {"horse_number": 3, "win_probability": 0.5, "reason": "相手比較。"},
                ],
            }
        ]
        payload["result"] = {
            "finish_order": [3, 1, 2],
            "horses": [
                {"horse_number": 3, "finish_position": 1},
                {"horse_number": 1, "finish_position": 2},
                {"horse_number": 2, "finish_position": 3},
            ],
            "payouts": {"win": [{"horse_number": 3, "payout_per_100": 500}]},
        }
        payload["evaluation"] = {
            "winner": {"horse_number": 3, "predicted_probability": 0.2, "predicted_rank": 3},
            "metrics": {
                "top1_hit": False,
                "top3_hit": True,
                "top5_hit": True,
                "log_loss": 1.609438,
                "brier_score": 0.24,
            },
            "market_baseline": {"available": False},
            "simulation_results": {
                "value": {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None},
                "dutching": {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None},
            },
            "variants": [
                {
                    "method": "statistical",
                    "model_provider": "codex",
                    "model_name": "gpt-test",
                    "winner": {
                        "horse_number": 3,
                        "predicted_probability": 0.5,
                        "predicted_rank": 1,
                    },
                    "metrics": {
                        "top1_hit": True,
                        "top3_hit": True,
                        "top5_hit": True,
                        "log_loss": 0.693147,
                        "brier_score": 0.126667,
                    },
                    "market_baseline": {"available": False},
                }
            ],
        }

        context = build_race_context(payload)
        prediction_context = {
            **context,
            "page_kind": "prediction",
            "prediction_page_name": "nakayama_11r.html",
            "result_page_name": "nakayama_11r_result.html",
            "status_label": "予想公開",
            "status_class": "status-prediction",
        }
        result_context = {
            **context,
            "page_kind": "result",
            "prediction_page_name": "nakayama_11r.html",
            "result_page_name": "nakayama_11r_result.html",
            "status_label": "結果公開",
            "status_class": "status-result",
        }
        template = build_environment(ROOT).get_template("race.html.j2")
        rendered = template.render(**prediction_context)
        result_rendered = template.render(**result_context)

        self.assertEqual(
            [row["prediction_rank"] for row in context["statistical_horse_rows"]],
            [3, 2, 1],
        )
        self.assertEqual(context["statistical_evaluation"]["winner"]["predicted_rank"], 1)
        self.assertIn("<h2>予想比較</h2>", rendered)
        self.assertIn('data-ai-method="traditional"', rendered)
        self.assertIn('data-ai-method="statistical"', rendered)
        self.assertNotIn("<h3>総合AI予想</h3>", rendered)
        self.assertNotIn("<h3>統計重視予想</h3>", rendered)
        self.assertIn('id="prediction-statistical" role="tabpanel" data-ai-panel data-ai-method="statistical" hidden', rendered)
        self.assertIn(PREDICTION_METHOD_DESCRIPTIONS["statistical"], rendered)
        self.assertIn("客観データの比較では3番を上位評価。", rendered)
        self.assertEqual(rendered.count('class="prediction-table" data-sortable'), 2)
        self.assertNotIn("レース結果", rendered)
        self.assertEqual(result_rendered.count('class="result-table" data-sortable'), 2)
        self.assertIn("総合AI予想の予測評価", result_rendered)
        self.assertIn("統計重視予想の予測評価", result_rendered)
        self.assertNotIn("シミュレーション収支", result_rendered)
        self.assertIn('id="result-statistical"', result_rendered)
        self.assertNotIn("カスタム購入シミュレーション", result_rendered)

    def test_render_only_prediction_races_and_remove_stale_managed_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_dir = root / "data" / "races"
            predicted_path = race_dir / "2026-01-01" / "nakayama_11r.json"
            pending_path = race_dir / "2026-01-02" / "tokyo_11r.json"
            predicted_path.parent.mkdir(parents=True)
            pending_path.parent.mkdir(parents=True)
            predicted_path.write_text(
                json.dumps(
                    make_payload(predicted=True, track="中山", date="2026-01-01", name="予想済み"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            pending_path.write_text(
                json.dumps(
                    make_payload(predicted=False, track="東京", date="2026-01-02", name="未予想"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            public = root / "public"
            stale = public / "races" / "2025-12-31" / "stale_11r.html"
            asset = public / "assets" / "site.css"
            stale.parent.mkdir(parents=True)
            asset.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            asset.write_text("body {}", encoding="utf-8")

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-render",
                root=root,
            )

            index = (output / "index.html").read_text(encoding="utf-8")
            race_html = (output / "races" / "2026-01-01" / "nakayama_11r.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("予想済み", index)
            self.assertIn('<table class="index-table">', index)
            self.assertIn('<th class="nowrap">発走</th>', index)
            self.assertIn('<td class="nowrap">15:30</td>', index)
            self.assertIn('<th class="nowrap">予想</th>', index)
            self.assertIn('<th class="nowrap result-link-column">結果</th>', index)
            self.assertIn(
                '<a class="page-link" href="races/2026-01-01/nakayama_11r.html">開く</a>',
                index,
            )
            self.assertIn('<td class="nowrap result-link-column">-</td>', index)
            self.assertNotIn("未予想", index)
            self.assertNotIn("prediction_only", index)
            self.assertTrue((output / "assets" / "site.css").exists())
            self.assertFalse((output / stale.relative_to(public)).exists())
            self.assertFalse((output / "races" / "2026-01-02" / "tokyo_11r.html").exists())
            self.assertFalse((output / "races" / "2026-01-01" / "nakayama_11r_result.html").exists())
            self.assertIn('class="prediction-table"', race_html)
            self.assertIn('data-ai-method="traditional"', race_html)
            self.assertIn("<h3>総合AI予想</h3>", race_html)
            self.assertNotIn("統計重視予想", race_html)
            self.assertNotIn("prediction_only", race_html)

    def test_result_race_generates_separate_prediction_and_result_pages_and_index_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_path = root / "data" / "races" / "2026-01-01" / "nakayama_11r.json"
            race_path.parent.mkdir(parents=True)
            payload = make_payload(
                predicted=True,
                track="中山",
                date="2026-01-01",
                name="結果確認レース",
            )
            payload["result"] = {
                "finish_order": [2, 1, 3],
                "horses": [
                    {"horse_number": 2, "finish_position": 1},
                    {"horse_number": 1, "finish_position": 2},
                    {"horse_number": 3, "finish_position": 3},
                ],
                "payouts": {"win": [{"horse_number": 2, "payout_per_100": 500}]},
                "final_win_odds": [
                    {"horse_number": 1, "win_odds": 4.4},
                    {"horse_number": 2, "win_odds": 5.0},
                    {"horse_number": 3, "win_odds": 6.2},
                ],
            }
            payload["simulation"]["value"]["post"] = {
                "total_stake": 100,
                "total_return": 500,
                "profit": 400,
                "roi": 5.0,
                "selections": [
                    {"horse_number": 2, "stake": 100, "hit": True, "return": 500}
                ],
            }
            payload["evaluation"] = {
                "winner": {"horse_number": 2, "predicted_probability": 0.4, "predicted_rank": 2},
                "metrics": {
                    "top1_hit": False,
                    "top3_hit": True,
                    "top5_hit": True,
                    "log_loss": 0.916291,
                    "brier_score": 0.24,
                },
                "market_baseline": {"available": False},
                "simulation_results": {
                    "value": {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None},
                    "dutching": {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None},
                },
            }
            race_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-render-result",
                root=root,
            )
            prediction_odds_before = [horse["win_odds"] for horse in payload["horses"]]
            prediction_html = (output / "races" / "2026-01-01" / "nakayama_11r.html").read_text(encoding="utf-8")
            result_html = (output / "races" / "2026-01-01" / "nakayama_11r_result.html").read_text(encoding="utf-8")
            index = (output / "index.html").read_text(encoding="utf-8")

            self.assertIn("購入シミュレーション", prediction_html)
            self.assertNotIn('<section class="panel result-section', prediction_html)
            self.assertIn('<section class="panel result-section">', result_html)
            self.assertIn("予測評価", result_html)
            self.assertNotIn("実着順一覧", result_html)
            self.assertIn("確定オッズ", result_html)
            self.assertNotIn("確定単勝オッズ", result_html)
            self.assertIn(">5.0</td><td class=\"nowrap\">的中</td>", result_html)
            self.assertNotIn(">yes<", result_html)
            self.assertNotIn(">no<", result_html)
            self.assertNotIn('class="stats"', result_html)
            self.assertIn('class="metric-grid"', result_html)
            self.assertNotIn("シミュレーション収支", result_html)
            self.assertEqual(
                [horse["win_odds"] for horse in payload["horses"]],
                prediction_odds_before,
            )
            self.assertNotIn('class="prediction-table"', result_html)
            self.assertNotIn("カスタム購入シミュレーション", result_html)
            self.assertIn(
                '<a class="page-link" href="races/2026-01-01/nakayama_11r.html">開く</a>',
                index,
            )
            self.assertIn(
                '<a class="page-link" href="races/2026-01-01/nakayama_11r_result.html">開く</a>',
                index,
            )

    def test_index_renders_generated_evaluation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_path = root / "data" / "races" / "2026-01-01" / "nakayama_11r.json"
            race_path.parent.mkdir(parents=True)
            race_path.write_text(
                json.dumps(
                    make_payload(predicted=True, track="中山", date="2026-01-01", name="予想済み"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary_path = root / "data" / "evaluation_summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "overall": {
                            "evaluated_races": 5,
                            "top1_hits": 1,
                            "top1_hit_rate": 0.2,
                            "top3_hits": 3,
                            "top3_hit_rate": 0.6,
                            "top5_hits": 4,
                            "top5_hit_rate": 0.8,
                            "average_winner_predicted_rank": 3.4,
                        },
                        "simulation": {
                            "value": {"simulation_races": 5, "cumulative_profit": 0},
                            "dutching": {"simulation_races": 5, "cumulative_profit": -9670},
                        },
                        "methods": {
                            "traditional": {
                                "overall": {
                                    "evaluated_races": 5,
                                    "top1_hits": 1,
                                    "top1_hit_rate": 0.2,
                                    "top3_hits": 3,
                                    "top3_hit_rate": 0.6,
                                    "top5_hits": 4,
                                    "top5_hit_rate": 0.8,
                                    "average_winner_predicted_rank": 3.4,
                                }
                            },
                            "statistical": {
                                "overall": {
                                    "evaluated_races": 2,
                                    "top1_hits": 1,
                                    "top1_hit_rate": 0.5,
                                    "top3_hits": 2,
                                    "top3_hit_rate": 1.0,
                                    "top5_hits": 2,
                                    "top5_hit_rate": 1.0,
                                    "average_winner_predicted_rank": 1.5,
                                },
                                "simulation": {
                                    "value": {"simulation_races": 1, "cumulative_profit": 300},
                                    "dutching": {"simulation_races": 1, "cumulative_profit": -100},
                                },
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-render-summary",
                root=root,
            )
            index = (output / "index.html").read_text(encoding="utf-8")

            self.assertIn("20.0%", index)
            self.assertIn("1 / 5レース", index)
            self.assertIn("60.0%", index)
            self.assertIn("3 / 5レース", index)
            self.assertIn("3.4位", index)
            self.assertIn("50.0%", index)
            self.assertIn("1 / 2レース", index)
            self.assertIn("100.0%", index)
            self.assertIn("2 / 2レース", index)
            self.assertIn("<span>単勝分配方式</span>", index)
            self.assertIn('<strong class="profit-amount profit-negative">-9,670円</strong>', index)
            self.assertIn('<strong class="profit-amount profit-neutral">0円</strong>', index)
            self.assertIn('<strong class="profit-amount profit-negative">-100円</strong>', index)
            self.assertIn('<strong class="profit-amount profit-positive">300円</strong>', index)
            self.assertNotIn("Top5的中率", index)
            self.assertNotIn("80.0%", index)
            self.assertIn("予想済み", index)
            self.assertLess(index.index("予測成績"), index.index("予想済み"))


if __name__ == "__main__":
    unittest.main()
