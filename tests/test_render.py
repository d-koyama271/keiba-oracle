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
    build_expected_value_rows,
    build_race_context,
    format_jst_datetime,
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

    def test_expected_value_rows_use_raw_values_sort_and_handle_missing_odds(self) -> None:
        rows = build_expected_value_rows(
            [
                {"horse_number": 3, "horse_name": "Horse 3", "win_odds": 5.0, "prediction": {"win_probability": 0.2}},
                {"horse_number": 1, "horse_name": "Horse 1", "win_odds": 3.0, "prediction": {"win_probability": 0.3333334}},
                {"horse_number": 4, "horse_name": "Horse 4", "win_odds": None, "prediction": {"win_probability": 0.1}},
                {"horse_number": 2, "horse_name": "Horse 2", "win_odds": 4.0, "prediction": {"win_probability": 0.25}},
            ],
            1.0,
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
            self.assertIn("中央競馬 予想レース一覧", index)
            self.assertIn("background: #f2f2f0", index)
            self.assertIn("background: #f2f2f0", race_html)
            self.assertIn("予想済み", index)
            self.assertIn("予想公開", index)
            self.assertIn('class="status status-prediction">予想公開</span>', index)
            self.assertIn("--status-prediction-bg: #dde9e4", index)
            self.assertIn("--status-result-bg: #e4eef3", index)
            self.assertNotIn("予想生成", index)
            self.assertNotIn("未予想", index)
            self.assertNotIn("prediction_only", index)
            self.assertTrue((output / "assets" / "site.css").exists())
            self.assertFalse((output / stale.relative_to(public)).exists())
            self.assertFalse((output / "races" / "2026-01-02" / "tokyo_11r.html").exists())
            self.assertIn('<div class="status race-status status-prediction">予想公開</div>', race_html)
            self.assertIn("--status-prediction-bg: #dde9e4", race_html)
            self.assertIn("--status-result-bg: #e4eef3", race_html)
            self.assertNotIn("prediction_only", race_html)


if __name__ == "__main__":
    unittest.main()
