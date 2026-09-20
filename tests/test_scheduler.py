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
from utils import JST, atomic_write_json, race_json_path, prediction_input_path, load_race_json
from automation_state import record_failure, automation_state_path, load_automation_state


class SchedulerTests(unittest.TestCase):
    def update_cancellations(self, races, config, now, root=None):
        tasks = scheduler.update_race_cancellations(races, config, now, root)
        scheduler.execute_phases(tasks, config, now, root)

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
            with patch.object(scheduler, "fetch_cancellation_notices", return_value=notice) as notices, patch.object(scheduler, "publish_post_results", return_value=[path]) as publish:
                # The race disappeared from discovery, but its saved record still receives the notice.
                self.update_cancellations([], config, datetime(2026, 2, 8, 9, tzinfo=JST), Path(tmp))
                saved = load_race_json(path)
                self.assertTrue(saved["race"]["cancelled"])
                self.assertEqual(saved["prediction"], payload["prediction"])
                self.assertEqual(saved["race"]["cancellation"]["replacement_date"], "2026-02-10")
                self.assertIsNone(load_automation_state(path, config))
                publish.assert_called_once()
                replacement = {"race_id": "202605010411", "race": {**race, "date": "2026-02-10"}}
                with patch.object(scheduler, "discover_scheduled_races", return_value=[replacement]):
                    scheduler.discover_cached_races(config, datetime(2026, 2, 9, 18, tzinfo=JST), Path(tmp))
                self.update_cancellations([replacement], config, datetime(2026, 2, 9, 18, tzinfo=JST), Path(tmp))
                self.assertEqual(notices.call_count, 1)
                self.assertNotIn("source_urls", notices.call_args.kwargs)
                new_path = race_json_path(config, "2026-02-10", "東京", 11)
                self.assertEqual(load_race_json(new_path)["race"]["rescheduled_from"], "2026-02-08")
                self.assertTrue(load_race_json(path)["race"]["cancelled"])
                input_path = prediction_input_path(config, new_path, root=Path(tmp))
                atomic_write_json(input_path, {"meta": {"race_id": replacement["race_id"], "kind": "prediction", "method": "general"}, "race": race, "horses": [{"horse_number": 1}]})
                decisions = scheduler.decide_phases(scheduler.create_phase_tasks([replacement], config, Path(tmp)), config, datetime(2026, 2, 10, 15, tzinfo=JST), Path(tmp))
                self.assertEqual(next(d for d in decisions if d["phase"] == "general")["reason"], "invalid_prediction_input")
                atomic_write_json(input_path, {"meta": {"race_id": replacement["race_id"], "kind": "prediction", "method": "general"}, "race": replacement["race"], "horses": [{"horse_number": 1}]})
                decisions = scheduler.decide_phases(scheduler.create_phase_tasks([replacement], config, Path(tmp)), config, datetime(2026, 2, 10, 15, tzinfo=JST), Path(tmp))
                self.assertEqual(next(d for d in decisions if d["phase"] == "general")["mode"], "resume")
            before = path.read_bytes()
            with patch.object(scheduler, "fetch_cancellation_notices", side_effect=scheduler.requests.ConnectionError("offline")):
                self.update_cancellations([], config, datetime(2026, 2, 10, 15, tzinfo=JST))
            self.assertEqual(path.read_bytes(), before)

    def setUp(self):
        self.enterContext(patch.object(scheduler, "fetch_cancellation_notices", return_value=[]))
        self.config = {
            "target_races": ["中山"], "odds_reference_minutes_before_start": 60,
            "automation": {"statistical_time": "18:00", "general_minutes_before_start": 45,
                           "result_minutes_after_start": 10,
                           "retry_interval_minutes": 10, "max_attempts": 3,
                           "result_retry_interval_minutes": 10, "result_max_attempts": 3},
        }

    def phase_fixture(self, directory, phase):
        config = copy.deepcopy(self.config)
        config["data_dir"] = directory
        race = {"date": "2026-09-20", "track": config["target_races"][0],
                "race_number": 11, "start_time": "15:40"}
        task = scheduler.PhaseTask.from_race("202606040711", race, phase, config)
        prediction = {"id": "p1"}
        if phase == "result":
            prediction["statistical"] = {"horses": [1]}
        payload = {"meta": {"schema_version": 10, "race_id": task.race_id},
                   "race": race, "prediction": [prediction]}
        atomic_write_json(task.path, payload)
        return config, task, payload

    def save_input(self, config, task, root=None):
        atomic_write_json(prediction_input_path(config, task.path, task.phase, root), {
            "meta": {"race_id": task.race_id, "kind": "prediction", "method": task.phase},
            "race": task.race, "horses": [{"horse_number": 1}]})

    def test_phase_lifecycle_retry_and_execution_mode(self):
        # name, failed attempts, limit, save artifacts before failure, exception
        scenarios = (
            ("success", 0, 3, False, None),
            ("runtime_retry", 1, 3, False, SystemExit),
            ("partial_retry", 1, 3, True, RuntimeError),
            ("partial_blocked", 1, 1, True, RuntimeError),
            ("retry_limit", 2, 2, False, RuntimeError),
            ("empty_result", 2, 2, False, None),
            ("missing_artifact", 2, 2, False, None),
        )
        for phase in ("statistical", "general", "result"):
            for name, failures, limit, partial, exception in scenarios:
                with self.subTest(phase=phase, scenario=name), tempfile.TemporaryDirectory() as tmp:
                    config, task, payload = self.phase_fixture(tmp, phase)
                    config["automation"].update(retry_interval_minutes=2, result_retry_interval_minutes=3,
                                                max_attempts=limit, result_max_attempts=limit)
                    interval = timedelta(minutes=3 if phase == "result" else 2)
                    current = task.scheduled_at
                    modes = []

                    def flow(*args, **kwargs):
                        self.assertEqual(args, (config, task.date, "post") if phase == "result" else (config, task.date))
                        self.assertEqual(kwargs["race_id"], task.race_id)
                        record = load_automation_state(task.path, config)["phases"][phase]
                        self.assertEqual((record["status"], record["attempts"]), ("in_progress", flow_mock.call_count - 1))
                        self.assertEqual(scheduler.decide_phase(task, config, current)["state"], "running")
                        if phase != "result":
                            self.assertEqual(kwargs["phase"], phase)
                            modes.append(kwargs["resume"])
                            self.save_input(config, task)
                        failing = flow_mock.call_count <= failures
                        if not failing or partial:
                            if phase == "result":
                                payload.update(result={"horses": [1]}, evaluation=[{
                                    "prediction_id": "p1", "statistical": {"metrics": {}}}],
                                    simulation=[{"prediction_id": "p1", "statistical": {
                                        "quinella": {"status": "ready", "post_status": "settled"}}}])
                            else:
                                payload["prediction"][0][phase] = {"horses": [1]}
                            atomic_write_json(task.path, payload)
                        if failing and exception:
                            raise exception("flow failed")
                        return [] if failing and name == "empty_result" else [task.path]

                    flow_name = "run_post_flow" if phase == "result" else "run_pre_flow"
                    with patch.object(scheduler, flow_name, side_effect=flow) as flow_mock:
                        before = scheduler.decide_phase(task, config, current - timedelta(seconds=1))
                        self.assertEqual((before["state"], before["mode"], before["runnable"]), ("scheduled", "normal", False))
                        scheduler.execute_phases([task], config, current - timedelta(seconds=1))
                        flow_mock.assert_not_called()
                        for attempt in range(1, min(failures + 1, limit) + 1):
                            scheduler.execute_phases([task, task], config, current)
                            self.assertEqual(flow_mock.call_count, attempt)
                            expected = "completed" if attempt > failures else "blocked" if attempt == limit else "retry_wait"
                            decision = scheduler.decide_phase(task, config, current)
                            self.assertEqual((decision["state"], decision["runnable"]), (expected, False))
                            self.assertEqual(decision["mode"], "normal" if phase == "result" else "resume")
                            state = load_automation_state(task.path, config)
                            if expected == "completed":
                                self.assertIsNone(state)
                            else:
                                record = state["phases"][phase]
                                self.assertEqual((record["status"], record["attempts"]), (expected, attempt))
                                self.assertEqual(record["next_retry_at"], None if expected == "blocked" else (current + interval).isoformat())
                            snapshot = task.path.read_bytes()
                            scheduler.execute_phases([task], config, current + interval - timedelta(seconds=1))
                            self.assertEqual(flow_mock.call_count, attempt)
                            self.assertEqual(task.path.read_bytes(), snapshot)
                            current += interval
                        scheduler.execute_phases([task], config, current + interval)
                        self.assertEqual(flow_mock.call_count, min(failures + 1, limit))
                    if phase != "result":
                        self.assertEqual(modes, [False] + [True] * (len(modes) - 1))

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
                        self.update_cancellations(items, self.config, now + timedelta(minutes=minutes))
                    self.assertEqual(fetch.call_count, 1)
                    self.update_cancellations(items, self.config, now + timedelta(minutes=60))
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
                self.update_cancellations([item], self.config, datetime(2026, 9, 20, 12, tzinfo=JST))
                race["date"] = "2026-09-20"
                path = race_json_path(self.config, race["date"], race["track"], 11)
                payload = ensure_race_payload(None, item["race_id"])
                payload["race"] = race
                payload["result"] = {"horses": [1]}
                atomic_write_json(path, payload)
                self.update_cancellations([item], self.config, datetime(2026, 9, 20, 12, tzinfo=JST))
                payload["result"] = None
                payload["race"]["cancelled"] = True
                payload["race"]["cancellation"] = {"source_url": "https://info.netkeiba.com/?id=1"}
                atomic_write_json(path, payload)
                self.update_cancellations([item], self.config, datetime(2026, 9, 20, 13, tzinfo=JST))
                fetch.assert_not_called()

    def test_evening_discovery_refresh_once_and_failure_backoff(self):
        import json
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                self.config["data_dir"] = tmp
                now = datetime(2026, 9, 20, 17, 50, tzinfo=JST)
                with patch.object(scheduler, "discover_scheduled_races", return_value=[]) as discover:
                    scheduler.discover_cached_races(self.config, now)
                    if failure:
                        discover.side_effect = scheduler.requests.ConnectionError("offline")
                    for minutes in range(10, 70, 10):
                        scheduler.discover_cached_races(self.config, now + timedelta(minutes=minutes))
                    self.assertEqual(discover.call_count, 2)
                    cache = json.loads((Path(tmp) / "automation/discovery_cache.json").read_text())
                    if failure:
                        self.assertEqual(cache["discovered_at"], now.isoformat())
                    discover.side_effect = None
                    scheduler.discover_cached_races(self.config, now + timedelta(minutes=70))
                    self.assertEqual(discover.call_count, 3 if failure else 2)
                    scheduler.discover_cached_races(self.config, now + timedelta(hours=5))
                    self.assertEqual(discover.call_count, 3 if failure else 2)

    def test_cancellation_reads_only_today_and_history_only_on_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            now = datetime(2026, 9, 20, 12, tzinfo=JST)
            with patch.object(scheduler, "list_race_files", wraps=scheduler.list_race_files) as files, \
                 patch.object(scheduler, "discover_scheduled_races", return_value=[]):
                scheduler.discover_cached_races(self.config, now)
                self.assertEqual([c.args[1] for c in files.call_args_list], [None])
                files.reset_mock()
                for minutes in range(0, 70, 10):
                    current = now + timedelta(minutes=minutes)
                    scheduler.discover_cached_races(self.config, current)
                    self.update_cancellations([], self.config, current)
                self.assertEqual([c.args[1] for c in files.call_args_list], ["2026-09-20"] * 7)

    def test_result_waits_for_all_ready_quinella_and_retries_until_settled(self):
        self.config["automation"].update(retry_interval_minutes=7, max_attempts=3,
                                        result_retry_interval_minutes=11, result_max_attempts=4)
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            item = {"race_id": "202606040711", "race": {
                "date": "2026-09-20", "track": self.config["target_races"][0],
                "race_number": 11, "start_time": "15:40"}}
            path = race_json_path(self.config, item["race"]["date"], item["race"]["track"], 11)
            now = scheduler.calculate_phase_times(item["race"], self.config)["result"]
            payload = {"meta": {"schema_version": 10, "race_id": item["race_id"]},
                       "race": item["race"], "result": {"horses": [1]},
                       "prediction": [{"id": "p1", "general": {"horses": [1]}, "statistical": {"horses": [1]}}],
                       "simulation": [{"prediction_id": "p1", "general": {"quinella": {"status": "ready", "post_status": "settled"}},
                                       "statistical": {"quinella": {"status": "ready", "post_status": "awaiting_payouts"}}}]}
            self.assertTrue(scheduler.result_phase_complete({"result": {"horses": [1]}}))
            self.assertFalse(scheduler.result_phase_complete({}))
            payload["evaluation"] = [{"prediction_id": "p1", "general": {"metrics": {}}, "statistical": {"metrics": {}}}]
            atomic_write_json(path, payload)
            with patch.object(scheduler, "run_post_flow", return_value=[]) as post:
                scheduler.execute_phases(scheduler.create_phase_tasks([item], self.config), self.config, now)
                record = load_automation_state(path, self.config)["phases"]["result"]
                self.assertEqual(record["status"], "retry_wait")
                self.assertEqual(record["next_retry_at"], (now + timedelta(minutes=11)).isoformat())
                def settle(*args, **kwargs):
                    payload["simulation"][0]["statistical"]["quinella"]["post_status"] = "settled"
                    atomic_write_json(path, payload)
                    return [path]
                post.side_effect = settle
                scheduler.execute_phases(scheduler.create_phase_tasks([item], self.config), self.config, now + timedelta(minutes=11))
                self.assertEqual(post.call_count, 2)
                self.assertIsNone(load_automation_state(path, self.config))
                decision = scheduler.decide_phases(scheduler.create_phase_tasks([item], self.config), self.config, now)[-1]
                self.assertEqual(decision["state"], "completed")
            payload["simulation"][0]["statistical"]["quinella"] = {"status": "unavailable"}
            self.assertTrue(scheduler.result_phase_complete(payload))

    def test_interrupted_phase_restarts_after_partial_artifact_save(self):
        for phase in ("general", "statistical", "result"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                config, task, payload = self.phase_fixture(tmp, phase)
                path = task.path
                if phase != "result":
                    self.save_input(config, task)
                now = task.scheduled_at
                def interrupt(*args, **kwargs):
                    if phase == "result":
                        payload["result"] = {"horses": [1]}
                    else:
                        payload["prediction"][0][phase] = {"horses": [1]}
                    atomic_write_json(path, payload)
                    raise KeyboardInterrupt("process interrupted before publication")
                flow_name = "run_post_flow" if phase == "result" else "run_pre_flow"
                with patch.object(scheduler, flow_name, side_effect=interrupt) as flow:
                    with self.assertRaises(KeyboardInterrupt):
                        scheduler.execute_phases([task], config, now)
                    state = load_automation_state(path, config)["phases"][phase]
                    self.assertEqual((state["status"], state["attempts"]), ("in_progress", 0))
                    later = scheduler.calculate_phase_times(task.race, config)["result"] + timedelta(minutes=10)
                    decision = scheduler.decide_phases([task], config, later)[0]
                    self.assertEqual((decision["state"], decision["runnable"]), ("running", True))
                    def complete(*args, **kwargs):
                        if phase == "result":
                            payload["evaluation"] = [{"prediction_id": "p1", "statistical": {"metrics": {}}}]
                            atomic_write_json(path, payload)
                        return [path]
                    flow.side_effect = complete
                    scheduler.execute_phases([task], config, later)
                    self.assertEqual(flow.call_count, 2)
                    if phase != "result":
                        self.assertTrue(flow.call_args.kwargs["resume"])
                    self.assertIsNone(load_automation_state(path, config))
                    self.assertEqual(scheduler.decide_phases([task], config, later)[0]["state"], "completed")

    def test_result_completion_requires_each_prediction_evaluation_and_available_posts(self):
        payload = {"result": {"horses": [1]},
                   "prediction": [{"id": "p1", "statistical": {"horses": [1]}}],
                   "simulation": [], "evaluation": []}
        self.assertFalse(scheduler.result_phase_complete(payload))
        with tempfile.TemporaryDirectory() as tmp:
            config = {**self.config, "data_dir": tmp}
            race = {"date": "2026-09-20", "track": config["target_races"][0], "race_number": 11, "start_time": "15:40"}
            path = race_json_path(config, race["date"], race["track"], 11)
            atomic_write_json(path, {**payload, "meta": {"schema_version": 10, "race_id": "test"}, "race": race})
            decisions = scheduler.decide_phases(scheduler.create_phase_tasks([{"race_id": "test", "race": race}], config), config,
                                                scheduler.calculate_phase_times(race, config)["result"])
            self.assertTrue(next(d for d in decisions if d["phase"] == "result")["runnable"])
        payload["evaluation"] = [{"prediction_id": "p2", "statistical": {"metrics": {}}}]
        self.assertFalse(scheduler.result_phase_complete(payload))
        payload["evaluation"][0]["prediction_id"] = "p1"
        self.assertTrue(scheduler.result_phase_complete(payload))
        payload["prediction"].append({"id": "p2", "general": {"horses": [1]}})
        self.assertFalse(scheduler.result_phase_complete(payload))
        payload["evaluation"].append({"prediction_id": "p2", "general": {"metrics": {}}})
        payload["simulation"] = [{"prediction_id": "p1", "statistical": {
            "win": {"value": {"pre": {}, "post": None}, "dutching": {"pre": None, "post": None}}}}]
        self.assertFalse(scheduler.result_phase_complete(payload))
        payload["simulation"][0]["statistical"]["win"]["value"]["post"] = {}
        self.assertTrue(scheduler.result_phase_complete(payload))

    def test_previous_day_pending_states_run_locally_without_extra_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = copy.deepcopy(self.config)
            config["data_dir"] = tmp
            config["automation"].update(retry_interval_minutes=10, max_attempts=3,
                                        result_retry_interval_minutes=10, result_max_attempts=3)
            now = datetime(2026, 9, 21, 12, tzinfo=JST)
            paths = {}
            for number, status in ((9, "retry_wait"), (10, "blocked"), (11, None)):
                race = {"date": "2026-09-20", "track": config["target_races"][0], "race_number": number,
                        "start_time": "15:40", "race_name": "test"}
                path = race_json_path(config, race["date"], race["track"], number)
                paths[str(number)] = path
                atomic_write_json(path, {"meta": {"schema_version": 10, "race_id": str(number)}, "race": race,
                                        "prediction": [{"id": "p1", "statistical": {"horses": [1]}}],
                                        "result": {"horses": [1]}, "evaluation": [{"prediction_id": "p1", "statistical": {"metrics": {}}}]})
                if status:
                    record_failure(path, config, str(number), "result", "publish failed", status=status,
                                   next_retry_at=(now - timedelta(minutes=10)).isoformat())
            atomic_write_json(Path(tmp) / "automation/discovery_cache.json", {
                "discovered_at": now.isoformat(), "dates": ["2026-09-21", "2026-09-22"],
                "target_races": config["target_races"], "races": []})
            candidates = scheduler.add_pending_tasks([], config, now)
            self.assertEqual([task.key for task in candidates], [("2026-09-20", "9", "result")])
            def finish(config, date, job, *, race_id):
                self.assertEqual(date, "2026-09-20")
                return [paths[race_id]]
            with patch.object(sys, "argv", ["scheduler.py", "--execute"]), \
                 patch.object(scheduler, "load_config", return_value=config), \
                 patch.object(scheduler, "now_jst", return_value=now), \
                 patch.object(scheduler, "discover_scheduled_races") as discover, \
                 patch.object(scheduler, "fetch_cancellation_notices") as notices, \
                 patch.object(scheduler, "run_pre_flow") as pre, \
                 patch.object(scheduler, "run_post_flow", side_effect=finish) as post, \
                 patch.object(scheduler, "deploy_site"), patch("builtins.print"):
                scheduler.main()
                scheduler.main()
                discover.assert_not_called()
                notices.assert_not_called()
                pre.assert_not_called()
                post.assert_called_once()
            self.assertIsNone(load_automation_state(paths["9"], config))
            self.assertEqual(load_automation_state(paths["10"], config)["phases"]["result"]["status"], "blocked")

    def test_same_race_id_on_different_dates_has_independent_phase_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = copy.deepcopy(self.config)
            config["data_dir"] = tmp
            config["automation"].update(retry_interval_minutes=10, max_attempts=3,
                                        result_retry_interval_minutes=10, result_max_attempts=3)
            now = datetime(2026, 9, 21, 18, tzinfo=JST)
            items, paths = [], []
            for date, cancelled in (("2026-09-20", True), ("2026-09-21", False)):
                race = {"date": date, "track": config["target_races"][0], "race_number": 11,
                        "start_time": "15:40", "cancelled": cancelled}
                item = {"race_id": "202606040711", "race": race}
                path = race_json_path(config, date, race["track"], 11)
                atomic_write_json(path, {
                    "meta": {"schema_version": 10, "race_id": item["race_id"]}, "race": race,
                    "prediction": [{"id": "p1", "statistical": {"horses": [1]}}],
                    "result": None if cancelled else {"horses": [1]},
                    "evaluation": [{"prediction_id": "p1", "statistical": {"metrics": {}}}],
                })
                record_failure(path, config, item["race_id"], "result", "publish failed",
                               next_retry_at=now.isoformat())
                items.append(item)
                paths.append(path)
            with patch.object(scheduler, "publish_post_results", return_value=[paths[0]]) as publish, \
                 patch.object(scheduler, "run_post_flow", return_value=[paths[1]]) as post:
                scheduler.execute_phases([task for task in scheduler.create_phase_tasks(items + items, config) if task.phase == "result"], config, now)
                publish.assert_called_once_with([paths[0]], config, "cancellation", None)
                post.assert_called_once_with(config, "2026-09-21", "post", race_id=items[1]["race_id"])
                self.assertTrue(all(load_automation_state(path, config) is None for path in paths))
                scheduler.execute_phases([task for task in scheduler.create_phase_tasks(items, config) if task.phase == "result"], config, now)
                self.assertEqual((publish.call_count, post.call_count), (1, 1))

    def test_cancelled_interrupted_publication_is_retried_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = copy.deepcopy(self.config)
            config["data_dir"] = tmp
            config["automation"].update(retry_interval_minutes=10, max_attempts=3,
                                        result_retry_interval_minutes=10, result_max_attempts=3)
            now = datetime(2026, 9, 20, 9, tzinfo=JST)
            race = {"date": "2026-09-20", "track": config["target_races"][0], "race_number": 11, "start_time": "15:40"}
            path = race_json_path(config, race["date"], race["track"], 11)
            atomic_write_json(path, {"meta": {"schema_version": 10, "race_id": "202606040711"}, "race": race})
            with patch.object(scheduler, "fetch_cancellation_notices", return_value=[("url", "html")]), \
                 patch.object(scheduler, "parse_cancellation_notice", return_value={"source_url": "url"}), \
                 patch.object(scheduler, "publish_post_results", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.update_cancellations([], config, now)
            self.assertTrue(load_race_json(path)["race"]["cancelled"])
            self.assertEqual(load_automation_state(path, config)["phases"]["result"]["status"], "in_progress")
            later = now + timedelta(days=1)
            candidates = scheduler.add_pending_tasks([], config, later)
            with patch.object(scheduler, "publish_post_results", return_value=[path]) as publish, \
                 patch.object(scheduler, "fetch_cancellation_notices") as notices:
                scheduler.execute_phases(candidates, config, later)
                publish.assert_called_once()
                notices.assert_not_called()
            self.assertEqual(scheduler.add_pending_tasks([], config, later), [])

    def test_replacement_failure_does_not_commit_discovery_and_backs_off(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            config = {**self.config, "data_dir": tmp}
            noon = datetime(2026, 9, 20, 12, tzinfo=JST)
            with patch.object(scheduler, "discover_scheduled_races", return_value=[]):
                scheduler.discover_cached_races(config, noon)
            path = Path(tmp) / "automation/discovery_cache.json"
            evening = noon.replace(hour=18)
            with patch.object(scheduler, "discover_scheduled_races", return_value=[]) as discover, \
                 patch.object(scheduler, "restore_replacement_races", side_effect=OSError("disk unavailable")) as restore:
                self.assertEqual(scheduler.discover_cached_races(config, evening), [])
                self.assertEqual(json.loads(path.read_text())["discovered_at"], noon.isoformat())
                scheduler.discover_cached_races(config, evening + timedelta(minutes=10))
                self.assertEqual(discover.call_count, 1)
                restore.side_effect = None
                scheduler.discover_cached_races(config, evening + timedelta(hours=1))
                self.assertEqual(discover.call_count, 2)
                self.assertEqual(json.loads(path.read_text())["discovered_at"], (evening + timedelta(hours=1)).isoformat())

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
                decisions = scheduler.decide_phases(scheduler.create_phase_tasks(cached, self.config), self.config, later)
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
                self.assertNotEqual(path.read_bytes(), before)
                self.assertEqual(discover.call_count, 2)
                discover.side_effect = None
                scheduler.discover_cached_races(self.config, now + timedelta(minutes=70))
                self.assertEqual(discover.call_count, 3)
                self.assertNotEqual(path.read_bytes(), before)

    def test_discovery_failure_without_cache_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.config["data_dir"] = tmp
            with patch.object(scheduler, "discover_scheduled_races", side_effect=scheduler.requests.ConnectionError):
                with self.assertRaises(scheduler.requests.ConnectionError):
                    scheduler.discover_cached_races(self.config, datetime(2026, 9, 20, 13, 30, tzinfo=JST))

    def test_prediction_inputs_are_date_and_method_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {**self.config, "data_dir": tmp}
            paths = []
            for date in ("2026-09-20", "2026-09-27"):
                race_path = race_json_path(config, date, config["target_races"][0], 11)
                for method in ("general", "statistical"):
                    path = prediction_input_path(config, race_path, method)
                    self.assertEqual(path.parent, Path(tmp) / "prediction_inputs" / date)
                    atomic_write_json(path, {"date": date, "method": method})
                    paths.append(path)
            self.assertEqual(len(set(paths)), 4)
            self.assertTrue(all(path.is_file() for path in paths))

    def test_scheduler_rejects_wrong_saved_input_identity_without_overwriting(self):
        for method in ("general", "statistical"):
            for field in ("race_id", "date", "track", "race_number", "method", "invalid_json"):
                with self.subTest(method=method, field=field), tempfile.TemporaryDirectory() as tmp:
                    config = {**self.config, "data_dir": tmp}
                    race = {"date": "2026-09-20", "track": config["target_races"][0], "race_number": 11, "start_time": "15:40"}
                    item = {"race_id": "202606040711", "race": race}
                    path = prediction_input_path(config, race_json_path(config, race["date"], race["track"], 11), method)
                    snapshot = {"meta": {"race_id": item["race_id"], "kind": "prediction", "method": method}, "race": dict(race), "horses": [{"horse_number": 1}]}
                    if field in ("race_id", "method"):
                        snapshot["meta"][field] = "old-race-or-other-method"
                    elif field != "invalid_json":
                        snapshot["race"][field] = "wrong-identity"
                    atomic_write_json(path, snapshot)
                    if field == "invalid_json":
                        path.write_text("{", encoding="utf-8")
                    before = path.read_bytes()
                    decisions = scheduler.decide_phases(scheduler.create_phase_tasks([item], config), config, datetime(2026, 9, 20, 15, tzinfo=JST))
                    decision = next(d for d in decisions if d["phase"] == method)
                    self.assertFalse(decision["runnable"])
                    self.assertEqual(decision["reason"], "invalid_prediction_input")
                    self.assertEqual(path.read_bytes(), before)

    def test_pre_input_cannot_generate_new_prediction_after_start(self):
        self.config["data_dir"] = "custom-data"
        race = {"date": "2026-09-20", "start_time": "15:40", "track": "中山", "race_number": 11}
        items = [{"race_id": "202606040711", "race": race}]
        times = scheduler.calculate_phase_times(race, self.config)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = scheduler.create_phase_tasks(items, self.config, root)
            def decide(now):
                return {d["phase"]: d for d in scheduler.decide_phases(tasks, self.config, now, root)}
            self.assertEqual(decide(times["result"])["result"]["reason"], "no_prediction")
            after = times["result"]
            for phase in ("general", "statistical"):
                self.assertEqual(decide(after)[phase]["reason"], "missed_execution_window")
                self.save_input(self.config, next(t for t in tasks if t.phase == phase), root)
                for current in (times["general"], after):
                    decision = decide(current)[phase]
                    self.assertEqual(decision["runnable"], current < after)
                    self.assertEqual(decision["mode"], "resume")
            # Decision reads must not create race or automation state files.
            self.assertFalse((root / "custom-data/races").exists())
            self.assertFalse((root / "custom-data/automation").exists())

    def test_completion_state_and_multiple_races_are_independent(self):
        self.config["data_dir"] = "custom-data"
        races = [{"race_id": str(number), "race": {
            "date": "2026-09-20", "start_time": "15:40", "track": "中山", "race_number": number,
        }} for number in (10, 11)]
        current = scheduler.calculate_phase_times(races[0]["race"], self.config)["general"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [race_json_path(self.config, "2026-09-20", "中山", n, root) for n in (10, 11)]
            tasks = scheduler.create_phase_tasks(races, self.config, root)
            self.assertEqual([task.key for task in tasks], [
                ("2026-09-20", str(number), phase) for number in (10, 11)
                for phase in ("statistical", "general", "result")])
            def decide(now=current):
                return {(d["race_id"], d["phase"]): d for d in scheduler.decide_phases(tasks, self.config, now, root)}
            record_failure(paths[0], self.config, "10", "general", "failed", status="blocked", root=root)
            record_failure(paths[1], self.config, "11", "general", "failed",
                           next_retry_at=(current + timedelta(seconds=1)).isoformat(), root=root)
            self.assertEqual(decide()["10", "general"]["state"], "blocked")
            self.assertTrue(decide()["10", "statistical"]["runnable"])
            self.assertEqual(decide()["11", "general"]["state"], "retry_wait")
            self.assertTrue(decide(current + timedelta(seconds=1))["11", "general"]["runnable"])
            payload = {"meta": {"race_id": "10", "schema_version": 10}, "race": races[0]["race"],
                       "horses": [], "prediction": [{"id": "p1", "general": {"horses": [1]},
                                                                "statistical": {"horses": [1]}}],
                       "result": {"horses": [1]}, "simulation": [], "evaluation": [{"prediction_id": "p1", "general": {"metrics": {}}, "statistical": {"metrics": {}}}]}
            atomic_write_json(paths[0], payload)
            before = {p: p.read_bytes() for p in root.rglob("*.json")}
            for phase in ("general", "statistical", "result"):
                decision = decide()["10", phase]
                self.assertFalse(decision["runnable"])
                self.assertEqual(decision["state"], "blocked" if phase == "general" else "completed")
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*.json")})
            payload["prediction"] = []
            atomic_write_json(paths[0], payload)
            self.assertEqual(decide()["10", "statistical"]["reason"], "result_exists")
            automation_state_path(paths[1], self.config, root).write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                decide()

    def test_cancelled_publish_failure_retries_without_discovery_or_notice_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = copy.deepcopy(self.config)
            config["data_dir"] = tmp
            config["automation"].update(retry_interval_minutes=10, max_attempts=3,
                                        result_retry_interval_minutes=10, result_max_attempts=3)
            race = {"date": "2026-09-20", "track": config["target_races"][0], "race_number": 11, "start_time": "15:40"}
            path = race_json_path(config, race["date"], race["track"], 11)
            atomic_write_json(path, {"meta": {"schema_version": 10, "race_id": "202606040711"}, "race": race})
            now = datetime(2026, 9, 20, 9, tzinfo=JST)
            with patch.object(scheduler, "fetch_cancellation_notices", return_value=[("url", "html")]) as fetch, \
                 patch.object(scheduler, "parse_cancellation_notice", return_value={"source_url": "url"}), \
                 patch.object(scheduler, "publish_post_results", side_effect=RuntimeError("publish failed")) as publish:
                self.update_cancellations([], config, now)
                self.assertTrue(load_race_json(path)["race"]["cancelled"])
                self.assertEqual(load_automation_state(path, config)["phases"]["result"]["status"], "retry_wait")
                self.update_cancellations([], config, now + timedelta(minutes=1))
                self.assertEqual(publish.call_count, 1)
                publish.side_effect = None
                publish.return_value = [path]
                self.update_cancellations([], config, now + timedelta(minutes=10))
                self.assertEqual(publish.call_count, 2)
                self.assertIsNone(load_automation_state(path, config))
                self.update_cancellations([], config, now + timedelta(minutes=20))
                self.assertEqual(publish.call_count, 2)
                self.assertEqual(fetch.call_count, 1)

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
                atomic_write_json(prediction_input_path(self.config, paths["10"], phase, root), {"meta": {"race_id": "10", "kind": "prediction", "method": phase}, "race": items[0]["race"], "horses": [{"horse_number": 1}]})
                record_failure(paths['10'], self.config, "10", phase, "failed", next_retry_at=now.isoformat(), root=root)
            atomic_write_json(paths['10'], {"meta": {"schema_version": 10, "race_id": "10"},
                                          "prediction": [{"id": "p1", "general": {"horses": [1]}, "statistical": {"horses": [1]}}]})
            def pre(config, date, *, phase, resume, race_id):
                payload = load_race_json(paths[race_id]) or {"meta": {"schema_version": 10, "race_id": race_id}, "prediction": [{"id": "p1"}]}
                payload["prediction"][0][phase] = {"horses": [1]}
                atomic_write_json(paths[race_id], payload)
                return [paths[race_id]]
            def post(config, date, job, *, race_id):
                payload = load_race_json(paths[race_id])
                payload["result"] = {"horses": [1]}
                payload["evaluation"] = [{"prediction_id": "p1", "general": {"metrics": {}}, "statistical": {"metrics": {}}}]
                atomic_write_json(paths[race_id], payload)
                return [paths[race_id]]
            with patch.object(scheduler, "run_pre_flow", side_effect=pre) as pre_mock, \
                 patch.object(scheduler, "run_post_flow", side_effect=post) as post_mock:
                scheduler.execute_phases(scheduler.create_phase_tasks(items, self.config, root), self.config, now, root)
                self.assertEqual(pre_mock.call_count, 2)
                self.assertEqual(post_mock.call_count, 1)
                self.assertIsNone(load_automation_state(paths['10'], self.config, root))
                state = load_automation_state(paths['11'], self.config, root)
                self.assertEqual(set(state["phases"]), {"general", "statistical"})
                for record in state["phases"].values():
                    self.assertEqual((record["status"], record["attempts"]), ("blocked", 1))
                    self.assertEqual(record["last_error"], "missed_execution_window")
                scheduler.execute_phases(scheduler.create_phase_tasks(items, self.config, root), self.config, now, root)
                self.assertEqual(load_automation_state(paths['11'], self.config, root), state)
                self.assertEqual(pre_mock.call_count, 2)

    def test_retry_settings_require_positive_integers(self):
        for key in ("retry_interval_minutes", "max_attempts", "result_retry_interval_minutes", "result_max_attempts"):
            for invalid in (0, -1, True, "3", 1.5, None):
                config = copy.deepcopy(self.config)
                config["automation"].update(retry_interval_minutes=1, max_attempts=1,
                                            result_retry_interval_minutes=1, result_max_attempts=1)
                config["automation"][key] = invalid
                with self.assertRaises(ValueError):
                    scheduler.execute_phases([], config)

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
                deploy.assert_not_called()
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
