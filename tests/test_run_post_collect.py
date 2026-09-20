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
import run_post  # noqa: E402
from utils import ensure_race_payload, atomic_write_json, load_race_json  # noqa: E402


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
    def test_cancelled_result_is_persisted_published_and_terminal(self):
        from evaluation_summary import build_evaluation_summary
        from evaluation import build_evaluation
        from simulate import simulate_file
        import scheduler
        from datetime import datetime
        from utils import JST
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for module in ("collect", "simulate", "evaluation", "evaluation_summary", "run_post_collect"):
                stack.enter_context(patch(f"{module}.setup_logger", return_value=self.logger))
            root = Path(tmp)
            config = {"data_dir": str(root / "data"), "public_dir": str(root / "public"),
                      "automation": {"statistical_time": "18:00", "general_minutes_before_start": 45,
                                     "result_minutes_after_start": 15, "retry_interval_minutes": 10,
                                     "max_attempts": 3, "result_retry_interval_minutes": 5, "result_max_attempts": 12}}
            path = root / "data/races/2026-07-26/niigata_7r.json"
            payload = ensure_race_payload(race_payload("202604020207"))
            payload["simulation"] = []
            payload["race"].update(start_time="15:00", race_name="Cancelled test")
            payload["race"]["cancelled"] = True
            atomic_write_json(path, payload)
            with patch.object(collect, "fetch_html", return_value='<div class="RaceNotice">開催中止</div>'):
                self.assertEqual(run_post_collect.run_post_flow(config, "2026-07-26", "test-cancel"), [path])
            saved = load_race_json(path)
            self.assertTrue(saved["race"]["cancelled"])
            self.assertEqual(saved["prediction"], payload["prediction"])
            self.assertEqual(saved["simulation"], payload["simulation"])
            self.assertIsNone(build_evaluation(saved))
            self.assertFalse(simulate_file(path, config, "post", "test-cancel"))
            self.assertEqual(build_evaluation_summary([saved], generated_at="fixed"), build_evaluation_summary([], generated_at="fixed"))
            self.assertTrue((root / "public/races/2026-07-26/niigata_7r.html").exists())
            self.assertFalse((root / "public/races/2026-07-26/niigata_7r_result.html").exists())
            from automation_state import record_failure, load_automation_state
            record_failure(path, config, "202604020207", "general", "old", status="blocked")
            record_failure(path, config, "202604020207", "result", "old", next_retry_at="2026-07-26T16:00:00+09:00")
            with patch.object(scheduler, "run_pre_flow") as pre, patch.object(scheduler, "run_post_flow") as post:
                scheduler.execute_phases(scheduler.create_phase_tasks([{"race_id": "202604020207", "race": saved["race"]}], config), config, datetime(2026, 7, 26, 17, tzinfo=JST))
                pre.assert_not_called(); post.assert_not_called()
            self.assertIsNone(load_automation_state(path, config))

    def test_cancellation_requires_dated_official_notice(self):
        race = {"date": "2026-02-08", "track": "東京", "race_number": 11}
        html = '<div class="InfoArticle"><h1 class="ArticleTitle">8日(日)の東京競馬・京都競馬は中止、小倉競馬は第4レースが取り止めとなります</h1><div class="ArticleInfoData">2026年02月08日</div></div>'
        url = "https://info.netkeiba.com/?pid=info_detail&id=1564"
        self.assertIsNotNone(collect.parse_cancellation_notice(html, race, url))
        self.assertIsNotNone(collect.parse_cancellation_notice(html, {**race, "track": "京都"}, url))
        self.assertIsNone(collect.parse_cancellation_notice(html, {**race, "track": "小倉"}, url))
        self.assertIsNotNone(collect.parse_cancellation_notice(html, {**race, "track": "小倉", "race_number": 4}, url))
        self.assertIsNone(collect.parse_cancellation_notice(html, {**race, "date": "2026-02-09"}, url))
        self.assertIsNone(collect.parse_cancellation_notice(html, {**race, "track": "中山"}, url))
        self.assertIsNone(collect.parse_cancellation_notice(html.replace("東京競馬・京都競馬は中止", "東京競馬の前日発売は中止"), race, url))
        self.assertIsNone(collect.parse_cancellation_notice(html.replace("東京競馬・京都競馬は中止", "東京競馬は中止しません"), race, url))
        self.assertIsNone(collect.parse_cancellation_notice(html, race, "https://example.invalid/notice"))
        self.assertIsNone(collect.parse_cancellation_notice('<div class="RaceData01">レース中止</div>', race, url))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "race.json"
            atomic_write_json(path, race_payload("202604020207"))
            before = path.read_bytes()
            with patch.object(collect, "fetch_html", side_effect=collect.requests.ConnectionError("offline")), patch.object(collect, "setup_logger", return_value=self.logger):
                self.assertEqual(collect.collect_results({"data_dir": tmp}, "test-offline", [path]), [])
            self.assertEqual(path.read_bytes(), before)

    def test_official_notice_fetch_uses_listing_only(self):
        url = "https://info.netkeiba.com/?pid=info_detail&id=1"
        listing = f'<div class="InfoListBox"><a href="{url}">2026年02月08日 東京競馬の開催中止</a><a href="?pid=info_detail&id=3">2026年02月08日 システムメンテナンス</a></div>'
        with patch.object(collect, "fetch_html", side_effect=[listing, "notice1"]) as fetch:
            records = collect.fetch_cancellation_notices(None, since="2026-02-08")
        self.assertEqual(records, [(url, "notice1")])
        self.assertEqual(fetch.call_count, 2)

    def test_notice_fetch_skips_old_and_confirmed_articles(self):
        confirmed = "https://info.netkeiba.com/?pid=info_detail&id=1"
        listing = f'<div class="InfoListBox"><a href="{confirmed}">2026年02月08日 2月8日の開催中止</a><a href="?pid=info_detail&id=2">2026年01月10日 1月10日の開催中止</a></div>'
        with patch.object(collect, "fetch_html", return_value=listing) as fetch:
            self.assertEqual(collect.fetch_cancellation_notices(None, since="2026-02-08", excluded_urls={confirmed}), [])
        fetch.assert_called_once_with(None, "https://info.netkeiba.com/")

    def setUp(self) -> None:
        self.logger = logging.getLogger(f"test.{self.id()}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def test_post_race_id_collects_only_selected_race_and_missing_id_fails(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config = {"data_dir": str(root / "data")}
            target = root / "data/races/2026-07-26/niigata_7r.json"
            other = target.with_name("chukyo_7r.json")
            race_id = "202604020207"
            atomic_write_json(target, race_payload(race_id))
            atomic_write_json(other, race_payload("202607020207"))
            before = other.read_bytes()
            result = {
                "horses": [{"horse_number": n, "finish_position": n} for n in range(1, 15)],
                "finish_order": list(range(1, 15)),
                "payouts": {"win": [{"horse_number": 1, "payout_per_100": 480}]},
            }
            stack.enter_context(patch.object(collect, "setup_logger", return_value=self.logger))
            stack.enter_context(patch.object(run_post_collect, "setup_logger", return_value=self.logger))
            fetch = stack.enter_context(patch.object(collect, "fetch_html", return_value="<html></html>"))
            stack.enter_context(patch.object(collect, "parse_result", return_value=result))
            simulate = stack.enter_context(patch.object(run_post_collect, "simulate_paths", return_value=[target]))
            publish = stack.enter_context(patch.object(run_post_collect, "publish_post_results", return_value=[target]))
            self.assertEqual(run_post_collect.run_post_flow(config, "2026-07-26", "post", race_id=race_id), [target])
            fetch.assert_called_once()
            self.assertIn(f"race_id={race_id}", fetch.call_args.args[1])
            simulate.assert_called_once_with([target], config, "post", "post")
            publish.assert_called_once_with([target], config, "post", race_id=race_id)
            self.assertEqual(other.read_bytes(), before)
            self.assertIsNotNone(load_race_json(target)["result"])
            with self.assertRaises(FileNotFoundError):
                run_post_collect.run_post_flow(config, "2026-07-26", "post", race_id="202604020299")
            self.assertEqual(fetch.call_count, 1)

    def test_statistical_result_without_simulation_is_evaluated_and_published(self):
        from test_evaluation import make_payload
        from utils import load_config, race_json_path, race_result_html_path
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for module in ("run_post_collect", "simulate", "evaluation", "evaluation_summary"):
                stack.enter_context(patch(f"{module}.setup_logger", return_value=self.logger))
            root = Path(tmp)
            config = load_config()
            config.update(data_dir=str(root / "data"), public_dir=str(root / "public"))
            payload = ensure_race_payload(make_payload())
            payload["prediction"][0]["statistical"] = payload["prediction"][0].pop("general")
            payload["simulation"] = []
            race = payload["race"]
            path = race_json_path(config, race["date"], race["track"], race["race_number"])
            atomic_write_json(path, payload)
            with patch.object(run_post_collect, "collect_results", return_value=[path]):
                processed = run_post_collect.run_post_flow(config, race["date"], "test-statistical-post", race_id=payload["meta"]["race_id"])
            self.assertEqual(processed, [path])
            saved = load_race_json(path)
            self.assertEqual(saved["simulation"], [])
            self.assertEqual(saved["prediction"], payload["prediction"])
            self.assertIn("statistical", saved["evaluation"][0])
            self.assertNotIn("general", saved["evaluation"][0])
            self.assertTrue((root / "public" / race_result_html_path(race["date"], race["track"], race["race_number"])).is_file())
            self.assertTrue((root / "public/index.html").is_file())
            self.assertTrue((root / "data/evaluation_summary.json").is_file())

    def test_post_clis_forward_optional_race_id(self):
        for module, job in ((run_post, "post"), (run_post_collect, "post_collect")):
            for race_id in (None, "202604020207"):
                with self.subTest(module=module.__name__, race_id=race_id):
                    argv = ["post", "--date", "2026-07-26"]
                    kwargs = {}
                    if race_id:
                        argv += ["--race-id", race_id]
                        kwargs["race_id"] = race_id
                    with patch.object(sys, "argv", argv), patch.object(module, "load_config", return_value={}), \
                         patch.object(module, "run_post_flow") as flow:
                        module.main()
                        flow.assert_called_once_with({}, "2026-07-26", job, **kwargs)

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
                self.assertEqual(after[key], ensure_race_payload(before)[key])

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
