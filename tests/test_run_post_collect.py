from __future__ import annotations

import copy
import logging
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collect  # noqa: E402
import run_post_collect  # noqa: E402
from utils import atomic_write_json, load_race_json  # noqa: E402


def race_payload(race_id: str, horse_count: int = 14) -> dict:
    return {
        "meta": {"race_id": race_id},
        "race": {"date": "2026-07-26", "track": "新潟", "race_number": 7},
        "horses": [
            {"horse_number": number, "horse_name": f"horse-{number}", "win_odds": 5.0}
            for number in range(1, horse_count + 1)
        ],
        "prediction": {"horses": [{"horse_number": 1, "win_probability": 1.0, "reason": "test"}]},
        "simulation": {
            "value": {"pre": {"selections": []}, "post": None},
            "dutching": {"pre": {"selections": []}, "post": None},
            "variants": [],
        },
        "result": None,
        "evaluation": None,
    }


class ResultCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger(f"test.{self.id()}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def test_result_collection_updates_only_result_for_existing_race_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "races" / "2026-07-26" / "niigata_7r.json"
            payload = race_payload("202604020207")
            atomic_write_json(path, payload)
            before = copy.deepcopy(payload)
            result = {
                "fetched_at": "2026-07-26T16:00:00+09:00",
                "finish_order": list(range(1, 15)),
                "horses": [
                    {"horse_number": number, "finish_position": number}
                    for number in range(1, 15)
                ],
                "payouts": {"win": [{"horse_number": 1, "payout_per_100": 480}]},
                "final_win_odds": [
                    {"horse_number": number, "win_odds": 4.0 + number}
                    for number in range(1, 15)
                ],
            }

            with ExitStack() as stack:
                stack.enter_context(patch.object(collect, "setup_logger", return_value=self.logger))
                fetch = stack.enter_context(patch.object(collect, "fetch_html", return_value="<html></html>"))
                stack.enter_context(patch.object(collect, "parse_result", return_value=result))
                updated = collect.collect_results({}, "test-post", [path])

            after = load_race_json(path)
            self.assertEqual(updated, [path])
            self.assertIn("race_id=202604020207", fetch.call_args.args[1])
            self.assertEqual(after["result"], result)
            for key in ("race", "horses", "prediction", "simulation"):
                self.assertEqual(after[key], before[key])

    def test_incomplete_result_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "races" / "2026-07-26" / "niigata_7r.json"
            payload = race_payload("202604020207")
            atomic_write_json(path, payload)
            incomplete = {
                "fetched_at": "2026-07-26T16:00:00+09:00",
                "finish_order": [1, 2, 3, 4, 5],
                "horses": [
                    {"horse_number": number, "finish_position": number}
                    for number in range(1, 6)
                ],
                "payouts": {"win": [{"horse_number": 1, "payout_per_100": 480}]},
            }

            with ExitStack() as stack:
                stack.enter_context(patch.object(collect, "setup_logger", return_value=self.logger))
                stack.enter_context(patch.object(collect, "fetch_html", return_value="<html></html>"))
                stack.enter_context(patch.object(collect, "parse_result", return_value=incomplete))
                updated = collect.collect_results({}, "test-post", [path])

            self.assertEqual(updated, [])
            self.assertIsNone(load_race_json(path)["result"])


class PostFlowTargetTests(unittest.TestCase):
    def test_post_flow_uses_race_ids_from_predicted_race_jsons(self) -> None:
        niigata = Path("data/races/2026-07-26/niigata_7r.json")
        chukyo = Path("data/races/2026-07-26/chukyo_7r.json")
        pending = Path("data/races/2026-07-26/sapporo_11r.json")
        payloads = {
            niigata: race_payload("202604020207"),
            chukyo: race_payload("202607020207"),
            pending: {**race_payload("202601010111"), "prediction": None},
        }

        with ExitStack() as stack:
            stack.enter_context(patch.object(run_post_collect, "setup_logger"))
            stack.enter_context(
                patch.object(
                    run_post_collect,
                    "list_race_files",
                    return_value=[niigata, chukyo, pending],
                )
            )
            stack.enter_context(
                patch.object(
                    run_post_collect,
                    "load_race_json",
                    side_effect=lambda path: payloads[path],
                )
            )
            collect_results = stack.enter_context(
                patch.object(
                    run_post_collect,
                    "collect_results",
                    return_value=[niigata, chukyo],
                )
            )
            stack.enter_context(
                patch.object(
                    run_post_collect,
                    "simulate_paths",
                    return_value=[niigata, chukyo],
                )
            )
            publish = stack.enter_context(
                patch.object(
                    run_post_collect,
                    "publish_post_results",
                    return_value=[niigata, chukyo],
                )
            )
            updated = run_post_collect.run_post_flow({}, "2026-07-26", "test-post")

        self.assertEqual(updated, [niigata, chukyo])
        self.assertEqual(collect_results.call_args.args[2], [niigata, chukyo])
        self.assertEqual(publish.call_args.args[0], [niigata, chukyo])


if __name__ == "__main__":
    unittest.main()
