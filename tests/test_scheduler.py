from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import scheduler
from utils import JST, atomic_write_json, race_json_path, outbox_chat_input_dir
from automation_state import record_failure, automation_state_path


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "target_races": ["中山"], "odds_reference_minutes_before_start": 60,
            "automation": {"statistical_time": "18:00", "general_minutes_before_start": 45,
                           "result_minutes_after_start": 10},
        }

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


if __name__ == "__main__":
    unittest.main()
