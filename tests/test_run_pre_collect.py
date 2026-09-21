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
import run_pre  # noqa: E402
import run_pre_collect  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from utils import load_race_json  # noqa: E402


FUKUSHIMA = "202603020711"
HAKODATE = "202602020711"
TOKYO = "202605020711"
KYOTO = "202608020711"
KOKURA = "202610020711"
NIIGATA_7R = "202604020207"
CHUKYO_7R = "202607020207"
NIIGATA_8R = "202604030408"
SAPPORO_8R = "202601020308"


def race(track: str, name: str, start_time: str, race_number: int = 11) -> dict:
    return {
        "date": None,
        "track": track,
        "race_number": race_number,
        "race_name": name,
        "start_time": start_time,
        "surface": "芝",
        "distance": 1800,
        "odds_captured_at": None,
        "odds_reference_minutes_before_start": 60,
    }


class DefaultRaceSelectionTests(unittest.TestCase):
    def test_confirmed_cancellation_publishes_without_prediction_or_simulation(self):
        from utils import atomic_write_json, ensure_race_payload
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            config = {"data_dir": str(Path(tmp) / "data"), "public_dir": str(Path(tmp) / "public"),
                      "target_races": ["中山"], "odds_reference_minutes_before_start": 60}
            for module in ("collect", "run_pre_collect", "run_pre", "evaluation_summary"):
                stack.enter_context(patch(f"{module}.setup_logger", return_value=logging.getLogger("test-cancel")))
            path = Path(tmp) / "data/races/2026-09-20/nakayama_11r.json"
            payload = ensure_race_payload(None, "202606040711")
            payload["race"] = {"date": "2026-09-20", "track": "中山", "race_number": 11,
                               "start_time": "15:40", "race_name": "中止テスト", "cancelled": True}
            payload["prediction"] = [{"id": "p1", "statistical": {"horses": []}}]
            atomic_write_json(path, payload)
            stack.enter_context(patch.object(collect_module, "fetch_html", return_value=(
                '<div class="RaceNotice">このレースは中止となりました</div>', collect_module.SHUTUBA_URL.format(race_id="202606040711"))))
            odds = stack.enter_context(patch.object(collect_module, "fetch_validated_win_odds", side_effect=AssertionError("no odds for cancelled race")))
            predict = stack.enter_context(patch.object(run_pre, "predict_paths", return_value=[]))
            for resume in (False, True):
                self.assertEqual(run_pre.run_pre_flow(config, "2026-09-20", phase="general", race_id="202606040711", resume=resume), [path])
            self.assertTrue(load_race_json(path)["race"]["cancelled"])
            self.assertEqual(load_race_json(path)["prediction"], payload["prediction"])
            self.assertTrue(all(call.args[0] == [] for call in predict.call_args_list))
            odds.assert_not_called()
            self.assertTrue((Path(tmp) / "public/races/2026-09-20/nakayama_11r.html").exists())

    def test_graded_only_includes_flat_grades_and_excludes_jump_grades(self) -> None:
        html = "".join(
            f'<li class="RaceList_DataItem"><a href="?race_id=2026090403{number:02d}">'
            f'<span class="Icon_GradeType{grade}"></span></a></li>'
            for number, grade in ((1, 1), (2, 2), (3, 3), (4, 12), (5, 11), (6, 10), (7, 4))
        )
        with patch.object(collect_module, "fetch_html", side_effect=[
            '<li class="Active" date="20260919" group="1"></li>', html,
        ]):
            ids = collect_module.discover_race_ids(None, "2026-09-19", race_number=None, graded_only=True)
        self.assertEqual(ids, [f"2026090403{number:02d}" for number in range(1, 4)])

    def test_mobile_race_list_is_used_when_desktop_endpoint_fails(self) -> None:
        mobile_html = """
        <ul class="Tab">
          <li><a data-date="20260822">8/22</a></li>
          <li><a class="Tab_Active" data-date="20260823">8/23</a></li>
        </ul>
        <div class="RaceList_Slide">
          <div class="RaceList_Main_Box">
            <a href="?race_id=202604030111"><span class="Race_Num">11R</span></a>
          </div>
        </div>
        <div class="RaceList_Slide">
          <div class="RaceList_Main_Box">
            <a href="?race_id=202604030207">
              <span class="Race_Num">7R</span>
              <span class="Icon_GradeType3">GIII</span>
            </a>
          </div>
          <div class="RaceList_Main_Box">
            <a href="?race_id=202607030211"><span class="Race_Num">11R</span></a>
          </div>
        </div>
        """
        with patch.object(
            collect_module,
            "fetch_html",
            side_effect=[collect_module.requests.HTTPError("blocked"), mobile_html],
        ):
            race_ids = collect_module.discover_race_ids(
                None,
                "2026-08-23",
                race_number=None,
                graded_only=True,
            )

        self.assertEqual(race_ids, ["202604030207"])

    def test_date_pre_selects_all_target_graded_races_regardless_of_number(self) -> None:
        tracks = {
            CHUKYO_7R: "中京",
            NIIGATA_8R: "新潟",
            SAPPORO_8R: "札幌",
            FUKUSHIMA: "福島",
        }

        def discover(_session, _target_date, race_number=11, graded_only=False):
            if graded_only:
                return [NIIGATA_8R, SAPPORO_8R, CHUKYO_7R]
            return [FUKUSHIMA]

        with ExitStack() as stack:
            discovered = stack.enter_context(
                patch.object(collect_module, "discover_race_ids", side_effect=discover)
            )
            stack.enter_context(
                patch.object(collect_module, "track_name_from_race_id", side_effect=tracks.get)
            )
            selected = collect_module.discover_pre_race_ids(
                None,
                "2026-08-30",
                {"中京", "新潟", "福島"},
            )

        self.assertEqual(selected, sorted([CHUKYO_7R, NIIGATA_8R]))
        discovered.assert_called_once_with(
            None,
            "2026-08-30",
            race_number=None,
            graded_only=True,
        )

    def test_date_pre_falls_back_to_target_track_11r_when_no_target_grade_exists(self) -> None:
        tracks = {
            SAPPORO_8R: "札幌",
            FUKUSHIMA: "福島",
            KOKURA: "小倉",
            HAKODATE: "函館",
        }

        def discover(_session, _target_date, race_number=11, graded_only=False):
            if graded_only:
                return [SAPPORO_8R]
            return [FUKUSHIMA, KOKURA, HAKODATE]

        with ExitStack() as stack:
            discovered = stack.enter_context(
                patch.object(collect_module, "discover_race_ids", side_effect=discover)
            )
            stack.enter_context(
                patch.object(collect_module, "track_name_from_race_id", side_effect=tracks.get)
            )
            selected = collect_module.discover_pre_race_ids(
                None,
                "2026-08-30",
                {"福島", "小倉"},
            )

        self.assertEqual(selected, sorted([FUKUSHIMA, KOKURA]))
        self.assertEqual(discovered.call_count, 2)
        self.assertEqual(discovered.call_args_list[1].args, (None, "2026-08-30"))

    def test_pre_collection_without_selected_ids_uses_date_pre_discovery(self) -> None:
        logger = logging.getLogger("test.pre-race-discovery")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        config = {"target_races": ["福島"]}

        with tempfile.TemporaryDirectory() as directory:
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(collect_module, "setup_logger", return_value=logger)
                )
                pre_discovery = stack.enter_context(
                    patch.object(collect_module, "discover_pre_race_ids", return_value=[])
                )
                default_discovery = stack.enter_context(
                    patch.object(collect_module, "discover_race_ids")
                )
                paths = collect_module.collect_races(
                    config,
                    "test-pre-race-discovery",
                    "2026-08-30",
                    "pre",
                    Path(directory),
                )

        self.assertEqual(paths, [])
        default_discovery.assert_not_called()
        self.assertEqual(pre_discovery.call_count, 1)
        self.assertEqual(
            pre_discovery.call_args.args[1:],
            ("2026-08-30", {"福島"}),
        )

    def test_post_collection_keeps_default_11r_discovery(self) -> None:
        logger = logging.getLogger("test.post-race-discovery")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        config = {"target_races": ["福島"]}

        with tempfile.TemporaryDirectory() as directory:
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(collect_module, "setup_logger", return_value=logger)
                )
                pre_discovery = stack.enter_context(
                    patch.object(collect_module, "discover_pre_race_ids")
                )
                discovery = stack.enter_context(
                    patch.object(collect_module, "discover_race_ids", return_value=[])
                )
                paths = collect_module.collect_races(
                    config,
                    "test-post-race-discovery",
                    "2026-08-30",
                    "post",
                    Path(directory),
                )

        self.assertEqual(paths, [])
        pre_discovery.assert_not_called()
        self.assertEqual(discovery.call_count, 1)
        self.assertEqual(discovery.call_args.args[1:], ("2026-08-30",))

    def test_grade_icon_is_used_when_race_name_has_no_grade_text(self) -> None:
        soup = BeautifulSoup(
            '<h1 class="RaceName">小倉記念<span class="Icon_GradeType Icon_GradeType3"></span></h1>',
            "html.parser",
        )

        self.assertEqual(run_pre_collect.grade_rank("小倉記念", soup), 1)

    def select(self, schedule: dict[str, list[str]], races: dict[str, dict]):
        seen_dates = []

        def discover(_session, target_date, race_number=11, graded_only=False):
            seen_dates.append((target_date, race_number, graded_only))
            race_ids = schedule.get(target_date, [])
            if graded_only:
                race_ids = [
                    race_id
                    for race_id in race_ids
                    if run_pre_collect.grade_rank(races[race_id]["race_name"]) > 0
                ]
            if race_number is not None:
                race_ids = [
                    race_id
                    for race_id in race_ids
                    if races[race_id]["race_number"] == race_number
                ]
            return race_ids

        def overview(_html, race_id, target_date, _reference_minutes):
            value = dict(races[race_id])
            value["date"] = target_date
            return value

        config = {
            "target_races": ["福島", "函館", "新潟", "東京", "中京", "京都", "小倉"],
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

    def test_default_selection_respects_grades_dates_period_and_fallback(self):
        cases = (
            ("later_nearest_graded_date_beats_first_non_graded_date", {'2026-07-18': [FUKUSHIMA], '2026-07-19': [KOKURA]},
             {FUKUSHIMA: race('福島', '阿武隈S', '15:45'), KOKURA: race('小倉', '小倉記念 (G3)', '15:35')}, "2026-07-19", {KOKURA}),
            ("two_graded_races_on_same_date_are_both_selected", {'2026-07-18': [KOKURA, HAKODATE]},
             {KOKURA: race('小倉', '小倉記念 (G3)', '15:35'), HAKODATE: race('函館', '函館2歳S (G3)', '15:25')}, "2026-07-18", {KOKURA, HAKODATE}),
            ("different_grades_on_same_date_are_all_selected", {'2026-07-18': [TOKYO, KYOTO, KOKURA]},
             {TOKYO: race('東京', 'G1テスト (G1)', '15:40'), KYOTO: race('京都', 'G2テスト (G2)', '15:35'), KOKURA: race('小倉', 'G3テスト (G3)', '15:30')}, "2026-07-18", {TOKYO, KYOTO, KOKURA}),
            ("all_graded_races_in_same_race_period_are_selected", {'2026-07-18': [KOKURA, HAKODATE], '2026-07-19': [TOKYO]},
             {KOKURA: race('小倉', '小倉記念 (G3)', '15:35'), HAKODATE: race('函館', '函館2歳S (G3)', '15:25'), TOKYO: race('東京', '翌日重賞 (G1)', '15:40')}, "2026-07-18", {KOKURA, HAKODATE, TOKYO}),
            ("graded_races_are_selected_regardless_of_race_number", {'2026-07-18': [NIIGATA_7R, CHUKYO_7R, FUKUSHIMA]},
             {NIIGATA_7R: race('新潟', '関屋記念 (G3)', '15:45', 7), CHUKYO_7R: race('中京', '東海S (G3)', '15:35', 7), FUKUSHIMA: race('福島', '非重賞', '15:25')}, "2026-07-18", {NIIGATA_7R, CHUKYO_7R}),
            ("later_race_period_is_not_included", {'2026-07-18': [KOKURA], '2026-07-19': [HAKODATE], '2026-07-25': [TOKYO]},
             {KOKURA: race('小倉', '小倉記念 (G3)', '15:35'), HAKODATE: race('函館', '函館2歳S (G3)', '15:25'), TOKYO: race('東京', '翌週重賞 (G1)', '15:40')}, "2026-07-18", {KOKURA, HAKODATE}),
            ("no_graded_race_falls_back_to_all_11r_in_race_period", {'2026-07-18': [FUKUSHIMA, KOKURA], '2026-07-19': [HAKODATE]},
             {FUKUSHIMA: race('福島', '非重賞A', '15:30'), KOKURA: race('小倉', '非重賞B', '15:45'), HAKODATE: race('函館', '非重賞C', '15:50')}, "2026-07-18", {FUKUSHIMA, KOKURA, HAKODATE}),
        )
        for name, dates, races, expected_date, expected_ids in cases:
            with self.subTest(case=name):
                (target_date, items, reason), seen_dates = self.select(dates, races)
                self.assertEqual(target_date, expected_date)
                self.assertEqual({item["race_id"] for item in items}, expected_ids)
                self.assertTrue(reason)
                if name == "different_grades_on_same_date_are_all_selected":
                    self.assertEqual({item["grade_rank"] for item in items}, {1, 2, 3})
                    self.assertEqual(len(items), 3)
                if name == "all_graded_races_in_same_race_period_are_selected":
                    self.assertIn(("2026-07-20", None, True), seen_dates)
                    self.assertNotIn(("2026-07-21", None, True), seen_dates)
                if name == "later_race_period_is_not_included":
                    self.assertNotIn(("2026-07-25", None, True), seen_dates)

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
        self.assertIsNone(collect.call_args.kwargs["selected_race_ids"])

    def test_collect_cli_date_pre_uses_pre_collection_mode(self) -> None:
        config = {"target_races": ["中京", "新潟"]}
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    sys,
                    "argv",
                    ["collect.py", "--date", "2026-08-30", "--mode", "pre"],
                )
            )
            stack.enter_context(patch.object(collect_module, "load_config", return_value=config))
            stack.enter_context(
                patch.object(
                    collect_module,
                    "parse_target_date",
                    return_value="2026-08-30",
                )
            )
            collect = stack.enter_context(patch.object(collect_module, "collect_races"))
            collect_module.main()

        collect.assert_called_once_with(config, "pre", "2026-08-30", "pre")

    def test_main_batches_selected_tracks_and_inputs_by_date(self) -> None:
        for dates in (("2026-07-18", "2026-07-18"), ("2026-07-18", "2026-07-19")):
            with self.subTest(dates=dates), ExitStack() as stack:
                selected = [
                    {"race_id": KOKURA, "race": race("小倉", "小倉記念 (G3)", "15:35"), "grade_rank": 1},
                    {"race_id": HAKODATE, "race": race("函館", "函館2歳S (G3)", "15:25"), "grade_rank": 1},
                ]
                for item, date in zip(selected, dates):
                    item["race"]["date"] = date
                config = {"target_races": ["福島", "小倉", "函館"], "odds_reference_minutes_before_start": 60}
                paths = {KOKURA: Path("kokura.json"), HAKODATE: Path("hakodate.json")}
                stack.enter_context(patch.object(sys, "argv", ["run_pre_collect.py"]))
                stack.enter_context(patch.object(run_pre_collect, "load_config", return_value=config))
                stack.enter_context(patch.object(run_pre_collect, "select_default_races", return_value=(dates[0], selected, "selected")))
                stack.enter_context(patch.object(run_pre_collect, "now_jst", return_value=datetime(2026, 7, 17, 12, tzinfo=timezone(timedelta(hours=9)))))
                collect = stack.enter_context(patch.object(run_pre_collect, "collect_races",
                    side_effect=lambda *args, selected_race_ids: [paths[rid] for rid in selected_race_ids]))
                export = stack.enter_context(patch.object(run_pre_collect, "export_prediction_chat_input", return_value=list(paths.values())))
                stack.enter_context(patch("builtins.print"))
                run_pre_collect.main()
                self.assertEqual(len(collect.call_args_list), len(set(dates)))
                for call, date in zip(collect.call_args_list, dict.fromkeys(dates)):
                    expected = [item for item in selected if item["race"]["date"] == date]
                    self.assertEqual(call.args[2], date)
                    self.assertEqual(call.args[0]["target_races"], [item["race"]["track"] for item in selected])
                    self.assertEqual(call.kwargs["selected_race_ids"], [item["race_id"] for item in expected])
                self.assertEqual(export.call_args.args[0], list(paths.values()))

class MultipleRaceGenerationTests(unittest.TestCase):
    def test_collection_rejects_existing_race_id_mismatch_without_writes(self):
        from utils import atomic_write_json, race_json_path
        with tempfile.TemporaryDirectory() as tmp:
            config = {"data_dir": tmp, "target_races": [collect_module.track_name_from_race_id(NIIGATA_7R)]}
            path = race_json_path(config, "2026-07-26", config["target_races"][0], 7)
            atomic_write_json(path, {"meta": {"race_id": "old-race"}, "prediction": {"horses": []}})
            before = path.read_bytes()
            with patch.object(collect_module, "fetch_html") as fetch, patch.object(collect_module, "setup_logger"):
                with self.assertRaisesRegex(ValueError, "race_id mismatch"):
                    collect_module.collect_races(config, "test-identity", "2026-07-26", "pre", selected_race_ids=[NIIGATA_7R])
                fetch.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_each_selected_race_gets_separate_race_and_chat_json(self) -> None:
        race_ids = [NIIGATA_7R, CHUKYO_7R]
        tracks = {NIIGATA_7R: "新潟", CHUKYO_7R: "中京"}

        def overview(_html, race_id, target_date, reference_minutes, **_kwargs):
            value = race(tracks[race_id], f"{tracks[race_id]}重賞 (G3)", "15:35", 7)
            value["date"] = target_date
            value["odds_reference_minutes_before_start"] = reference_minutes
            value["source_url"] = f"https://example.invalid/{race_id}"
            return value

        def horses(_session, _html, _race, race_id, _odds, _existing, **_kwargs):
            return [
                {
                    "horse_number": 1,
                    "horse_name": f"horse-{race_id}",
                    "win_odds": 2.5,
                    "past_runs": [],
                }
            ]

        def fetched(_session, url, *, return_source_url=False):
            return ("<html></html>", url) if return_source_url else "<html></html>"

        logger = logging.getLogger("test.multiple-race-generation")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "target_races": ["新潟", "中京"],
                "odds_reference_minutes_before_start": 60,
                "data_dir": str(root / "data"),
            }
            with ExitStack() as stack:
                stack.enter_context(patch.object(collect_module, "setup_logger", return_value=logger))
                discovery = stack.enter_context(
                    patch.object(collect_module, "discover_pre_race_ids")
                )
                stack.enter_context(patch.object(collect_module, "track_name_from_race_id", side_effect=tracks.get))
                stack.enter_context(patch.object(collect_module, "fetch_html", side_effect=fetched))
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
                paths = collect_module.collect_races(
                    config,
                    "test-multiple-collect",
                    "2026-07-19",
                    "pre",
                    root,
                    selected_race_ids=race_ids,
                )

            discovery.assert_not_called()

            config["data_dir"] = str(root / "data")
            outbox = root / "data/prediction_inputs/2026-07-19"
            with ExitStack() as stack:
                stack.enter_context(patch.object(run_pre_collect, "setup_logger", return_value=logger))
                exported = run_pre_collect.export_prediction_chat_input(paths, config, "test-multiple-export")

            self.assertEqual({path.name for path in paths}, {"niigata_7r.json", "chukyo_7r.json"})
            self.assertEqual({path.name for path in exported}, {"niigata_7r.json", "chukyo_7r.json"})

            for path in paths:
                payload = load_race_json(path)
                chat_input = json.loads((outbox / path.name).read_text(encoding="utf-8"))
                self.assertEqual(chat_input["meta"]["race_id"], payload["meta"]["race_id"])
                self.assertEqual(chat_input["race"], payload["race"])
                self.assertEqual(chat_input["horses"], payload["horses"])
                self.assertEqual(payload["simulation"], [])
                other_id = next(race_id for race_id in race_ids if race_id != payload["meta"]["race_id"])
                self.assertNotIn(other_id, json.dumps(chat_input, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
