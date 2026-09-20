from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_state import automation_state_path, clear_phase_state, load_automation_state, record_failure, record_phase_started


class AutomationStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = {"data_dir": "custom-data"}
        self.race = self.root / "custom-data/races/2026-09-20/hanshin_11r.json"
        self.race.parent.mkdir(parents=True)
        self.race.write_text('{"meta":{"race_id":"test-race"}}', encoding="utf-8")
        self.before = self.race.read_bytes()

    def record(self, phase="general", **kwargs):
        return record_failure(self.race, self.config, "test-race", phase, "runtime failed",
                              root=self.root, **kwargs)

    def load(self):
        return load_automation_state(self.race, self.config, self.root)

    def test_missing_state_is_no_failure_and_clear_is_noop(self):
        self.assertIsNone(self.load())
        clear_phase_state(self.race, self.config, "general", self.root)
        self.assertFalse((self.root / "custom-data/automation").exists())

    def test_retry_roundtrip_increments_attempts_and_preserves_race(self):
        first = self.record(next_retry_at="2026-09-20T15:05:00+09:00")
        self.assertEqual(self.load(), first)
        self.assertEqual(first["phases"]["general"]["attempts"], 1)
        self.assertEqual(first["phases"]["general"]["status"], "retry_wait")
        self.assertEqual(first["phases"]["general"]["last_error"], "runtime failed")
        self.assertTrue(first["phases"]["general"]["updated_at"])
        second = self.record(next_retry_at="2026-09-20T15:10:00+09:00")
        self.assertEqual(second["phases"]["general"]["attempts"], 2)
        self.assertEqual(self.load(), second)
        self.assertEqual(self.race.read_bytes(), self.before)
        self.assertEqual(automation_state_path(self.race, self.config, self.root),
                         self.root / "custom-data/automation/2026-09-20/hanshin_11r.json")

    def test_started_state_preserves_failure_count_and_survives_reload(self):
        record_phase_started(self.race, self.config, "test-race", "general", self.root)
        self.assertEqual(self.load()["phases"]["general"]["status"], "in_progress")
        self.assertEqual(self.load()["phases"]["general"]["attempts"], 0)
        self.record(next_retry_at="2026-09-20T15:05:00+09:00")
        self.assertEqual(self.load()["phases"]["general"]["attempts"], 1)
        record_phase_started(self.race, self.config, "test-race", "general", self.root)
        self.assertEqual(self.load()["phases"]["general"]["attempts"], 1)
        self.record(status="blocked")
        self.assertEqual(self.load()["phases"]["general"]["attempts"], 2)
        self.assertEqual(self.race.read_bytes(), self.before)

    def test_blocked_and_clear_only_target_phase(self):
        self.record(status="blocked", next_retry_at="2026-09-20T15:05:00+09:00")
        self.record("statistical", status="blocked")
        self.record("result", next_retry_at="2026-09-20T16:00:00+09:00")
        before = self.load()
        self.assertEqual(before["phases"]["general"]["status"], "blocked")
        self.assertIsNone(before["phases"]["general"]["next_retry_at"])
        clear_phase_state(self.race, self.config, "general", self.root)
        self.assertEqual(self.load()["phases"], {k: v for k, v in before["phases"].items() if k != "general"})
        clear_phase_state(self.race, self.config, "statistical", self.root)
        clear_phase_state(self.race, self.config, "result", self.root)
        self.assertIsNone(self.load())
        self.assertFalse(automation_state_path(self.race, self.config, self.root).exists())
        self.assertEqual(self.race.read_bytes(), self.before)

    def test_invalid_state_cannot_be_loaded_overwritten_or_cleared(self):
        valid = self.record(status="blocked")
        invalid = ["{broken", "null", "[]", '{}']
        for key, value in (("status", "complete"), ("attempts", True), ("attempts", 0),
                           ("next_retry_at", "2026-09-20T16:00:00+09:00"),
                           ("last_error", None), ("updated_at", "invalid")):
            candidate = copy.deepcopy(valid)
            candidate["phases"]["general"][key] = value
            invalid.append(json.dumps(candidate))
        invalid += [json.dumps({"race_id": "test-race", "phases": {"unknown": {}}}),
                    json.dumps({"race_id": "test-race", "phases": []})]
        path = automation_state_path(self.race, self.config, self.root)
        for raw in invalid:
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                before = path.read_bytes()
                for operation in (self.load, lambda: self.record(status="blocked"),
                                  lambda: clear_phase_state(self.race, self.config, "general", self.root)):
                    with self.assertRaises(ValueError):
                        operation()
                    self.assertEqual(path.read_bytes(), before)

    def test_invalid_updates_preserve_existing_state(self):
        self.record(status="blocked")
        path = automation_state_path(self.race, self.config, self.root)
        before = path.read_bytes()
        for kwargs in ({}, {"next_retry_at": "invalid"}, {"status": "success"},
                       {"phase": "unknown", "status": "blocked"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.record(**kwargs)
            self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "race_id mismatch"):
            record_failure(self.race, self.config, "other-race", "general", "error", status="blocked", root=self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_path_uses_date_and_stem_not_race_id(self):
        self.record(status="blocked")
        other = self.root / "custom-data/races/2026-09-21/hanshin_11r.json"
        self.assertIsNone(load_automation_state(other, self.config, self.root))
        record_failure(other, self.config, "test-race", "result", "error", status="blocked", root=self.root)
        self.assertEqual(set(self.load()["phases"]), {"general"})
        self.assertEqual(set(load_automation_state(other, self.config, self.root)["phases"]), {"result"})
        absolute = {"data_dir": str(self.root / "custom-data")}
        self.assertEqual(automation_state_path(self.race, absolute), automation_state_path(self.race, self.config, self.root))
        with self.assertRaises(ValueError):
            automation_state_path(self.root / "outside/race.json", self.config, self.root)


if __name__ == "__main__":
    unittest.main()
