from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from render import (  # noqa: E402
    build_environment,
    build_expected_value_rows,
    build_race_context,
    format_jst_datetime,
    index_row_sort_key,
    rank_comparison,
    rejection_reason_text,
    result_highlight_class,
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

    def test_odds_note_requires_timestamp_and_at_least_one_odds_value(self) -> None:
        note = "単勝オッズと人気は記録時点の値です。現在のオッズとは異なる場合があります。"
        payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        payload["race"]["odds_captured_at"] = "2026-01-01T14:30:00+09:00"
        payload["prediction"]["model_provider"] = "manual"
        payload["prediction"]["predicted_at"] = "2026-01-01T14:40:00+09:00"

        rendered = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )
        self.assertIn(note, rendered)
        self.assertNotIn("AI予想生成時刻", rendered)

        for horse in payload["horses"]:
            horse["win_odds"] = None
        rendered_without_odds = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )
        self.assertNotIn(note, rendered_without_odds)

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
        self.assertIn('content="A&amp;B AI予想のAI予想。', rendered)

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

    def test_result_highlight_uses_explicit_priority(self) -> None:
        self.assertEqual(result_highlight_class(1, 1), "prediction-hit")
        self.assertEqual(result_highlight_class(1, 2), "prediction-top")
        self.assertEqual(result_highlight_class(2, 1), "result-winner")
        self.assertEqual(result_highlight_class(2, 2), "")

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
        self.assertIn(
            "オッズ等の市場由来の情報を使用せず、レース条件、過去成績、条件適性、近況、相手関係からAIが1着確率を推定しています。",
            rendered,
        )
        self.assertIn("客観データの比較では3番を上位評価。", rendered)
        self.assertEqual(rendered.count('class="prediction-table" data-sortable'), 2)
        self.assertNotIn("レース結果", rendered)
        self.assertEqual(result_rendered.count('class="result-table" data-sortable'), 2)
        self.assertIn("総合AI予想の予測評価", result_rendered)
        self.assertIn("統計重視予想の予測評価", result_rendered)
        self.assertNotIn("シミュレーション収支", result_rendered)
        self.assertIn(
            "市場情報は予想には使用せず、結果評価の比較基準としてのみ使用しています。",
            result_rendered,
        )
        self.assertIn(
            "このレースでは統計重視予想の購入シミュレーションは記録されていません。",
            result_rendered,
        )
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
            self.assertIn("<title>中央競馬 AI予想レース一覧 | keiba-oracle</title>", index)
            self.assertIn(
                '<meta name="description" content="中央競馬の各レースについて、AIが出走馬の過去成績・条件適性・市場オッズなどから1着確率を推定し、購入シミュレーションを掲載します。">',
                index,
            )
            self.assertIn("<h1>中央競馬 AI予想レース一覧</h1>", index)
            self.assertIn(
                "AIが出走馬の過去成績・条件適性・市場オッズなどを分析し、各馬の1着確率を推定しています。",
                index,
            )
            self.assertIn("<h2>総合AI予想の予測成績</h2>", index)
            self.assertIn("<h2>統計重視予想の予測成績</h2>", index)
            self.assertIn("1位的中率", index)
            self.assertIn("勝ち馬の予想3位内率", index)
            self.assertNotIn("Top5的中率", index)
            self.assertIn("評価済みレースなし", index)
            self.assertLess(index.index('class="panel performance-panel"'), index.index('<table class="index-table">'))
            self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", index)
            self.assertNotIn('class="ai-badge"', index)
            self.assertIn("background: #f2f2f0", index)
            self.assertIn("background: #f2f2f0", race_html)
            self.assertIn("予想済み", index)
            self.assertIn("予想公開", index)
            self.assertIn('class="status status-prediction">予想公開</span>', index)
            self.assertIn("--status-prediction-bg: #dde9e4", index)
            self.assertIn("--status-result-bg: #e4eef3", index)
            self.assertIn('<table class="index-table">', index)
            self.assertIn("overscroll-behavior-inline: contain", index)
            self.assertIn("-webkit-overflow-scrolling: touch", index)
            self.assertIn(".index-table { min-width: 760px; }", index)
            self.assertIn("table { font-size: 13px; }", index)
            self.assertIn('class="nowrap">2026-01-01</td>', index)
            self.assertIn('<th class="nowrap">発走</th>', index)
            self.assertIn('<td class="nowrap">15:30</td>', index)
            self.assertIn('<th class="nowrap">予想</th>', index)
            self.assertIn('<th class="nowrap">結果</th>', index)
            self.assertIn("<td>予想済み</td>", index)
            self.assertIn(
                '<a class="page-link" href="races/2026-01-01/nakayama_11r.html">開く</a>',
                index,
            )
            self.assertIn('<td class="nowrap unavailable-link">-</td>', index)
            self.assertIn(".unavailable-link { text-align: center; }", index)
            page_link_css = index.split(".page-link {", 1)[1].split("}", 1)[0]
            self.assertIn("text-decoration: underline", page_link_css)
            self.assertIn("text-underline-offset: 2px", page_link_css)
            for declaration in ("display:", "padding:", "border:", "background:", "font-weight:"):
                self.assertNotIn(declaration, page_link_css)
            self.assertEqual(
                index.count("AI予想およびシミュレーション結果は、的中や利益を保証するものではありません。"),
                1,
            )
            self.assertNotIn("予想生成", index)
            self.assertNotIn("未予想", index)
            self.assertNotIn("prediction_only", index)
            self.assertTrue((output / "assets" / "site.css").exists())
            self.assertFalse((output / stale.relative_to(public)).exists())
            self.assertFalse((output / "races" / "2026-01-02" / "tokyo_11r.html").exists())
            self.assertFalse((output / "races" / "2026-01-01" / "nakayama_11r_result.html").exists())
            self.assertIn('<div class="page-badges">', race_html)
            self.assertIn('<span class="ai-badge">AI予想</span>', race_html)
            self.assertIn('<span class="status status-prediction">予想公開</span>', race_html)
            self.assertIn("<title>予想済み AI予想 | keiba-oracle</title>", race_html)
            self.assertIn("<h2>予想比較</h2>", race_html)
            self.assertIn("<h3>総合AI予想</h3>", race_html)
            self.assertNotIn("統計重視予想", race_html)
            self.assertIn(
                '<meta name="description" content="予想済みのAI予想。各馬の1着確率、予想理由、単勝分配方式と期待値重視方式による購入シミュレーションを掲載します。">',
                race_html,
            )
            self.assertIn("background: #eee8f6", race_html)
            self.assertIn("color: #604879", race_html)
            self.assertIn("flex-wrap: wrap", race_html)
            self.assertEqual(
                race_html.count(
                    "AIが各馬の過去成績、コース・距離適性、斤量、脚質、市場オッズなどから1着確率を推定しています。"
                ),
                1,
            )
            self.assertIn("--status-prediction-bg: #dde9e4", race_html)
            self.assertIn("--status-result-bg: #e4eef3", race_html)
            self.assertNotIn("prediction_only", race_html)
            self.assertNotIn("AI予想生成時刻", race_html)
            self.assertEqual(
                race_html.count(
                    "本ページはAIによる確率推定と購入シミュレーションを掲載するもので、的中や利益を保証するものではありません。投票はご自身の判断で行ってください。"
                ),
                1,
            )

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

            for html_path in (
                output / "index.html",
                output / "races" / "2026-01-01" / "nakayama_11r.html",
                output / "races" / "2026-01-01" / "nakayama_11r_result.html",
            ):
                content = html_path.read_bytes()
                self.assertNotIn(b"\r\n", content)

            self.assertIn("購入シミュレーション", prediction_html)
            self.assertNotIn('<section class="result-section">', prediction_html)
            self.assertIn('<section class="result-section">', result_html)
            self.assertIn("予測評価", result_html)
            self.assertNotIn("実着順一覧", result_html)
            self.assertGreaterEqual(result_html.count("確定オッズ"), 2)
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
            self.assertIn("<title>結果確認レース 結果 | keiba-oracle</title>", result_html)
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
            self.assertIn("単勝分配方式</strong> -9,670円", index)
            self.assertIn("期待値重視方式</strong> 0円", index)
            self.assertIn("単勝分配方式</strong> -100円", index)
            self.assertIn("期待値重視方式</strong> 300円", index)
            self.assertNotIn("Top5的中率", index)
            self.assertNotIn("80.0%", index)
            self.assertIn("予想済み", index)
            self.assertLess(index.index("予測成績"), index.index("予想済み"))


if __name__ == "__main__":
    unittest.main()
