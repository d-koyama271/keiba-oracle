from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from render import (  # noqa: E402
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
        labels = {
            status: status_label(status)
            for status in ("prediction_only", "result_published", "unknown")
        }

        self.assertTrue(all(labels.values()))
        self.assertTrue(all(label != status for status, label in labels.items()))
        self.assertNotEqual(labels["prediction_only"], labels["result_published"])

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

    def test_new_badge_only_marks_current_week_prediction_pages(self) -> None:
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst)
        race_date = now.date().isoformat()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_dir = root / "data" / "races" / race_date
            race_dir.mkdir(parents=True)

            prediction_payload = make_payload(
                predicted=True,
                track="中山",
                date=race_date,
                name="今週予想レース",
            )
            result_payload = make_payload(
                predicted=True,
                track="東京",
                date=race_date,
                name="今週結果レース",
            )
            for payload in (prediction_payload, result_payload):
                payload["meta"]["created_at"] = now.isoformat(timespec="seconds")
            result_payload["result"] = {
                "finish_order": [1, 2, 3],
                "horses": [
                    {"horse_number": 1, "finish_position": 1},
                    {"horse_number": 2, "finish_position": 2},
                    {"horse_number": 3, "finish_position": 3},
                ],
                "payouts": {"win": [{"horse_number": 1, "payout_per_100": 400}]},
            }
            result_payload["evaluation"] = {
                "winner": {
                    "horse_number": 1,
                    "predicted_probability": 0.4,
                    "predicted_rank": 1,
                },
                "metrics": {
                    "top1_hit": True,
                    "top3_hit": True,
                    "top5_hit": True,
                    "log_loss": 0.916291,
                    "brier_score": 0.24,
                },
                "market_baseline": {"available": False},
            }
            (race_dir / "nakayama_11r.json").write_text(
                json.dumps(prediction_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            (race_dir / "tokyo_11r.json").write_text(
                json.dumps(result_payload, ensure_ascii=False),
                encoding="utf-8",
            )

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-new-badge",
                root=root,
            )
            soup = BeautifulSoup(
                (output / "index.html").read_text(encoding="utf-8"),
                "html.parser",
            )
            prediction_row = soup.find(string="今週予想レース").find_parent("tr")
            result_row = soup.find(string="今週結果レース").find_parent("tr")

            self.assertIsNotNone(prediction_row.select_one(".new-badge"))
            self.assertIsNone(result_row.select_one(".new-badge"))

    def test_recorded_odds_requires_timestamp_and_at_least_one_odds_value(self) -> None:
        payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        payload["race"]["odds_captured_at"] = "2026-01-01T14:30:00+09:00"

        self.assertTrue(build_race_context(payload)["has_recorded_odds"])

        for horse in payload["horses"]:
            horse["win_odds"] = None
        self.assertFalse(build_race_context(payload)["has_recorded_odds"])

    def test_race_title_escapes_race_name(self) -> None:
        payload = make_payload(
            predicted=True,
            track="中山",
            date="2026-01-01",
            name="A&B AI予想",
        )
        rendered = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )
        title = BeautifulSoup(rendered, "html.parser").title.get_text(strip=True)

        self.assertIn("A&B", title)

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
        internal_reasons = [
            "coverage_probability_below_threshold",
            "minimum_profit_not_positive",
        ]
        known = rejection_reason_text(internal_reasons)
        unknown = rejection_reason_text(["unknown_reason"])

        self.assertTrue(known)
        self.assertTrue(unknown)
        self.assertNotEqual(known, unknown)
        self.assertTrue(
            all(reason not in known for reason in internal_reasons),
        )
        self.assertNotIn("unknown_reason", unknown)

    def test_rank_comparison_preserves_direction_and_ignores_non_numeric_finish(self) -> None:
        upward = rank_comparison(3, 1)
        downward = rank_comparison(2, 10)
        same = rank_comparison(5, 5)
        unavailable = rank_comparison(2, "中止")

        self.assertEqual(upward[1], "comparison-up")
        self.assertIn("2", upward[0])
        self.assertEqual(downward[1], "comparison-down")
        self.assertIn("8", downward[0])
        self.assertEqual(same[1], "comparison-neutral")
        self.assertEqual(unavailable[1], "comparison-neutral")
        self.assertNotEqual(same[0], unavailable[0])

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
        prediction_soup = BeautifulSoup(rendered, "html.parser")
        result_soup = BeautifulSoup(result_rendered, "html.parser")

        self.assertEqual(
            [row["prediction_rank"] for row in context["statistical_horse_rows"]],
            [3, 2, 1],
        )
        self.assertEqual(context["statistical_evaluation"]["winner"]["predicted_rank"], 1)
        prediction_panels = prediction_soup.select(
            ".prediction-section [data-ai-panel]"
        )
        result_panels = result_soup.select(".result-section [data-ai-panel]")
        self.assertEqual(
            [panel["data-ai-method"] for panel in prediction_panels],
            ["traditional", "statistical"],
        )
        self.assertEqual(
            [panel["data-ai-method"] for panel in result_panels],
            ["traditional", "statistical"],
        )
        self.assertFalse(prediction_panels[0].has_attr("hidden"))
        self.assertTrue(prediction_panels[1].has_attr("hidden"))
        self.assertEqual(len(prediction_soup.select("table.prediction-table")), 2)
        self.assertEqual(len(result_soup.select("table.result-table")), 2)
        self.assertIn(
            payload["prediction"]["variants"][0]["optional_summary"],
            prediction_panels[1].get_text(" ", strip=True),
        )
        self.assertIsNone(prediction_soup.select_one(".result-section"))
        self.assertIsNone(result_soup.select_one("#custom-simulator"))

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

            index_soup = BeautifulSoup(
                (output / "index.html").read_text(encoding="utf-8"),
                "html.parser",
            )
            race_soup = BeautifulSoup(
                (output / "races" / "2026-01-01" / "nakayama_11r.html").read_text(
                    encoding="utf-8"
                ),
                "html.parser",
            )
            table = index_soup.select_one("table.index-table")
            rows = table.select("tbody tr")
            self.assertEqual(len(rows), 1)
            self.assertIn("予想済み", rows[0].get_text(" ", strip=True))
            self.assertNotIn("未予想", table.get_text(" ", strip=True))
            self.assertEqual(
                [link["href"] for link in rows[0].select("a[href]")],
                ["races/2026-01-01/nakayama_11r.html"],
            )
            self.assertIsNone(rows[0].select("td")[-1].find("a"))
            self.assertTrue((output / "assets" / "site.css").exists())
            self.assertFalse((output / stale.relative_to(public)).exists())
            self.assertFalse((output / "races" / "2026-01-02" / "tokyo_11r.html").exists())
            self.assertFalse((output / "races" / "2026-01-01" / "nakayama_11r_result.html").exists())
            self.assertEqual(
                [
                    panel["data-ai-method"]
                    for panel in race_soup.select(".prediction-section [data-ai-panel]")
                ],
                ["traditional"],
            )
            self.assertIsNone(race_soup.select_one(".result-section"))

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
            prediction_soup = BeautifulSoup(prediction_html, "html.parser")
            result_soup = BeautifulSoup(result_html, "html.parser")
            index_soup = BeautifulSoup(index, "html.parser")

            self.assertIsNotNone(prediction_soup.select_one(".prediction-section"))
            self.assertIsNotNone(prediction_soup.select_one(".simulation-section"))
            self.assertIsNone(prediction_soup.select_one(".result-section"))
            self.assertIsNotNone(result_soup.select_one(".result-section"))
            self.assertIsNotNone(result_soup.select_one(".result-section .metric-grid"))
            self.assertIsNotNone(
                result_soup.select_one('table.result-table td[data-sort-value="5.0"]')
            )
            self.assertEqual(
                [horse["win_odds"] for horse in payload["horses"]],
                prediction_odds_before,
            )
            self.assertIsNone(result_soup.select_one("table.prediction-table"))
            self.assertIsNone(result_soup.select_one("#custom-simulator"))
            row = index_soup.select_one("table.index-table tbody tr")
            self.assertEqual(
                [link["href"] for link in row.select("a[href]")],
                [
                    "races/2026-01-01/nakayama_11r.html",
                    "races/2026-01-01/nakayama_11r_result.html",
                ],
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
            soup = BeautifulSoup(
                (output / "index.html").read_text(encoding="utf-8"),
                "html.parser",
            )
            performance_panels = soup.select(".performance-panel")

            self.assertEqual(len(performance_panels), 2)
            self.assertTrue(
                all(len(panel.select(".performance-item")) == 4 for panel in performance_panels)
            )
            self.assertTrue(
                all(len(panel.select(".profit-amount")) == 2 for panel in performance_panels)
            )
            self.assertTrue(
                all(
                    value.get_text(strip=True) != "-"
                    for panel in performance_panels
                    for value in panel.select(".performance-value")
                )
            )
            self.assertIsNotNone(soup.select_one("table.index-table"))


if __name__ == "__main__":
    unittest.main()
