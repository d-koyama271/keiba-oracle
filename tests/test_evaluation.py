from __future__ import annotations

import copy
import logging
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import run_post_collect  # noqa: E402
import watcher  # noqa: E402
from evaluation import build_evaluation, evaluate_file  # noqa: E402
from render import build_environment, build_race_context  # noqa: E402
from utils import load_race_json, save_race_json  # noqa: E402


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
            evaluation["winner"],
            {"horse_number": 2, "predicted_probability": 0.4, "predicted_rank": 2},
        )
        self.assertAlmostEqual(evaluation["metrics"]["log_loss"], -math.log(0.4), places=6)
        self.assertEqual(evaluation["metrics"]["brier_score"], 0.186667)
        self.assertFalse(evaluation["metrics"]["top1_hit"])
        self.assertTrue(evaluation["metrics"]["top3_hit"])
        self.assertTrue(evaluation["metrics"]["top5_hit"])

        market = evaluation["market_baseline"]
        self.assertTrue(market["available"])
        self.assertEqual(market["winner_rank"], 2)
        self.assertEqual(market["winner_probability"], 0.263158)
        self.assertEqual(
            market["model_log_loss_difference"],
            round(evaluation["metrics"]["log_loss"] - market["log_loss"], 6),
        )
        self.assertEqual(
            market["model_brier_difference"],
            round(evaluation["metrics"]["brier_score"] - market["brier_score"], 6),
        )
        self.assertTrue(market["odds_recorded_after_start"])
        self.assertIsNotNone(market["comparison_note"])

        self.assertEqual(
            evaluation["simulation_results"]["value"],
            {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None, "hit": False},
        )
        self.assertEqual(
            evaluation["simulation_results"]["dutching"],
            {"total_stake": 1000, "total_return": 4000, "profit": 3000, "roi": 3.0, "hit": True},
        )

    def test_market_is_unavailable_when_any_odds_are_missing(self) -> None:
        evaluation = build_evaluation(make_payload(missing_odds=True))

        self.assertEqual(evaluation["market_baseline"], {"available": False})

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

        self.assertEqual(loaded["prediction"], prediction_before)
        self.assertEqual(loaded["result"], result_before)
        self.assertEqual(loaded["simulation"]["value"]["pre"], pre_before["value"])
        self.assertEqual(loaded["simulation"]["dutching"]["pre"], pre_before["dutching"])
        self.assertIsNotNone(loaded["evaluation"])
        self.assertNotIn("feedback", loaded)


class EvaluationFlowTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in ("test-evaluation", "test-post-publish", "test-watcher"):
            close_logger(name)

    def test_post_publish_sets_status_without_feedback_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = test_config(root)
            path = root / "race.json"
            save_race_json(path, make_payload())
            try:
                with patch.object(run_post_collect, "render_site") as render, patch.object(
                    run_post_collect,
                    "publish_site",
                    return_value=root / "public",
                ) as publish:
                    updated = run_post_collect.publish_post_results(
                        [path],
                        config,
                        "test-post-publish",
                        root,
                    )

                loaded = load_race_json(path)
                self.assertEqual(updated, [path])
                self.assertEqual(loaded["meta"]["post_status"], "published")
                self.assertIsNotNone(loaded["evaluation"])
                self.assertFalse((root / "outbox" / "chat_input" / "feedback").exists())
                render.assert_called_once_with(config, "test-post-publish", None, root)
                publish.assert_called_once_with(config, root)
            finally:
                close_logger("test-post-publish")

    def test_watcher_only_checks_prediction_inbox(self) -> None:
        logger = logging.getLogger("test.watcher.stub")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        with patch.object(watcher, "setup_logger", return_value=logger), patch.object(
            watcher,
            "inbox_files",
            return_value=[],
        ) as inbox_files:
            self.assertEqual(watcher.process_once({"data_dir": "data"}, "test-watcher"), 0)

        inbox_files.assert_called_once_with("prediction")

    def test_result_html_contains_evaluation_without_feedback(self) -> None:
        payload = make_payload()
        payload["evaluation"] = build_evaluation(payload)
        payload["simulation"]["value"]["pre"] = None
        payload["simulation"]["dutching"]["pre"] = None
        rendered = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )

        for text in (
            "予測評価",
            "勝ち馬の予測順位",
            "Log Loss",
            "Brier Score",
            "市場ベースライン比較",
            "正式な発走前性能比較ではありません",
        ):
            self.assertIn(text, rendered)
        self.assertNotIn("フィードバック要約", rendered)
        self.assertEqual(build_race_context(payload)["status"], "result_published")


if __name__ == "__main__":
    unittest.main()
