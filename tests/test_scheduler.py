from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import scheduler
from utils import JST, atomic_write_json, race_json_path, outbox_chat_input_dir, load_race_json
from automation_state import record_failure, automation_state_path, load_automation_state


class SchedulerTests(unittest.TestCase):
    def test_official_cancellation_and_replacement_keep_original_prediction(self):
        from utils import ensure_race_payload, load_race_json
        from automation_state import load_automation_state
        with tempfile.TemporaryDirectory() as tmp:
            config = {**self.config, "data_dir": tmp}
            race = {"date": "2026-02-08", "track": "東京", "race_number": 11, "start_time": "15:40"}
            config["target_races"] = [race["track"]]
            path = race_json_path(config, race["date"], race["track"], 11)
            payload = ensure_race_payload(None, "202605010411")
            payload["race"] = race
            payload["prediction"] = [{"id": "p1", "statistical": {"horses": []}}]
            atomic_write_json(path, payload)
            record_failure(path, config, "202605010411", "general", "failed", status="blocked")
            html = '<div class="InfoArticle"><h1 class="ArticleTitle">8日(日)の東京競馬は中止</h1><div class="ArticleInfoData">2026年02月08日</div><div class="InfoArticle_Body"><p class="ArticleMainText">第1回東京競馬第4日（代替競馬） 2月10日（火曜）</p></div></div>'
            notice = [("https://info.netkeiba.com/?pid=info_detail&id=1564", html)]
            with patch.object(scheduler, "fetch_cancellation_notices", return_value=notice) as notices, patch.object(scheduler, "publish_post_results") as publish:
                # The race disappeared from discovery, but its saved record still receives the notice.
                scheduler.update_race_cancellations([], config, datetime(2026, 2, 8, 9, tzinfo=JST), Path(tmp))
                saved = load_race_json(path)
                self.assertTrue(saved["race"]["cancelled"])
                self.assertEqual(saved["prediction"], payload["prediction"])
                self.assertEqual(saved["race"]["cancellation"]["replacement_date"], "2026-02-10")
                self.assertIsNone(load_automation_state(path, config))
                publish.assert_called_once()
                replacement = {"race_id": "202605010411", "race": {**race, "date": "2026-02-10"}}
                scheduler.update_race_cancellations([replacement], config, datetime(2026, 2, 9, 18, tzinfo=JST), Path(tmp))
                self.assertEqual(notices.call_count, 1)
                self.assertNotIn("source_urls", notices.call_args.kwargs)
                new_path = race_json_path(config, "2026-02-10", "東京", 11)
                self.assertEqual(load_race_json(new_path)["race"]["rescheduled_from"], "2026-02-08")
                self.assertTrue(load_race_json(path)["race"]["cancelled"])
                input_path = outbox_chat_input_dir("prediction", Path(tmp)) / f"{new_path.stem}.json"
                atomic_write_json(input_path, {"race": race})
                decisions = scheduler.decide_phases([replacement], config, datetime(2026, 2, 10, 15, tzinfo=JST), Path(tmp))
                self.assertTrue(next(d for d in decisions if d["phase"] == "general")["runnable"])
                self.assertEqual(next(d for d in decisions if d["phase"] == "general")["mode"], "normal")
                atomic_write_json(input_path, {"race": replacement["race"]})
                decisions = scheduler.decide_phases([replacement], config, datetime(2026, 2, 10, 15, tzinfo=JST), Path(tmp))
                self.assertEqual(next(d for d in decisions if d["phase"] == "general")["mode"], "resume")
            before = path.read_bytes()
            with patch.object(scheduler, "fetch_cancellation_notices", side_effect=scheduler.requests.ConnectionError("offline")):
                scheduler.update_race_cancellations([], config, datetime(2026, 2, 10, 15, tzinfo=JST))
            self.assertEqual(path.read_bytes(), before)

    def setUp(self):
        self.enterContext(patch.object(scheduler, "fetch_cancellation_notices", return_value=[]))
        self.config = {
            "target_races": ["中山"], "odds_reference_minutes_before_start": 60,
            "automation": {"discovery_interval_minutes": 60, "statistical_time": "18:00", "general_minutes_before_start": 45,
                           "result_minutes_after_start": 10},
        }

    def test_cli_reuses_discovery_but_checks_phases_every_ten_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            now = datetime(2026, 9, 20, 12, tzinfo=JST)
            with patch.object(sys, "argv", ["scheduler.py", "--execute"]), \
                 patch.object(scheduler, "load_config", return_value=self.config), \
                 patch.object(scheduler, "discover_scheduled_races", return_value=[]) as discover, \
                 patch.object(scheduler, "execute_phases") as execute, \
                 patch.object(scheduler, "deploy_site") as deploy:
                for minutes in range(0, 71, 10):
                    with patch.object(scheduler, "now_jst", return_value=now + timedelta(minutes=minutes)):
                        scheduler.main()
                self.assertEqual(discover.call_count, 1)
                self.assertEqual(execute.call_count, 8)
                self.assertEqual(deploy.call_count, 8)
                with patch.object(scheduler, "now_jst", return_value=now + timedelta(days=1)):
                    scheduler.main()
                self.assertEqual(discover.call_count, 2)

    def test_cancellation_hourly_attempts_include_network_failures(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                self.config["data_dir"] = tmp
                now = datetime(2026, 9, 20, 12, tzinfo=JST)
                race = {"date": "2026-09-20", "track": self.config["target_races"][0],
                        "race_number": 11, "start_time": "15:40"}
                items = [{"race_id": "202606040711", "race": race}]
                with patch.object(scheduler, "fetch_cancellation_notices", return_value=[],
                                  side_effect=scheduler.requests.ConnectionError("offline") if failure else None) as fetch:
                    for minutes in range(0, 60, 10):
                        scheduler.update_race_cancellations(items, self.config, now + timedelta(minutes=minutes))
                    self.assertEqual(fetch.call_count, 1)
                    scheduler.update_race_cancellations(items, self.config, now + timedelta(minutes=60))
                    self.assertEqual(fetch.call_count, 2)
                    self.assertEqual(len(fetch.call_args.args), 1)

    def test_cancellation_skips_tomorrow_cancelled_and_result_races(self):
        from utils import ensure_race_payload
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            race = {"date": "2026-09-21", "track": self.config["target_races"][0],
                    "race_number": 11, "start_time": "15:40"}
            item = {"race_id": "202606040711", "race": race}
            with patch.object(scheduler, "fetch_cancellation_notices") as fetch:
                scheduler.update_race_cancellations([item], self.config, datetime(2026, 9, 20, 12, tzinfo=JST))
                race["date"] = "2026-09-20"
                path = race_json_path(self.config, race["date"], race["track"], 11)
                payload = ensure_race_payload(None, item["race_id"])
                payload["race"] = race
                payload["result"] = {"horses": [1]}
                atomic_write_json(path, payload)
                scheduler.update_race_cancellations([item], self.config, datetime(2026, 9, 20, 12, tzinfo=JST))
                payload["result"] = None
                payload["race"]["cancelled"] = True
                payload["race"]["cancellation"] = {"source_url": "https://info.netkeiba.com/?id=1"}
                atomic_write_json(path, payload)
                scheduler.update_race_cancellations([item], self.config, datetime(2026, 9, 20, 13, tzinfo=JST))
                fetch.assert_not_called()

    def test_schedule_uses_independent_settings(self):
        race = {"date": "2026-09-20", "start_time": "15:40"}
        before = copy.deepcopy(race)
        self.assertEqual(scheduler.calculate_phase_times(race, self.config), {
            "statistical": datetime(2026, 9, 19, 18, tzinfo=JST),
            "general": datetime(2026, 9, 20, 14, 55, tzinfo=JST),
            "result": datetime(2026, 9, 20, 15, 50, tzinfo=JST),
        })
        self.config["automation"].update(statistical_time="19:30", general_minutes_before_start=30,
                                          result_minutes_after_start=20)
        times = scheduler.calculate_phase_times(race, self.config)
        self.assertEqual(times["statistical"], datetime(2026, 9, 19, 19, 30, tzinfo=JST))
        self.assertEqual(times["general"], datetime(2026, 9, 20, 15, 10, tzinfo=JST))
        self.assertEqual(times["result"], datetime(2026, 9, 20, 16, tzinfo=JST))
        self.assertEqual(self.config["odds_reference_minutes_before_start"], 60)
        self.assertEqual(race, before)

    def test_schedule_crosses_day_and_year_boundaries(self):
        early = scheduler.calculate_phase_times({"date": "2027-01-01", "start_time": "00:20"}, self.config)
        self.assertEqual(early["statistical"], datetime(2026, 12, 31, 18, tzinfo=JST))
        self.assertEqual(early["general"], datetime(2026, 12, 31, 23, 35, tzinfo=JST))
        late = scheduler.calculate_phase_times({"date": "2026-12-31", "start_time": "23:55"}, self.config)
        self.assertEqual(late["result"], datetime(2027, 1, 1, 0, 5, tzinfo=JST))

    def test_discovers_today_tomorrow_grades_only_including_monday(self):
        # Sunday 15:30 UTC is already Monday 00:30 JST.
        now = datetime(2026, 9, 20, 15, 30, tzinfo=timezone.utc)
        html = '<div class="RaceName">Test G2</div><div class="RaceData01">15:40 芝2200m</div>'
        with patch.object(scheduler, "discover_race_ids", side_effect=[
            ["202606040711", "202609040711"], ["202606040810"],
        ]) as discover, patch.object(scheduler, "fetch_html", return_value=html) as fetch:
            races = scheduler.discover_scheduled_races(self.config, now)
        self.assertEqual([c.args[1] for c in discover.call_args_list], ["2026-09-21", "2026-09-22"])
        for invocation in discover.call_args_list:
            self.assertEqual(invocation.kwargs, {"race_number": None, "graded_only": True})
        self.assertEqual([item["race_id"] for item in races], ["202606040711", "202606040810"])
        self.assertEqual([item["race"]["date"] for item in races], ["2026-09-21", "2026-09-22"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(races[0]["scheduled_at"]["statistical"], datetime(2026, 9, 20, 18, tzinfo=JST))
        self.assertEqual(races[1]["race"]["race_number"], 10)

    def test_no_grades_is_empty_without_fallback(self):
        with patch.object(scheduler, "now_jst", return_value=datetime(2026, 9, 21, tzinfo=JST)), \
             patch.object(scheduler, "discover_race_ids", return_value=[]) as discover, \
             patch.object(scheduler, "fetch_html") as fetch:
            self.assertEqual(scheduler.discover_scheduled_races(self.config), [])
        self.assertEqual(discover.call_count, 2)
        self.assertEqual([c.args[1] for c in discover.call_args_list], ["2026-09-21", "2026-09-22"])
        fetch.assert_not_called()

    def test_invalid_start_or_settings_raise(self):
        with self.assertRaises(ValueError):
            scheduler.calculate_phase_times({"date": "2026-09-20", "start_time": None}, self.config)
        self.config["automation"]["general_minutes_before_start"] = -1
        with self.assertRaises(ValueError):
            scheduler.calculate_phase_times({"date": "2026-09-20", "start_time": "15:40"}, self.config)

    def test_discovery_cache_reuse_and_due_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            now = datetime(2026, 9, 20, 14, 30, tzinfo=JST)
            race = {"date": "2026-09-20", "start_time": "15:40", "track": "中山", "race_number": 11}
            races = [{"race_id": "202606040711", "race": race,
                      "scheduled_at": scheduler.calculate_phase_times(race, self.config)}]
            with patch.object(scheduler, "discover_scheduled_races", return_value=races) as discover:
                self.assertEqual(scheduler.discover_cached_races(self.config, now), races)
                path = Path(tmp) / "automation" / "discovery_cache.json"
                before = path.read_bytes()
                later = now + timedelta(minutes=30)
                cached = scheduler.discover_cached_races(self.config, later)
                self.assertEqual(cached, races)
                self.assertEqual(discover.call_count, 1)
                self.assertEqual(path.read_bytes(), before)
                decisions = scheduler.decide_phases(cached, self.config, later)
                self.assertTrue(next(d for d in decisions if d["phase"] == "general")["runnable"])
                scheduler.discover_cached_races(self.config, now + timedelta(minutes=60))
                self.assertEqual(discover.call_count, 1)

    def test_discovery_rebuilds_broken_or_incomplete_cache(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            now = datetime(2026, 9, 20, 12, tzinfo=JST)
            path = Path(tmp) / "automation/discovery_cache.json"
            with patch.object(scheduler, "discover_scheduled_races", return_value=[]) as discover:
                scheduler.discover_cached_races(self.config, now)
                valid = json.loads(path.read_text(encoding="utf-8"))
                missing_date = {**valid, "dates": ["2026-09-20"]}
                missing_start = {**valid, "races": [{"race_id": "202606040711", "race": {
                    "date": "2026-09-20", "track": self.config["target_races"][0], "race_number": 11,
                }}]}
                for invalid in (missing_date, missing_start):
                    atomic_write_json(path, invalid)
                    scheduler.discover_cached_races(self.config, now)
                path.write_text("{broken", encoding="utf-8")
                scheduler.discover_cached_races(self.config, now)
                self.assertEqual(discover.call_count, 4)

    def test_discovery_cache_rollover_failure_and_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            now = datetime(2026, 9, 19, 23, 50, tzinfo=JST)
            with patch.object(scheduler, "discover_scheduled_races", return_value=[]) as discover:
                self.assertEqual(scheduler.discover_cached_races(self.config, now), [])
                self.assertEqual(scheduler.discover_cached_races(self.config, now + timedelta(minutes=5)), [])
                self.assertEqual(discover.call_count, 1)
                path = Path(tmp) / "automation" / "discovery_cache.json"
                before = path.read_bytes()
                discover.side_effect = scheduler.requests.ConnectionError("offline")
                for minute in (10, 20):
                    self.assertEqual(scheduler.discover_cached_races(self.config, now + timedelta(minutes=minute)), [])
                    self.assertEqual(path.read_bytes(), before)
                self.assertEqual(discover.call_count, 3)
                discover.side_effect = None
                scheduler.discover_cached_races(self.config, now + timedelta(minutes=30))
                self.assertEqual(discover.call_count, 4)
                self.assertNotEqual(path.read_bytes(), before)

    def test_discovery_failure_retains_stale_races_and_no_cache_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            now = datetime(2026, 9, 20, 13, 30, tzinfo=JST)
            race = {"date": "2026-09-20", "start_time": "15:40", "track": "中山", "race_number": 11}
            races = [{"race_id": "202606040711", "race": race,
                      "scheduled_at": scheduler.calculate_phase_times(race, self.config)}]
            with patch.object(scheduler, "discover_scheduled_races", side_effect=scheduler.requests.ConnectionError):
                with self.assertRaises(scheduler.requests.ConnectionError):
                    scheduler.discover_cached_races(self.config, now)
            with patch.object(scheduler, "discover_scheduled_races", return_value=races):
                scheduler.discover_cached_races(self.config, now)
            path = Path(tmp) / "automation" / "discovery_cache.json"
            before = path.read_bytes()
            with patch.object(scheduler, "discover_scheduled_races", side_effect=scheduler.requests.ConnectionError) as discover:
                for minutes in (60, 70):
                    self.assertEqual(scheduler.discover_cached_races(self.config, now + timedelta(minutes=minutes)), races)
                discover.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_phase_decisions_schedule_resume_and_missed_window(self):
        self.config["data_dir"] = "custom-data"
        race = {"date": "2026-09-20", "start_time": "15:40", "track": "中山", "race_number": 11}
        items = [{"race_id": "202606040711", "race": race}]
        times = scheduler.calculate_phase_times(race, self.config)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def decide(now):
                return {d["phase"]: d for d in scheduler.decide_phases(items, self.config, now, root)}
            for phase, scheduled in times.items():
                self.assertFalse(decide(scheduled - timedelta(seconds=1))[phase]["runnable"])
                if phase == "result":
                    self.assertEqual(decide(scheduled)[phase]["reason"], "no_prediction")
                else:
                    self.assertTrue(decide(scheduled)[phase]["runnable"])
            after = times["result"]
            for phase in ("general", "statistical"):
                self.assertEqual(decide(after)[phase]["reason"], "missed_execution_window")
                path = race_json_path(self.config, race["date"], race["track"], 11, root)
                suffix = ".statistical.json" if phase == "statistical" else ".json"
                atomic_write_json(outbox_chat_input_dir("prediction", root) / f"{path.stem}{suffix}", {})
                for current in (times["general"], after):
                    decision = decide(current)[phase]
                    self.assertTrue(decision["runnable"])
                    self.assertEqual(decision["mode"], "resume")
            # Decision reads must not create race or automation state files.
            self.assertFalse((root / "custom-data").exists())

    def test_completion_state_and_multiple_races_are_independent(self):
        self.config["data_dir"] = "custom-data"
        races = [{"race_id": str(number), "race": {
            "date": "2026-09-20", "start_time": "15:40", "track": "中山", "race_number": number,
        }} for number in (10, 11)]
        current = scheduler.calculate_phase_times(races[0]["race"], self.config)["general"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [race_json_path(self.config, "2026-09-20", "中山", n, root) for n in (10, 11)]
            def decide(now=current):
                return {(d["race_id"], d["phase"]): d for d in scheduler.decide_phases(races, self.config, now, root)}
            record_failure(paths[0], self.config, "10", "general", "failed", status="blocked", root=root)
            record_failure(paths[1], self.config, "11", "general", "failed",
                           next_retry_at=(current + timedelta(seconds=1)).isoformat(), root=root)
            self.assertEqual(decide()["10", "general"]["reason"], "blocked")
            self.assertTrue(decide()["10", "statistical"]["runnable"])
            self.assertEqual(decide()["11", "general"]["reason"], "retry_wait")
            self.assertTrue(decide(current + timedelta(seconds=1))["11", "general"]["runnable"])
            payload = {"meta": {"race_id": "10", "schema_version": 10}, "race": races[0]["race"],
                       "horses": [], "prediction": [{"id": "p1", "general": {"horses": [1]},
                                                                "statistical": {"horses": [1]}}],
                       "result": {"horses": [1]}, "simulation": [], "evaluation": []}
            atomic_write_json(paths[0], payload)
            before = {p: p.read_bytes() for p in root.rglob("*.json")}
            for phase in ("general", "statistical", "result"):
                decision = decide()["10", phase]
                self.assertFalse(decision["runnable"])
                self.assertEqual(decision["reason"], "completed")
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*.json")})
            payload["prediction"] = []
            atomic_write_json(paths[0], payload)
            self.assertEqual(decide()["10", "statistical"]["reason"], "result_exists")
            automation_state_path(paths[1], self.config, root).write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                decide()

    def test_executor_retry_resume_limits_and_success(self):
        self.config.update(data_dir="custom-data")
        self.config["automation"].update(retry_interval_minutes=2, max_attempts=2,
                                       result_retry_interval_minutes=3, result_max_attempts=2)
        item = {"race_id": "202606040711", "race": {"date": "2026-09-20", "track": "中山",
                "race_number": 11, "start_time": "15:40"}}
        now = scheduler.calculate_phase_times(item["race"], self.config)["general"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = race_json_path(self.config, item["race"]["date"], "中山", 11, root)
            def fail(config, date, *, phase, resume, race_id):
                suffix = ".statistical.json" if phase == "statistical" else ".json"
                atomic_write_json(outbox_chat_input_dir("prediction", root) / f"{path.stem}{suffix}", {})
                raise SystemExit("runtime unavailable")
            with patch.object(scheduler, "run_pre_flow", side_effect=fail) as pre, \
                 patch.object(scheduler, "run_post_flow") as post:
                scheduler.execute_phases([item, item], self.config, now, root)
                self.assertEqual(pre.call_count, 2)
                for phase in ("statistical", "general"):
                    pre.assert_any_call(self.config, item["race"]["date"], phase=phase, resume=False, race_id=item["race_id"])
                    record = load_automation_state(path, self.config, root)["phases"][phase]
                    self.assertEqual(record["attempts"], 1)
                    self.assertEqual(record["status"], "retry_wait")
                    self.assertEqual(record["next_retry_at"], (now + timedelta(minutes=2)).isoformat())
                scheduler.execute_phases([item], self.config, now + timedelta(seconds=1), root)
                self.assertEqual(pre.call_count, 2)
                scheduler.execute_phases([item], self.config, now + timedelta(minutes=2), root)
                self.assertEqual(pre.call_count, 4)
                self.assertTrue(all(c.kwargs["resume"] for c in pre.call_args_list[2:]))
                for record in load_automation_state(path, self.config, root)["phases"].values():
                    self.assertEqual((record["status"], record["attempts"], record["next_retry_at"]), ("blocked", 2, None))
                scheduler.execute_phases([item], self.config, now + timedelta(minutes=4), root)
                self.assertEqual(pre.call_count, 4)
                post.assert_not_called()
            atomic_write_json(path, {"meta": {"schema_version": 10, "race_id": item["race_id"]},
                                    "prediction": [{"id": "p1", "general": {"horses": [1]}, "statistical": {"horses": [1]}}]})
            result_time = scheduler.calculate_phase_times(item["race"], self.config)["result"]
            with patch.object(scheduler, "run_post_flow", return_value=[]) as post:
                scheduler.execute_phases([item], self.config, result_time - timedelta(seconds=1), root)
                post.assert_not_called()
                self.assertIsNone(load_automation_state(path, self.config, root))
                scheduler.execute_phases([item], self.config, result_time, root)
                post.assert_called_once_with(self.config, item["race"]["date"], "post", race_id=item["race_id"])
                record = load_automation_state(path, self.config, root)["phases"]["result"]
                self.assertEqual(record["next_retry_at"], (result_time + timedelta(minutes=3)).isoformat())
                scheduler.execute_phases([item], self.config, result_time + timedelta(minutes=3), root)
                self.assertEqual(load_automation_state(path, self.config, root)["phases"]["result"]["status"], "blocked")

    def test_executor_independent_success_and_missed_window(self):
        self.config.update(data_dir="custom-data")
        self.config["automation"].update(retry_interval_minutes=7, max_attempts=1,
                                       result_retry_interval_minutes=9, result_max_attempts=1)
        items = [{"race_id": str(n), "race": {"date": "2026-09-20", "track": "中山", "race_number": n,
                 "start_time": "15:40"}} for n in (10, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {item["race_id"]: race_json_path(self.config, "2026-09-20", "中山", item["race"]["race_number"], root) for item in items}
            now = scheduler.calculate_phase_times(items[0]["race"], self.config)["result"]
            for phase in ("statistical", "general"):
                suffix = ".statistical.json" if phase == "statistical" else ".json"
                atomic_write_json(outbox_chat_input_dir("prediction", root) / f"{paths['10'].stem}{suffix}", {})
                record_failure(paths['10'], self.config, "10", phase, "failed", next_retry_at=now.isoformat(), root=root)
            def pre(config, date, *, phase, resume, race_id):
                payload = load_race_json(paths[race_id]) or {"meta": {"schema_version": 10, "race_id": race_id}, "prediction": [{"id": "p1"}]}
                payload["prediction"][0][phase] = {"horses": [1]}
                atomic_write_json(paths[race_id], payload)
            def post(config, date, job, *, race_id):
                payload = load_race_json(paths[race_id])
                payload["result"] = {"horses": [1]}
                atomic_write_json(paths[race_id], payload)
            with patch.object(scheduler, "run_pre_flow", side_effect=pre) as pre_mock, \
                 patch.object(scheduler, "run_post_flow", side_effect=post) as post_mock:
                scheduler.execute_phases(items, self.config, now, root)
                self.assertEqual(pre_mock.call_count, 2)
                self.assertEqual(post_mock.call_count, 1)
                self.assertIsNone(load_automation_state(paths['10'], self.config, root))
                state = load_automation_state(paths['11'], self.config, root)
                self.assertEqual(set(state["phases"]), {"general", "statistical"})
                for record in state["phases"].values():
                    self.assertEqual((record["status"], record["attempts"]), ("blocked", 1))
                    self.assertEqual(record["last_error"], "missed_execution_window")
                scheduler.execute_phases(items, self.config, now, root)
                self.assertEqual(load_automation_state(paths['11'], self.config, root), state)
                self.assertEqual(pre_mock.call_count, 2)

    def test_cli_is_read_only_unless_execute_and_retry_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "argv", ["scheduler.py"]), \
             patch.object(scheduler, "load_config", return_value=self.config), \
             patch.object(scheduler, "discover_scheduled_races", return_value=[]), \
             patch.object(scheduler, "execute_phases") as execute, \
             patch.object(scheduler, "deploy_site") as deploy:
            self.config["data_dir"] = tmp
            scheduler.main()
            execute.assert_not_called()
            deploy.assert_not_called()
            with patch.object(sys, "argv", ["scheduler.py", "--execute"]):
                scheduler.main()
            execute.assert_called_once_with([], self.config)
            deploy.assert_called_once_with(self.config)
        for key in ("retry_interval_minutes", "max_attempts", "result_retry_interval_minutes", "result_max_attempts"):
            for invalid in (0, -1, True, "3", 1.5, None):
                config = copy.deepcopy(self.config)
                config["automation"].update(retry_interval_minutes=1, max_attempts=1,
                                            result_retry_interval_minutes=1, result_max_attempts=1)
                config["automation"][key] = invalid
                with self.assertRaises(ValueError):
                    scheduler.execute_phases([], config)

    def test_empty_pre_result_is_failure_with_configured_limit(self):
        self.config.update(data_dir="custom-data")
        self.config["automation"].update(retry_interval_minutes=7, max_attempts=1,
                                       result_retry_interval_minutes=9, result_max_attempts=1)
        item = {"race_id": "10", "race": {"date": "2026-09-20", "track": "中山",
                "race_number": 10, "start_time": "15:40"}}
        now = scheduler.calculate_phase_times(item["race"], self.config)["statistical"]
        with tempfile.TemporaryDirectory() as tmp, patch.object(scheduler, "run_pre_flow", return_value=[]):
            root = Path(tmp)
            scheduler.execute_phases([item], self.config, now, root)
            path = race_json_path(self.config, "2026-09-20", "中山", 10, root)
            state = load_automation_state(path, self.config, root)
            self.assertEqual(set(state["phases"]), {"statistical"})
            self.assertEqual(state["phases"]["statistical"]["status"], "blocked")
            self.assertEqual(state["phases"]["statistical"]["attempts"], 1)

    def test_cli_lock_scope_skip_and_exception_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            state_path = Path(tmp) / "automation" / "sentinel.json"
            atomic_write_json(state_path, {"unchanged": True})
            before = state_path.read_bytes()
            with patch.object(scheduler, "load_config", return_value=self.config), \
                 patch.object(scheduler, "discover_scheduled_races", return_value=[]) as discover, \
                 patch.object(scheduler, "execute_phases") as execute, \
                 patch.object(scheduler, "deploy_site") as deploy:
                with patch.object(sys, "argv", ["scheduler.py"]), \
                     patch.object(scheduler, "scheduler_lock", side_effect=AssertionError("display must not lock")):
                    scheduler.main()
                execute.assert_not_called()
                discover.reset_mock()
                with patch.object(sys, "argv", ["scheduler.py", "--execute"]):
                    with scheduler.scheduler_lock(self.config) as acquired:
                        self.assertTrue(acquired)
                        with patch("builtins.print") as output:
                            self.assertIsNone(scheduler.main())
                            output.assert_called_once()
                        discover.assert_not_called()
                        execute.assert_not_called()
                        deploy.assert_not_called()
                        self.assertEqual(state_path.read_bytes(), before)
                    def fail_inside_lock(*args):
                        with scheduler.scheduler_lock(self.config) as acquired:
                            self.assertFalse(acquired)
                        raise RuntimeError("execution failed")
                    execute.side_effect = fail_inside_lock
                    def discover_inside_lock(*args):
                        with scheduler.scheduler_lock(self.config) as acquired:
                            self.assertFalse(acquired)
                        return []
                    discover.side_effect = discover_inside_lock
                    with self.assertRaisesRegex(RuntimeError, "execution failed"):
                        scheduler.main()
                    execute.side_effect = None
                    scheduler.main()
                    self.assertEqual(execute.call_count, 2)
                    def fail_deploy(*args):
                        with scheduler.scheduler_lock(self.config) as acquired:
                            self.assertFalse(acquired)
                        raise RuntimeError("push failed")
                    deploy.side_effect = fail_deploy
                    with self.assertRaisesRegex(RuntimeError, "push failed"):
                        scheduler.main()
                    self.assertEqual(state_path.read_bytes(), before)
                with scheduler.scheduler_lock(self.config) as acquired:
                    self.assertTrue(acquired)

    def test_process_lock_contention_and_abnormal_exit_release(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from scheduler import scheduler_lock
with scheduler_lock({'data_dir': sys.argv[2]}) as acquired:
    print('entered' if acquired else 'skipped', flush=True)
    if acquired and sys.argv[3] == 'hold':
        sys.stdin.readline()
"""
        with tempfile.TemporaryDirectory() as tmp:
            args = [sys.executable, "-c", code, str(ROOT / "src"), tmp]
            with subprocess.Popen(args + ["hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as first:
                try:
                    self.assertEqual(first.stdout.readline().strip(), "entered")
                    second = subprocess.run(args + ["once"], capture_output=True, text=True, timeout=15)
                    self.assertEqual(second.returncode, 0, second.stderr)
                    self.assertEqual(second.stdout.strip(), "skipped")
                finally:
                    first.kill()
                    first.communicate(timeout=15)
            # The file remains, but killing its owner released the OS lock.
            self.assertTrue((Path(tmp) / "automation" / "scheduler.lock").exists())
            following = subprocess.run(args + ["once"], capture_output=True, text=True, timeout=15)
            self.assertEqual(following.returncode, 0, following.stderr)
            self.assertEqual(following.stdout.strip(), "entered")


if __name__ == "__main__":
    unittest.main()
