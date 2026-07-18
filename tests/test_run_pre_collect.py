from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collect as collect_module  # noqa: E402
import run_pre_collect  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from utils import load_race_json  # noqa: E402


FUKUSHIMA = "202603020711"
HAKODATE = "202602020711"
TOKYO = "202605020711"
KYOTO = "202608020711"
KOKURA = "202610020711"


def race(track: str, name: str, start_time: str) -> dict:
    return {
        "date": None,
        "track": track,
        "race_number": 11,
        "race_name": name,
        "start_time": start_time,
        "surface": "芝",
        "distance": 1800,
        "odds_captured_at": None,
        "odds_reference_minutes_before_start": 60,
    }


class DefaultRaceSelectionTests(unittest.TestCase):
    def test_grade_icon_is_used_when_race_name_has_no_grade_text(self) -> None:
        soup = BeautifulSoup(
            '<h1 class="RaceName">小倉記念<span class="Icon_GradeType Icon_GradeType3"></span></h1>',
            "html.parser",
        )

        self.assertEqual(run_pre_collect.grade_rank("小倉記念", soup), 1)

    def select(self, schedule: dict[str, list[str]], races: dict[str, dict]):
        seen_dates = []

        def discover(_session, target_date):
            seen_dates.append(target_date)
            return schedule.get(target_date, [])

        def overview(_html, race_id, target_date, _reference_minutes):
            value = dict(races[race_id])
            value["date"] = target_date
            return value

        config = {
            "target_races": ["福島", "函館", "東京", "京都", "小倉"],
            "odds_reference_minutes_before_start": 60,
        }
        track_by_id = {race_id: value["track"] for race_id, value in races.items()}
        with ExitStack() as stack:
            stack.enter_context(patch.object(run_pre_collect, "today_jst", return_value="2026-07-18"))
            stack.enter_context(patch.object(run_pre_collect, "discover_race_ids", side_effect=discover))
            stack.enter_context(patch.object(run_pre_collect, "fetch_html", return_value="<html></html>"))
            stack.enter_context(patch.object(run_pre_collect, "find_entry_table", return_value=object()))
            stack.enter_context(patch.object(run_pre_collect, "parse_race_overview", side_effect=overview))
            stack.enter_context(patch.object(run_pre_collect, "track_name_from_race_id", side_effect=track_by_id.get))
            selected = run_pre_collect.select_default_races(config)
        return selected, seen_dates

    def test_later_nearest_graded_date_beats_first_non_graded_date(self) -> None:
        selected, _ = self.select(
            {
                "2026-07-18": [FUKUSHIMA],
                "2026-07-19": [KOKURA],
            },
            {
                FUKUSHIMA: race("福島", "阿武隈S", "15:45"),
                KOKURA: race("小倉", "小倉記念 (G3)", "15:35"),
            },
        )

        target_date, items, reason = selected
        self.assertEqual(target_date, "2026-07-19")
        self.assertEqual([item["race_id"] for item in items], [KOKURA])
        self.assertEqual(reason, "graded races on nearest graded race date")

    def test_two_graded_races_on_same_date_are_both_selected(self) -> None:
        selected, _ = self.select(
            {"2026-07-18": [KOKURA, HAKODATE]},
            {
                KOKURA: race("小倉", "小倉記念 (G3)", "15:35"),
                HAKODATE: race("函館", "函館2歳S (G3)", "15:25"),
            },
        )

        self.assertEqual({item["race_id"] for item in selected[1]}, {KOKURA, HAKODATE})

    def test_different_grades_on_same_date_are_all_selected(self) -> None:
        selected, _ = self.select(
            {"2026-07-18": [TOKYO, KYOTO, KOKURA]},
            {
                TOKYO: race("東京", "G1テスト (G1)", "15:40"),
                KYOTO: race("京都", "G2テスト (G2)", "15:35"),
                KOKURA: race("小倉", "G3テスト (G3)", "15:30"),
            },
        )

        self.assertEqual({item["grade_rank"] for item in selected[1]}, {1, 2, 3})
        self.assertEqual(len(selected[1]), 3)

    def test_first_graded_date_stops_search_before_later_dates(self) -> None:
        selected, seen_dates = self.select(
            {
                "2026-07-18": [KOKURA, HAKODATE],
                "2026-07-19": [TOKYO],
            },
            {
                KOKURA: race("小倉", "小倉記念 (G3)", "15:35"),
                HAKODATE: race("函館", "函館2歳S (G3)", "15:25"),
                TOKYO: race("東京", "翌日重賞 (G1)", "15:40"),
            },
        )

        self.assertEqual(selected[0], "2026-07-18")
        self.assertEqual(seen_dates, ["2026-07-18"])

    def test_no_graded_race_falls_back_to_latest_11r_on_first_date(self) -> None:
        selected, _ = self.select(
            {
                "2026-07-18": [FUKUSHIMA, KOKURA],
                "2026-07-19": [HAKODATE],
            },
            {
                FUKUSHIMA: race("福島", "非重賞A", "15:30"),
                KOKURA: race("小倉", "非重賞B", "15:45"),
                HAKODATE: race("函館", "非重賞C", "15:50"),
            },
        )

        target_date, items, reason = selected
        self.assertEqual(target_date, "2026-07-18")
        self.assertEqual([item["race_id"] for item in items], [KOKURA])
        self.assertEqual(reason, "fallback: no graded 11R in lookahead")

    def test_date_argument_keeps_existing_collection_path(self) -> None:
        config = {"target_races": ["福島", "小倉"]}
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["run_pre_collect.py", "--date", "2026-07-18"]))
            stack.enter_context(patch.object(run_pre_collect, "load_config", return_value=config))
            select_default = stack.enter_context(patch.object(run_pre_collect, "select_default_races"))
            collect = stack.enter_context(
                patch.object(run_pre_collect, "collect_races", return_value=[Path("race.json")])
            )
            stack.enter_context(
                patch.object(run_pre_collect, "export_prediction_chat_input", return_value=[Path("chat.json")])
            )
            run_pre_collect.main()

        select_default.assert_not_called()
        self.assertEqual(collect.call_args.args[0]["target_races"], ["福島", "小倉"])
        self.assertEqual(collect.call_args.args[2:], ("2026-07-18", "pre"))

    def test_main_passes_every_selected_track_to_collection(self) -> None:
        selected = [
            {"race_id": KOKURA, "race": race("小倉", "小倉記念 (G3)", "15:35"), "grade_rank": 1},
            {"race_id": HAKODATE, "race": race("函館", "函館2歳S (G3)", "15:25"), "grade_rank": 1},
        ]
        for item in selected:
            item["race"]["date"] = "2026-07-19"
        config = {
            "target_races": ["福島", "函館", "小倉"],
            "odds_reference_minutes_before_start": 60,
        }
        jst = timezone(timedelta(hours=9))
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["run_pre_collect.py"]))
            stack.enter_context(patch.object(run_pre_collect, "load_config", return_value=config))
            stack.enter_context(patch.object(
                run_pre_collect,
                "select_default_races",
                return_value=("2026-07-19", selected, "graded races on nearest graded race date"),
            ))
            stack.enter_context(patch.object(
                run_pre_collect,
                "target_odds_datetime",
                side_effect=[datetime(2026, 7, 19, 14, 35, tzinfo=jst), datetime(2026, 7, 19, 14, 25, tzinfo=jst)],
            ))
            stack.enter_context(
                patch.object(run_pre_collect, "now_jst", return_value=datetime(2026, 7, 18, 12, 0, tzinfo=jst))
            )
            collect = stack.enter_context(
                patch.object(
                    run_pre_collect,
                    "collect_races",
                    return_value=[Path("kokura.json"), Path("hakodate.json")],
                )
            )
            stack.enter_context(
                patch.object(
                    run_pre_collect,
                    "export_prediction_chat_input",
                    return_value=[Path("kokura.json"), Path("hakodate.json")],
                )
            )
            output = stack.enter_context(patch("builtins.print"))
            run_pre_collect.main()

        self.assertEqual(collect.call_args.args[0]["target_races"], ["小倉", "函館"])
        lines = [call.args[0] for call in output.call_args_list]
        self.assertEqual(sum("selected race:" in line for line in lines), 2)
        self.assertEqual(sum("grade=G3" in line for line in lines), 2)


class MultipleRaceGenerationTests(unittest.TestCase):
    def test_each_selected_race_gets_separate_race_and_chat_json(self) -> None:
        race_ids = [KOKURA, HAKODATE]
        tracks = {KOKURA: "小倉", HAKODATE: "函館"}

        def overview(_html, race_id, target_date, reference_minutes):
            value = race(tracks[race_id], f"{tracks[race_id]}重賞 (G3)", "15:35")
            value["date"] = target_date
            value["odds_reference_minutes_before_start"] = reference_minutes
            value["source_url"] = f"https://example.invalid/{race_id}"
            return value

        def horses(_session, _html, _race, race_id, _odds, _existing):
            return [
                {
                    "horse_number": 1,
                    "horse_name": f"horse-{race_id}",
                    "win_odds": 2.5,
                    "past_runs": [],
                }
            ]

        logger = logging.getLogger("test.multiple-race-generation")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "target_races": ["小倉", "函館"],
                "odds_reference_minutes_before_start": 60,
                "data_dir": str(root / "data"),
            }
            with ExitStack() as stack:
                stack.enter_context(patch.object(collect_module, "setup_logger", return_value=logger))
                stack.enter_context(patch.object(collect_module, "discover_race_ids", return_value=race_ids))
                stack.enter_context(patch.object(collect_module, "track_name_from_race_id", side_effect=tracks.get))
                stack.enter_context(patch.object(collect_module, "fetch_html", return_value="<html></html>"))
                stack.enter_context(patch.object(collect_module, "parse_race_overview", side_effect=overview))
                stack.enter_context(patch.object(
                    collect_module,
                    "fetch_validated_win_odds",
                    return_value=(
                        {1: {"win_odds": 2.5, "popularity": 1}},
                        "2026-07-18T12:00:00+09:00",
                        "netkeiba",
                        "https://example.invalid/odds",
                    ),
                ))
                stack.enter_context(patch.object(collect_module, "parse_horses", side_effect=horses))
                paths = collect_module.collect_races(config, "test-multiple-collect", "2026-07-19", "pre", root)

            outbox = root / "outbox"
            with ExitStack() as stack:
                stack.enter_context(patch.object(run_pre_collect, "setup_logger", return_value=logger))
                stack.enter_context(patch.object(run_pre_collect, "outbox_chat_input_dir", return_value=outbox))
                exported = run_pre_collect.export_prediction_chat_input(paths, config, "test-multiple-export")

            self.assertEqual({path.name for path in paths}, {"kokura_11r.json", "hakodate_11r.json"})
            self.assertEqual({path.name for path in exported}, {"kokura_11r.json", "hakodate_11r.json"})

            for path in paths:
                payload = load_race_json(path)
                chat_input = json.loads((outbox / path.name).read_text(encoding="utf-8"))
                self.assertEqual(chat_input["meta"]["race_id"], payload["meta"]["race_id"])
                self.assertEqual(chat_input["race"], payload["race"])
                self.assertEqual(chat_input["horses"], payload["horses"])
                self.assertEqual(payload["simulation"], {
                    "value": {"pre": None, "post": None},
                    "dutching": {"pre": None, "post": None},
                })
                other_id = next(race_id for race_id in race_ids if race_id != payload["meta"]["race_id"])
                self.assertNotIn(other_id, json.dumps(chat_input, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
