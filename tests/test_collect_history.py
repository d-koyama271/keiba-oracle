from __future__ import annotations

import html
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collect import (  # noqa: E402
    build_career_summaries,
    build_horse_summaries,
    normalize_class_grade,
    normalize_going,
    normalize_weather,
    parse_body_weight,
    parse_horse_history,
    parse_race_overview,
    parse_race_time,
)
from predict import build_prediction_chat_input  # noqa: E402


HISTORY_HEADERS = [
    "日付",
    "開催",
    "天気",
    "R",
    "レース名",
    "頭数",
    "枠番",
    "馬番",
    "オッズ",
    "人気",
    "着順",
    "騎手",
    "斤量",
    "距離",
    "馬場",
    "タイム",
    "着差",
    "通過",
    "ペース",
    "上り",
    "馬体重",
]


def history_html(rows: list[dict]) -> str:
    header = "".join(f"<th>{html.escape(name)}</th>" for name in HISTORY_HEADERS)
    body = []
    for row in rows:
        cells = []
        for name in HISTORY_HEADERS:
            value = html.escape(str(row.get(name, "")))
            if name == "レース名" and row.get("race_id"):
                value = f'<a href="https://db.netkeiba.com/race/{row["race_id"]}/">{value}</a>'
            cells.append(f"<td>{value}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def history_row(race_id: str, race_date: str, finish: str = "2") -> dict:
    return {
        "race_id": race_id,
        "日付": race_date,
        "開催": "2小倉8",
        "天気": "晴",
        "R": "11",
        "レース名": "小倉記念(GIII)",
        "頭数": "16",
        "枠番": "4",
        "馬番": "6",
        "オッズ": "12.5",
        "人気": "5",
        "着順": finish,
        "騎手": "川田将雅",
        "斤量": "57.5",
        "距離": "芝2000",
        "馬場": "良",
        "タイム": "1:46.8",
        "着差": "0.2",
        "通過": "5-5-4",
        "ペース": "35.0-34.8",
        "上り": "34.1",
        "馬体重": "466(-4)",
    }


class FakeResponse:
    def __init__(self, body: str):
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"status": "OK", "data": self.body}


class FakeSession:
    def __init__(self, body: str):
        self.body = body
        self.calls = 0

    def get(self, *_args, **_kwargs) -> FakeResponse:
        self.calls += 1
        return FakeResponse(self.body)


class HistoryParsingTests(unittest.TestCase):
    def test_time_body_weight_and_class_parsers(self) -> None:
        self.assertEqual(parse_race_time("1:01.4"), ("1:01.4", 61.4))
        self.assertEqual(parse_race_time("3:07.29"), ("3:07.29", 187.29))
        self.assertEqual(parse_race_time("中止"), (None, None))
        self.assertEqual(parse_race_time(""), (None, None))
        self.assertEqual(parse_body_weight("466(-4)"), (466, -4))
        self.assertEqual(parse_body_weight("476(+12)"), (476, 12))
        self.assertEqual(parse_body_weight("466"), (466, None))
        self.assertEqual(parse_body_weight("計不"), (None, None))
        class_cases = {
            "天皇賞(春)(GI)": "G1",
            "交流重賞(JpnII)": "G2",
            "小倉記念(GIII)": "G3",
            "都大路S(L)": "Listed",
            "オープン特別": "Open",
            "3勝クラス": "3-win",
            "2勝クラス": "2-win",
            "1勝クラス": "1-win",
            "3歳未勝利": "Maiden",
            "2歳新馬": "Newcomer",
            "条件不明": "Other",
        }
        for value, expected in class_cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_class_grade(value), expected)
        self.assertEqual(normalize_weather("晴"), "sunny")
        self.assertEqual(normalize_weather("曇"), "cloudy")
        self.assertEqual(normalize_weather("小雨"), "rain")
        self.assertEqual(normalize_weather("雪"), "snow")
        self.assertEqual(normalize_weather(""), None)
        self.assertEqual(normalize_going("稍"), "稍重")
        self.assertEqual(normalize_going("不良"), "不良")

    def test_current_race_weather_and_grade(self) -> None:
        race = parse_race_overview(
            """
            <h1 class="RaceName">小倉記念<span class="Icon_GradeType Icon_GradeType3"></span></h1>
            <div class="RaceData01">15:45発走 / 芝2000m / 天候:晴 / 馬場:稍重</div>
            <div class="RaceData02">サラ系3歳以上 オープン</div>
            """,
            "202610020811",
            "2026-07-19",
            60,
        )

        self.assertEqual(race["weather"], "晴")
        self.assertEqual(race["going"], "稍重")
        self.assertEqual(race["class_grade"], "G3")

    def test_target_is_removed_before_full_history_summary_and_five_run_slice(self) -> None:
        target_race_id = "202610020811"
        rows = [history_row(target_race_id, "2026/07/19", "1")]
        rows.extend(
            history_row(f"20261002{number:02d}11", f"2026/0{7 - number}/01", str(number))
            for number in range(1, 7)
        )
        session = FakeSession(history_html(rows))
        current_race = {
            "track": "小倉",
            "surface": "芝",
            "distance": 2000,
            "going": "良",
            "weather": "晴",
            "class_grade": "G3",
        }

        past_runs, summaries, previous_jockey = parse_horse_history(
            session,
            "https://db.netkeiba.com/horse/2020100001",
            target_race_id,
            current_race,
            "川田将雅",
        )

        self.assertEqual(session.calls, 1)
        self.assertEqual(len(past_runs), 5)
        self.assertEqual(summaries["overall_record"]["runs"], 6)
        self.assertNotIn(target_race_id, json.dumps([past_runs, summaries], ensure_ascii=False))
        self.assertEqual(previous_jockey, "川田将雅")
        self.assertEqual(past_runs[0]["race_id"], "202610020111")
        self.assertEqual(past_runs[0]["race_number"], 11)
        self.assertEqual(past_runs[0]["weather"], "晴")
        self.assertEqual(past_runs[0]["field_size"], 16)
        self.assertEqual(past_runs[0]["frame_number"], 4)
        self.assertEqual(past_runs[0]["horse_number"], 6)
        self.assertEqual(past_runs[0]["jockey"], "川田将雅")
        self.assertEqual(past_runs[0]["win_odds"], 12.5)
        self.assertEqual(past_runs[0]["popularity"], 5)
        self.assertEqual(past_runs[0]["race_time"], "1:46.8")
        self.assertEqual(past_runs[0]["race_time_seconds"], 106.8)
        self.assertEqual(past_runs[0]["pace"], "35.0-34.8")
        self.assertEqual(past_runs[0]["body_weight"], 466)
        self.assertEqual(past_runs[0]["body_weight_change"], -4)
        self.assertEqual(past_runs[0]["class_grade"], "G3")


class CareerSummaryTests(unittest.TestCase):
    def make_run(
        self,
        race_id: str,
        *,
        track: str = "小倉",
        surface: str = "芝",
        distance: int = 2000,
        going: str = "良",
        weather: str = "晴",
        class_grade: str = "G3",
        jockey: str = "A",
        finish: int | None = 1,
    ) -> dict:
        return {
            "race_id": race_id,
            "track": track,
            "surface": surface,
            "distance": distance,
            "going": going,
            "weather": weather,
            "class_grade": class_grade,
            "jockey": jockey,
            "finish_position": finish,
        }

    def test_all_requested_condition_records(self) -> None:
        runs = [
            self.make_run("202610010111", finish=1),
            self.make_run("202610010211", distance=2200, weather="曇", finish=3),
            self.make_run("202610010311", surface="ダート", finish=2, class_grade="Open", jockey="B"),
            self.make_run("202605010411", track="東京", distance=1800, going="稍重", weather="小雨", finish=5),
            self.make_run("202608010511", track="京都", distance=2400, weather="雨", class_grade="G2", jockey="C", finish=4),
            self.make_run("202610010611", distance=1900, finish=None),
            self.make_run("2024P0022408", finish=1),
        ]
        current_race = {
            "track": "小倉",
            "surface": "芝",
            "distance": 2000,
            "going": "良",
            "weather": "晴",
            "class_grade": "G3",
        }

        result = build_career_summaries(runs, current_race, "A")

        self.assertEqual(result["overall_record"], {"runs": 6, "wins": 1, "top3": 3, "average_finish_position": 3.0})
        self.assertEqual(result["current_track_record"], {"runs": 3, "wins": 1, "top3": 2, "average_finish_position": 2.0})
        self.assertEqual(result["current_surface_record"]["runs"], 5)
        self.assertEqual(result["current_surface_record"]["average_finish_position"], 3.25)
        self.assertEqual(result["current_distance_band_record"]["distance_min"], 1800)
        self.assertEqual(result["current_distance_band_record"]["distance_max"], 2200)
        self.assertEqual(result["current_distance_band_record"]["runs"], 4)
        self.assertEqual(result["current_going_record"]["average_finish_position"], 2.6667)
        self.assertEqual(result["current_weather_record"]["runs"], 2)
        self.assertEqual(result["current_class_record"]["runs"], 4)
        self.assertEqual(result["current_jockey_combo_record"]["runs"], 4)

        no_class_runs = build_career_summaries(
            runs,
            {**current_race, "class_grade": "Maiden"},
            "A",
        )
        self.assertEqual(no_class_runs["current_class_record"]["runs"], 0)
        unknown_class = build_career_summaries(
            runs,
            {**current_race, "class_grade": "Other"},
            "A",
        )
        self.assertIsNone(unknown_class["current_class_record"])

    def test_horse_output_and_chat_input_have_no_full_history_or_old_summaries(self) -> None:
        current_race = {
            "date": "2026-07-19",
            "track": "小倉",
            "surface": "芝",
            "distance": 2000,
            "going": "良",
            "weather": "晴",
            "class_grade": "G3",
        }
        summaries = build_career_summaries([], current_race, "A")
        horse = {"horse_number": 1, "jockey": "A", "weight_carried": 57.0}
        build_horse_summaries(horse, current_race, [], summaries, None)
        payload = {
            "meta": {"race_id": "202610020811"},
            "race": current_race,
            "horses": [horse],
        }

        with tempfile.TemporaryDirectory() as directory:
            chat_input = build_prediction_chat_input(
                {"data_dir": "data"},
                payload,
                Path(directory),
            )

        serialized = json.dumps(chat_input, ensure_ascii=False)
        self.assertEqual(set(chat_input), {"meta", "race", "horses"})
        self.assertIn("career_summaries", serialized)
        self.assertIn("class_grade", serialized)
        for forbidden in (
            "same_course_record_summary",
            "same_distance_record_summary",
            "going_record_summary",
            "all_past_runs",
            "full_history",
            "season_records",
            "frame_record",
            "horse_number_record",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
