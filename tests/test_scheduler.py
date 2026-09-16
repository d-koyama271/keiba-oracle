from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import scheduler
from utils import JST


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


if __name__ == "__main__":
    unittest.main()
