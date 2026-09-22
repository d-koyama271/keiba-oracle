from __future__ import annotations

import json
import copy
from unittest.mock import patch
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from render import (  # noqa: E402
    build_environment,
    build_expected_value_rows,
    build_race_context,
    build_value_selection_rows,
    format_jst_datetime,
    index_row_sort_key,
    is_created_this_week,
    rank_comparison,
    render_site,
)


from test_simulation import DUTCHING_ROWS, make_payload as simulation_payload, make_config as simulation_config, make_result
from test_quinella import CAPTURED, payload_with_odds, result_html
from collect import parse_result
from evaluation import build_evaluation
from simulate import calculate_pre_simulation, calculate_value_post, calculate_dutching_post, calculate_post
from utils import ensure_race_payload, load_config, parse_jst_datetime, save_race_json


def make_payload(*, predicted: bool, track: str, date: str, name: str) -> dict:
    horses = [
        {
            "horse_number": number,
            "horse_name": f"Horse {number}",
            "jockey": f"Jockey {number}",
            "weight_carried": 54.0 + number,
            "running_style_summary": f"Style {number}",
            "win_odds": 3.0 + number,
            "popularity": number,
        }
        for number in (3, 1, 2)
    ]
    prediction = None
    if predicted:
        prediction = {
            "model_provider": "codex",
            "model_name": "gpt-test",
            "horses": [
                {"horse_number": 2, "win_probability": 0.4, "reason": "reason 2"},
                {"horse_number": 1, "win_probability": 0.4, "reason": "reason 1"},
                {"horse_number": 3, "win_probability": 0.2, "reason": "reason 3"},
            ]
        }
    return {
        "meta": {"race_id": f"{date}-{track}", "schema_version": 4},
        "race": {
            "date": date,
            "track": track,
            "race_number": 11,
            "race_name": name,
            "start_time": "15:30",
            "source_url": "https://example.invalid/race",
        },
        "horses": horses,
        "prediction": prediction,
        "simulation": {
            "value": {"pre": None, "post": None},
            "dutching": {"pre": None, "post": None},
        },
        "result": None,
        "evaluation": None,
        "feedback": None,
    }


class RenderTests(unittest.TestCase):
    def test_publication_states_and_cancelled_pages(self):
        from utils import ensure_race_payload, atomic_write_json, calculate_phase_times
        payload = ensure_race_payload(make_payload(predicted=True, track="中山", date="2026-09-20", name="状態テスト"))
        payload["race"]["start_time"] = "00:10"
        payload["race"].update(going="良", weather="晴", class_grade="G2", age_condition="3歳", sex_condition="牡牝", weight_condition="馬齢",
                               race_info_captured_at="2026-09-19T23:00:02+09:00", odds_captured_at="2026-09-19T23:01:02+09:00")
        config = {"data_dir": "data", "public_dir": "public", "automation": {
            "statistical_time": "17:25", "general_minutes_before_start": 37, "result_minutes_after_start": 19,
        }}
        entry = payload["prediction"][0]
        entry["statistical"] = entry.pop("general")
        self.assertEqual(build_race_context(payload)["status_label"], "前日予想公開")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "templates", root / "templates")
            path = root / "data/races/2026-09-20/nakayama_11r.json"
            for state, label in (("statistical", "前日予想公開"), ("general", "直前予想公開"), ("result", "結果公開"), ("cancelled", "開催中止")):
                if state == "general":
                    entry["general"] = entry["statistical"]
                if state == "result":
                    payload["result"] = make_result(1, 400, [1, 2, 3])
                    payload["result"].update(going="重", weather="雨")
                    payload["race"]["odds_official_datetime"] = "2026-09-19T23:00:00+09:00"
                    payload["evaluation"] = build_evaluation(payload)
                if state == "cancelled":
                    payload["race"]["cancelled"] = True
                    payload["result"] = {"horses": []}
                    payload["evaluation"] = [{"prediction_id": entry["id"], "general": {"metrics": {}}}]
                atomic_write_json(path, payload)
                output = render_site(config, "test-status", root=root)
                index = BeautifulSoup((output / "index.html").read_text(encoding="utf-8"), "html.parser")
                page = BeautifulSoup((output / "races/2026-09-20/nakayama_11r.html").read_text(encoding="utf-8"), "html.parser")
                self.assertEqual(index.select_one(".status").get_text(), label)
                self.assertEqual(page.select_one(".status").get_text(), label)
                basic = page.select_one(".panel")
                self.assertIn("G2", basic.h1.get_text())
                values = {
                    node.strong.get_text(): node.get_text(" ", strip=True).split(" ", 1)[1]
                    for node in basic.select(".meta > div")
                    if node.strong
                }
                self.assertEqual({key: values[key] for key in ("馬場", "天候", "頭数", "条件", "負担重量")},
                                 {"馬場": "良", "天候": "晴", "頭数": "3頭", "条件": "3歳・牡牝", "負担重量": "馬齢"})
                self.assertIn("2026-09-19 23:00", basic.get_text())
                statistical = page.select_one('.prediction-method-content[data-ai-method$="statistical"]')
                self.assertNotIn("使用オッズ", statistical.get_text())
                general = page.select_one('.prediction-method-content[data-ai-method$="general"]')
                if state == "statistical":
                    self.assertIsNone(general)
                    self.assertNotIn("使用オッズ", page.get_text())
                else:
                    self.assertIn("23:01取得" if state == "general" else "23:00時点", general.get_text())
                self.assertEqual((output / "races/2026-09-20/nakayama_11r_result.html").exists(), state == "result")
                if state == "result":
                    result_page = BeautifulSoup((output / "races/2026-09-20/nakayama_11r_result.html").read_text(encoding="utf-8"), "html.parser")
                    self.assertEqual(result_page.select_one(".status").get_text(), "結果公開")
                    self.assertIn("雨", result_page.select_one(".meta").get_text())
                next_update = index.select_one(".status").parent.find("time")
                if state in ("statistical", "general"):
                    phase = "general" if state == "statistical" else "result"
                    expected = calculate_phase_times(payload["race"], config)[phase]
                    self.assertEqual(next_update["datetime"], expected.isoformat())
                    self.assertIn(expected.strftime("%H:%M"), next_update.get_text())
                    if state == "statistical":
                        self.assertIn(expected.strftime("%m/%d"), next_update.get_text())
                else:
                    self.assertIsNone(next_update)
                for value in config["automation"].values():
                    self.assertIn(str(value), index.get_text())
                self.assertIsNone(page.select_one(".next-update"))

    def test_index_sort_prioritizes_status_then_latest_start(self) -> None:
        rows = [
            {"name": "result", "status": "result_published", "date": "2026-07-20", "start_time": "16:00", "track": "東京", "href": "result"},
            {"name": "ongoing", "status": "awaiting_result", "date": "2026-07-21", "start_time": "16:00", "track": "中山", "href": "ongoing"},
            {"name": "prediction_old", "status": "general_published", "date": "2026-07-18", "start_time": "15:45", "track": "福島", "href": "prediction-old"},
            {"name": "prediction_early", "status": "general_published", "date": "2026-07-19", "start_time": "15:20", "track": "函館", "href": "prediction-early"},
            {"name": "prediction_late", "status": "general_published", "date": "2026-07-19", "start_time": "15:45", "track": "小倉", "href": "prediction-late"},
        ]

        ordered = sorted(rows, key=index_row_sort_key)

        self.assertEqual(
            [row["name"] for row in ordered],
            ["prediction_late", "prediction_early", "prediction_old", "ongoing", "result"],
        )

    def test_datetime_is_displayed_in_jst_without_changing_source(self) -> None:
        self.assertEqual(format_jst_datetime("2026-07-18T18:07:48+09:00"), "2026-07-18 18:07:48")
        self.assertEqual(format_jst_datetime("2026-07-18T09:07:48+00:00"), "2026-07-18 18:07:48")
        self.assertEqual(format_jst_datetime("2026-07-18T12:07:48+03:00"), "2026-07-18 18:07:48")
        self.assertEqual(format_jst_datetime(None), "-")

        payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        saved_value = "2026-07-18T09:07:48+00:00"
        payload["race"]["odds_captured_at"] = saved_value
        context = build_race_context(payload)

        self.assertEqual(context["odds_captured_at_label"], "2026-07-18 18:07:48")
        self.assertEqual(payload["race"]["odds_captured_at"], saved_value)

    def test_created_this_week_uses_jst_monday_to_sunday(self) -> None:
        jst = timezone(timedelta(hours=9))
        reference = datetime(2026, 8, 30, 12, 0, tzinfo=jst)

        self.assertTrue(is_created_this_week("2026-08-24T00:00:00+09:00", reference))
        self.assertTrue(is_created_this_week("2026-08-30T23:59:59+09:00", reference))
        self.assertTrue(is_created_this_week("2026-08-23T15:00:00+00:00", reference))
        self.assertFalse(is_created_this_week("2026-08-23T23:59:59+09:00", reference))
        self.assertFalse(is_created_this_week("2026-08-31T00:00:00+09:00", reference))
        self.assertFalse(is_created_this_week(None, reference))

    def test_new_badge_only_marks_current_week_prediction_pages(self) -> None:
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst)
        race_date = now.date().isoformat()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_dir = root / "data" / "races" / race_date
            race_dir.mkdir(parents=True)

            prediction_payload = make_payload(
                predicted=True,
                track="中山",
                date=race_date,
                name="今週予想レース",
            )
            result_payload = make_payload(
                predicted=True,
                track="東京",
                date=race_date,
                name="今週結果レース",
            )
            for payload in (prediction_payload, result_payload):
                payload["meta"]["created_at"] = now.isoformat(timespec="seconds")
            result_payload["result"] = {
                "finish_order": [1, 2, 3],
                "horses": [
                    {"horse_number": 1, "finish_position": 1},
                    {"horse_number": 2, "finish_position": 2},
                    {"horse_number": 3, "finish_position": 3},
                ],
                "payouts": {"win": [{"horse_number": 1, "payout_per_100": 400}]},
            }
            result_payload["evaluation"] = {
                "winner": {
                    "horse_number": 1,
                    "predicted_probability": 0.4,
                    "predicted_rank": 1,
                },
                "metrics": {
                    "top1_hit": True,
                    "top3_hit": True,
                    "top5_hit": True,
                    "log_loss": 0.916291,
                    "brier_score": 0.24,
                },
                "market_baseline": {"available": False},
            }
            (race_dir / "nakayama_11r.json").write_text(
                json.dumps(prediction_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            (race_dir / "tokyo_11r.json").write_text(
                json.dumps(result_payload, ensure_ascii=False),
                encoding="utf-8",
            )

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-new-badge",
                root=root,
            )
            soup = BeautifulSoup(
                (output / "index.html").read_text(encoding="utf-8"),
                "html.parser",
            )
            prediction_row = soup.find(string="今週予想レース").find_parent("tr")
            result_row = soup.find(string="今週結果レース").find_parent("tr")

            self.assertIsNotNone(prediction_row.select_one(".new-badge"))
            self.assertIsNone(result_row.select_one(".new-badge"))

    def test_recorded_odds_requires_timestamp_and_at_least_one_odds_value(self) -> None:
        payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        payload["race"]["odds_captured_at"] = "2026-01-01T14:30:00+09:00"

        self.assertTrue(build_race_context(payload)["has_recorded_odds"])

        for horse in payload["horses"]:
            horse["win_odds"] = None
        self.assertFalse(build_race_context(payload)["has_recorded_odds"])

    def test_prediction_basic_info_grid_and_used_odds_line(self) -> None:
        payload = ensure_race_payload(
            make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        )
        payload["prediction"][0]["statistical"] = copy.deepcopy(
            payload["prediction"][0]["general"]
        )
        payload["race"].update(
            going="良",
            weather="晴",
            age_condition="3歳",
            sex_condition="牡牝",
            weight_condition="馬齢",
            race_info_captured_at="2026-01-01T14:30:00+09:00",
            odds_captured_at="2026-01-01T15:00:00+09:00",
            quinella_odds={"official_datetime": "2026-01-01T14:59:00+09:00"},
        )
        template = build_environment(ROOT).get_template("race.html.j2")
        soup = BeautifulSoup(
            template.render(**build_race_context(payload), page_kind="prediction"),
            "html.parser",
        )

        grid = soup.select_one(".race-meta-grid")
        cells = grid.find_all("div", recursive=False)
        self.assertEqual(len(cells), 10)
        self.assertEqual(len(grid.select(".race-meta-spacer")), 2)
        self.assertEqual(
            [cell.strong.get_text(strip=True) for cell in cells if cell.strong],
            ["日付", "発走", "コース", "馬場", "天候", "頭数", "条件", "負担重量"],
        )
        self.assertIn("2026-01-01 14:30", soup.select_one(".race-info-time").get_text())

        general = soup.select_one('.prediction-method-content[data-ai-method$="general"]')
        statistical = soup.select_one('.prediction-method-content[data-ai-method$="statistical"]')
        self.assertEqual(
            general.select_one(".used-odds").get_text(" ", strip=True),
            "使用オッズ: 14:59時点（単勝オッズ・人気はこの時点の値です）",
        )
        self.assertNotIn("使用オッズ", statistical.get_text())
        self.assertNotIn("現在のオッズとは異なる場合があります。", soup.get_text())

        payload["race"]["quinella_odds"] = {}
        soup = BeautifulSoup(
            template.render(**build_race_context(payload), page_kind="prediction"),
            "html.parser",
        )
        self.assertEqual(
            soup.select_one(".used-odds").get_text(" ", strip=True),
            "使用オッズ: 15:00取得（単勝オッズ・人気は取得時点の値です）",
        )

        payload["race"]["odds_captured_at"] = "2026-01-01T15:31:00+09:00"
        soup = BeautifulSoup(
            template.render(**build_race_context(payload), page_kind="prediction"),
            "html.parser",
        )
        self.assertEqual(len(soup.select(".used-odds")), 1)
        self.assertIn(
            "発走後に取得されたオッズスナップショット",
            soup.select_one(".used-odds").get_text(" ", strip=True),
        )

    def test_race_title_escapes_race_name(self) -> None:
        payload = make_payload(
            predicted=True,
            track="中山",
            date="2026-01-01",
            name="A&B AI予想",
        )
        rendered = build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)
        )
        title = BeautifulSoup(rendered, "html.parser").title.get_text(strip=True)

        self.assertIn("A&B", title)

    def test_race_pages_show_saved_prediction_model_badge(self) -> None:
        payload = make_payload(
            predicted=True,
            track="中山",
            date="2026-01-01",
            name="検証レース",
        )
        payload["prediction"]["model_name"] = "saved-model"
        template = build_environment(ROOT).get_template("race.html.j2")

        for page_kind, status in (("prediction", "予想公開"), ("result", "結果公開")):
            with self.subTest(page_kind=page_kind):
                context = {
                    **build_race_context(payload),
                    "page_kind": page_kind,
                    "status_label": status,
                }
                rendered = template.render(**context)
                badges = BeautifulSoup(rendered, "html.parser").select_one(".page-badges")
                self.assertEqual(badges.select_one(".ai-badge").get_text(strip=True), "saved-model")
                self.assertEqual(badges.select_one(".status").get_text(strip=True), status)

    def test_expected_value_rows_use_raw_values_sort_and_handle_missing_odds(self) -> None:
        horse_rows = [
            {"horse_number": 3, "horse_name": "Horse 3", "win_odds": 5.0, "prediction": {"win_probability": 0.2}},
            {"horse_number": 1, "horse_name": "Horse 1", "win_odds": 3.0, "prediction": {"win_probability": 0.3333334}},
            {"horse_number": 4, "horse_name": "Horse 4", "win_odds": None, "prediction": {"win_probability": 0.1}},
            {"horse_number": 2, "horse_name": "Horse 2", "win_odds": 4.0, "prediction": {"win_probability": 0.25}},
        ]
        payload = {
            "horses": [
                {
                    "horse_number": horse["horse_number"],
                    "horse_name": horse["horse_name"],
                    "win_odds": horse["win_odds"],
                }
                for horse in horse_rows
            ],
            "prediction": {
                "horses": [
                    {
                        "horse_number": horse["horse_number"],
                        "win_probability": horse["prediction"]["win_probability"],
                    }
                    for horse in horse_rows
                ]
            },
        }
        value_pre = {
            "budget": 3000,
            "stake_unit": 100,
            "settings": {"ev_threshold": 1.0, "kelly_fraction": 0.5},
            "selections": [],
        }
        rows = build_expected_value_rows(
            payload,
            horse_rows,
            value_pre,
        )

        self.assertEqual([row["horse_number"] for row in rows], [1, 2, 3, 4])
        self.assertEqual([row["ev_rank"] for row in rows], [1, 2, 3, None])
        self.assertEqual(rows[0]["expected_value"], 0.3333334 * 3.0)
        self.assertEqual([row["meets_threshold"] for row in rows], [True, True, True, None])

    def test_value_selection_rows_derive_expected_return_without_mutating_simulation(self) -> None:
        value_pre = {
            "selections": [
                {
                    "horse_number": 3,
                    "stake": 200,
                    "expected_value": 1.234567,
                }
            ]
        }

        rows = build_value_selection_rows(value_pre)

        self.assertEqual(rows[0]["expected_return"], 246.9134)
        self.assertNotIn("expected_return", value_pre["selections"][0])

    def test_rank_comparison_preserves_direction_and_ignores_non_numeric_finish(self) -> None:
        upward = rank_comparison(3, 1)
        downward = rank_comparison(2, 10)
        same = rank_comparison(5, 5)
        unavailable = rank_comparison(2, "中止")

        self.assertIn("2", upward[0])
        self.assertIn("8", downward[0])
        self.assertNotEqual(same[0], unavailable[0])

    def test_prediction_rank_ties_use_horse_number_without_reordering_rows(self) -> None:
        context = build_race_context(
            make_payload(predicted=True, track="中山", date="2026-01-01", name="検証レース")
        )

        self.assertEqual([row["horse_number"] for row in context["horse_rows"]], [1, 2, 3])
        self.assertEqual([row["prediction_rank"] for row in context["horse_rows"]], [1, 2, 3])

    def test_method_panels_keep_saved_predictions_and_custom_data_separate(self):
        payload = ensure_race_payload(simulation_payload(DUTCHING_ROWS))
        entry = payload["prediction"][0]
        entry["statistical"] = copy.deepcopy(entry["general"])
        for horse, probability in zip(entry["statistical"]["horses"], [.10, .15, .20, .25, .30]):
            horse["win_probability"] = probability
        template = build_environment(ROOT).get_template("race.html.j2")
        for methods in (("general", "statistical"), ("statistical",)):
            with self.subTest(methods=methods):
                current = copy.deepcopy(payload)
                for method in set(("general", "statistical")) - set(methods):
                    del current["prediction"][0][method]
                current["simulation"] = calculate_pre_simulation(current, simulation_config(budget=1000))
                soup = BeautifulSoup(template.render(**build_race_context(current), page_kind="prediction"), "html.parser")
                embedded = json.loads(soup.select_one("#custom-simulator-data").string)
                self.assertEqual(set(embedded["methods"]), set(methods))
                for method in methods:
                    expected = [h["win_probability"] for h in entry[method]["horses"]]
                    panel = soup.select_one(f"#prediction-{method}")
                    column = 5 if method == "general" else 3
                    rows = panel.select("tbody tr")
                    self.assertEqual([float(r.select("td")[column]["data-sort-value"]) for r in rows], expected)
                    self.assertEqual([h["win_probability"] for h in embedded["methods"][method]["horses"]], expected)
                current["result"] = make_result(3, 500, [1, 2, 3, 4, 5])
                current["evaluation"] = build_evaluation(current)
                result = BeautifulSoup(template.render(**build_race_context(current), page_kind="result"), "html.parser")
                for method in methods:
                    rows = result.select(f"#result-{method} table.result-table tbody tr")
                    self.assertEqual([float(r.select("td")[3]["data-sort-value"]) for r in rows],
                                     [h["win_probability"] for h in entry[method]["horses"]])
                self.assertEqual(len(result.select("table.result-table")), len(methods))

    @unittest.skipUnless(shutil.which("node"), "Node.js required for tab scope test")
    def test_ai_and_ticket_panel_switching_is_scoped(self) -> None:
        template = (ROOT / "templates" / "race.html.j2").read_text(encoding="utf-8")
        start = template.index("function directScopedPanels")
        controller = template[start:template.index("(() => {", start)]
        script = controller + r'''
const makeTab = (key, value) => ({
  dataset: {[key]: value},
  attributes: {},
  tabIndex: 0,
  setAttribute(name, setting) { this.attributes[name] = String(setting); },
});
const makePanel = (selector, key, value, hidden) => ({
  dataset: {[key]: value},
  hidden,
  matches(candidate) { return candidate === selector; },
});
const makeList = (tabs, panels) => ({
  parentElement: {children: panels},
  querySelectorAll() { return tabs; },
});

const aiPanelsA = [
  makePanel("[data-ai-panel]", "aiMethod", "general", false),
  makePanel("[data-ai-panel]", "aiMethod", "statistical", true),
];
const aiPanelsB = [
  makePanel("[data-ai-panel]", "aiMethod", "general", false),
  makePanel("[data-ai-panel]", "aiMethod", "statistical", true),
];
const aiTabsA = [makeTab("aiMethod", "general"), makeTab("aiMethod", "statistical")];
activateAiMethodTabList(makeList(aiTabsA, aiPanelsA), "statistical");

const ticketPanelsA = [
  makePanel("[data-ticket-panel]", "ticketPanel", "win", false),
  makePanel("[data-ticket-panel]", "ticketPanel", "quinella", true),
];
const ticketPanelsB = [
  makePanel("[data-ticket-panel]", "ticketPanel", "win", false),
  makePanel("[data-ticket-panel]", "ticketPanel", "quinella", true),
];
const ticketTabsA = [makeTab("ticketTab", "win"), makeTab("ticketTab", "quinella")];
activateTicketTabList(makeList(ticketTabsA, ticketPanelsA), "quinella");

process.stdout.write(JSON.stringify({
  aiA: aiPanelsA.map((panel) => panel.hidden),
  aiB: aiPanelsB.map((panel) => panel.hidden),
  ticketA: ticketPanelsA.map((panel) => panel.hidden),
  ticketB: ticketPanelsB.map((panel) => panel.hidden),
}));
'''
        result = subprocess.run(
            ["node", "-e", script],
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(
            json.loads(result.stdout),
            {
                "aiA": [True, False],
                "aiB": [False, True],
                "ticketA": [True, False],
                "ticketB": [False, True],
            },
        )

    def test_render_only_prediction_races_and_remove_stale_managed_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_dir = root / "data" / "races"
            predicted_path = race_dir / "2026-01-01" / "nakayama_11r.json"
            pending_path = race_dir / "2026-01-02" / "tokyo_11r.json"
            predicted_path.parent.mkdir(parents=True)
            pending_path.parent.mkdir(parents=True)
            predicted_path.write_text(
                json.dumps(
                    make_payload(predicted=True, track="中山", date="2026-01-01", name="予想済み"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            pending_path.write_text(
                json.dumps(
                    make_payload(predicted=False, track="東京", date="2026-01-02", name="未予想"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            public = root / "public"
            stale = public / "races" / "2025-12-31" / "stale_11r.html"
            asset = public / "assets" / "site.css"
            stale.parent.mkdir(parents=True)
            asset.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            asset.write_text("body {}", encoding="utf-8")

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-render",
                root=root,
            )

            index_soup = BeautifulSoup(
                (output / "index.html").read_text(encoding="utf-8"),
                "html.parser",
            )
            race_soup = BeautifulSoup(
                (output / "races" / "2026-01-01" / "nakayama_11r.html").read_text(
                    encoding="utf-8"
                ),
                "html.parser",
            )
            table = index_soup.select_one("table.index-table")
            rows = table.select("tbody tr")
            self.assertEqual(len(rows), 1)
            self.assertIn("予想済み", rows[0].get_text(" ", strip=True))
            self.assertNotIn("未予想", table.get_text(" ", strip=True))
            self.assertEqual(
                [link["href"] for link in rows[0].select("a[href]")],
                ["races/2026-01-01/nakayama_11r.html"],
            )
            self.assertIsNone(rows[0].select("td")[-1].find("a"))
            self.assertTrue((output / "assets" / "site.css").exists())
            self.assertFalse((output / stale.relative_to(public)).exists())
            self.assertFalse((output / "races" / "2026-01-02" / "tokyo_11r.html").exists())
            self.assertFalse((output / "races" / "2026-01-01" / "nakayama_11r_result.html").exists())
            self.assertEqual(
                [
                    panel["data-ai-method"]
                    for panel in race_soup.select(".prediction-section [data-ai-panel]")
                ],
                ["general"],
            )
            self.assertIsNone(race_soup.select_one(".result-section"))

    def test_result_race_generates_separate_prediction_and_result_pages_and_index_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_path = root / "data" / "races" / "2026-01-01" / "nakayama_11r.json"
            race_path.parent.mkdir(parents=True)
            payload = make_payload(
                predicted=True,
                track="中山",
                date="2026-01-01",
                name="結果確認レース",
            )
            payload["result"] = {
                "finish_order": [2, 1, 3],
                "horses": [
                    {"horse_number": 2, "finish_position": 1},
                    {"horse_number": 1, "finish_position": 2},
                    {"horse_number": 3, "finish_position": 3},
                ],
                "payouts": {"win": [{"horse_number": 2, "payout_per_100": 500}]},
                "final_win_odds": [
                    {"horse_number": 1, "win_odds": 4.4},
                    {"horse_number": 2, "win_odds": 5.0},
                    {"horse_number": 3, "win_odds": 6.2},
                ],
            }
            payload["simulation"]["value"]["post"] = {
                "total_stake": 100,
                "total_return": 500,
                "profit": 400,
                "roi": 5.0,
                "selections": [
                    {"horse_number": 2, "stake": 100, "hit": True, "return": 500}
                ],
            }
            payload["evaluation"] = {
                "winner": {"horse_number": 2, "predicted_probability": 0.4, "predicted_rank": 2},
                "metrics": {
                    "top1_hit": False,
                    "top3_hit": True,
                    "top5_hit": True,
                    "log_loss": 0.916291,
                    "brier_score": 0.24,
                },
                "market_baseline": {"available": False},
                "simulation_results": {
                    "value": {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None},
                    "dutching": {"total_stake": 0, "total_return": 0, "profit": 0, "roi": None},
                },
            }
            race_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-render-result",
                root=root,
            )
            prediction_odds_before = [horse["win_odds"] for horse in payload["horses"]]
            prediction_html = (output / "races" / "2026-01-01" / "nakayama_11r.html").read_text(encoding="utf-8")
            result_html = (output / "races" / "2026-01-01" / "nakayama_11r_result.html").read_text(encoding="utf-8")
            index = (output / "index.html").read_text(encoding="utf-8")
            prediction_soup = BeautifulSoup(prediction_html, "html.parser")
            result_soup = BeautifulSoup(result_html, "html.parser")
            index_soup = BeautifulSoup(index, "html.parser")

            self.assertIsNotNone(prediction_soup.select_one(".prediction-section"))
            self.assertIsNotNone(prediction_soup.select_one(".simulation-section"))
            self.assertIsNone(prediction_soup.select_one(".result-section"))
            self.assertIsNotNone(result_soup.select_one(".result-section"))
            self.assertEqual(result_soup.select_one(".status").get_text(strip=True), "結果公開")
            self.assertIsNotNone(
                result_soup.select_one('table.result-table td[data-sort-value="5.0"]')
            )
            self.assertEqual(
                [horse["win_odds"] for horse in payload["horses"]],
                prediction_odds_before,
            )
            self.assertIsNone(result_soup.select_one("table.prediction-table"))
            self.assertIsNone(result_soup.select_one("#custom-simulator"))
            row = index_soup.select_one("table.index-table tbody tr")
            self.assertEqual(
                [link["href"] for link in row.select("a[href]")],
                [
                    "races/2026-01-01/nakayama_11r.html",
                    "races/2026-01-01/nakayama_11r_result.html",
                ],
            )

    def test_index_renders_generated_evaluation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "templates", root / "templates")
            race_path = root / "data" / "races" / "2026-01-01" / "nakayama_11r.json"
            race_path.parent.mkdir(parents=True)
            race_payload = make_payload(predicted=True, track="中山", date="2026-01-01", name="予想済み")
            race_payload["race"].update({"class_grade": "G2", "surface": "芝", "distance": 2400})
            race_path.write_text(
                json.dumps(
                    race_payload,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary_path = root / "data" / "evaluation_summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "overall": {
                            "evaluated_races": 5,
                            "top1_hits": 1,
                            "top1_hit_rate": 0.2,
                            "top3_hits": 3,
                            "top3_hit_rate": 0.6,
                            "top5_hits": 4,
                            "top5_hit_rate": 0.8,
                            "average_winner_predicted_rank": 3.4,
                        },
                        "simulation": {
                            "value": {"simulation_races": 5, "cumulative_profit": 0},
                            "dutching": {"simulation_races": 5, "cumulative_profit": -9670},
                        },
                        "methods": {
                            "general": {
                                "overall": {
                                    "evaluated_races": 5,
                                    "top1_hits": 1,
                                    "top1_hit_rate": 0.2,
                                    "top3_hits": 3,
                                    "top3_hit_rate": 0.6,
                                    "top5_hits": 4,
                                    "top5_hit_rate": 0.8,
                                    "average_winner_predicted_rank": 3.4,
                                },
                                "simulation": {
                                    "value": {
                                        "simulation_races": 5,
                                        "purchase_races": 2,
                                        "cumulative_profit": 300,
                                        "overall_roi": 1.234,
                                    },
                                    "dutching": {
                                        "simulation_races": 5,
                                        "purchase_races": 4,
                                        "cumulative_profit": -9670,
                                        "overall_roi": 0.679,
                                    },
                                    "quinella": {
                                        "value": {
                                            "simulation_races": 14,
                                            "purchase_races": 0,
                                            "cumulative_profit": 0,
                                            "overall_roi": None,
                                        },
                                        "dutching": {
                                            "simulation_races": 14,
                                            "purchase_races": 12,
                                            "cumulative_profit": 690,
                                            "overall_roi": 1.074,
                                        },
                                    },
                                },
                            },
                            "statistical": {
                                "overall": {
                                    "evaluated_races": 2,
                                    "top1_hits": 1,
                                    "top1_hit_rate": 0.5,
                                    "top3_hits": 2,
                                    "top3_hit_rate": 1.0,
                                    "top5_hits": 2,
                                    "top5_hit_rate": 1.0,
                                    "average_winner_predicted_rank": 1.5,
                                },
                                "simulation": {
                                    "value": {
                                        "simulation_races": 1,
                                        "purchase_races": 1,
                                        "cumulative_profit": 300,
                                        "overall_roi": 1.5,
                                    },
                                    "dutching": {
                                        "simulation_races": 1,
                                        "purchase_races": 1,
                                        "cumulative_profit": -100,
                                        "overall_roi": 0.9,
                                    },
                                },
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = render_site(
                {"data_dir": "data", "public_dir": "public"},
                "test-render-summary",
                root=root,
            )
            soup = BeautifulSoup(
                (output / "index.html").read_text(encoding="utf-8"),
                "html.parser",
            )
            performance_panels = soup.select(".performance-panel")

            self.assertEqual(len(performance_panels), 2)
            self.assertTrue(
                all(len(panel.select(".profit-amount")) == 4 for panel in performance_panels)
            )
            general_profit_lines = performance_panels[0].select(".profit-line")
            general_profit_amounts = [
                line.select_one(".profit-amount") for line in general_profit_lines
            ]
            self.assertEqual(
                [amount.get_text(strip=True) for amount in general_profit_amounts],
                ["-9,670円", "+300円", "+690円", "0円"],
            )
            self.assertIn("profit-negative", general_profit_amounts[0].get("class", []))
            self.assertTrue(
                all(
                    "profit-positive" in amount.get("class", [])
                    for amount in general_profit_amounts[1:3]
                )
            )
            self.assertIn("profit-neutral", general_profit_amounts[3].get("class", []))
            self.assertEqual(
                [line.select_one(".profit-meta").get_text(" ", strip=True) for line in general_profit_lines],
                [
                    "回収率 67.9% ・ 購入 4 / 5レース",
                    "回収率 123.4% ・ 購入 2 / 5レース",
                    "回収率 107.4% ・ 購入 12 / 14レース",
                    "回収率 - ・ 購入 0 / 14レース",
                ],
            )
            self.assertTrue(
                all(
                    value.get_text(strip=True) != "-"
                    for panel in performance_panels
                    for value in panel.select(".performance-value")
                )
            )
            index_table = soup.select_one("table.index-table")
            self.assertIsNotNone(index_table)
            self.assertEqual(
                [header.get_text(strip=True) for header in index_table.select("thead th")],
                ["日付", "発走", "開催場", "レース名", "概要", "状態", "予想", "結果"],
            )
            row_cells = index_table.select_one("tbody tr").find_all("td", recursive=False)
            self.assertEqual(len(row_cells), 8)
            self.assertIn("race-name-column", row_cells[3].get("class", []))
            self.assertEqual(
                row_cells[3].select_one(".mobile-race-condition").get_text(" ", strip=True),
                "G2 芝2400m",
            )
            self.assertEqual(row_cells[4].get_text(" ", strip=True), "G2 芝2400m")
            self.assertIn("condition-column", row_cells[4].get("class", []))
            self.assertEqual(row_cells[4].select_one(".race-grade").get_text(strip=True), "G2")
            self.assertEqual(row_cells[4].select_one(".race-course").get_text(strip=True), "芝2400m")
            self.assertIn("status-column", index_table.select("thead th")[5].get("class", []))
            self.assertIn("status-column", row_cells[5].get("class", []))
            self.assertEqual(row_cells[6].select_one(".mobile-link-label").get_text(strip=True), "予想")
            self.assertEqual(row_cells[7].select_one(".mobile-link-label").get_text(strip=True), "結果")


class SimulationRenderTests(unittest.TestCase):
    def full_payload(self) -> dict:
        payload = simulation_payload(DUTCHING_ROWS)
        payload = ensure_race_payload(payload)
        payload["simulation"] = calculate_pre_simulation(payload, simulation_config(budget=1000))
        payload["result"] = make_result(1, 400, [1, 2, 3, 4, 5])
        payload["simulation"][0]["general"]["win"]["value"]["post"] = calculate_value_post(payload)
        payload["simulation"][0]["general"]["win"]["dutching"]["post"] = calculate_dutching_post(payload)
        payload["evaluation"] = build_evaluation(payload)
        return payload

    def render_page(self, payload: dict, page_kind: str = "prediction") -> str:
        context = build_race_context(payload)
        status = "prediction" if page_kind == "prediction" else "result"
        context.update(
            {
                "page_kind": page_kind,
                "prediction_page_name": "test_11r.html",
                "result_page_name": "test_11r_result.html",
                "status_label": "予想公開" if status == "prediction" else "結果公開",
                "status_class": f"status-{status}",
            }
        )
        return build_environment(ROOT).get_template("race.html.j2").render(**context)

    def test_prediction_and_result_pages_expose_required_sections_and_custom_data(self) -> None:
        payload = self.full_payload()
        rendered = self.render_page(payload)
        result_rendered = self.render_page(payload, "result")
        soup = BeautifulSoup(rendered, "html.parser")
        result_soup = BeautifulSoup(result_rendered, "html.parser")

        self.assertIsNotNone(soup.select_one(".prediction-section"))
        self.assertIsNotNone(soup.select_one(".simulation-section"))
        self.assertIsNotNone(soup.select_one("#custom-simulator"))
        self.assertIsNone(soup.select_one(".result-section"))
        self.assertIsNotNone(result_soup.select_one(".result-section"))
        self.assertIsNone(result_soup.select_one("#custom-simulator"))

        simulation_section = soup.select_one("section.simulation-section")
        simulation_panels = simulation_section.select_one('[data-ticket-panel="win"]').find_all(
            "div",
            class_="simulation-panel",
            recursive=False,
        )
        self.assertEqual(len(simulation_panels), 2)
        self.assertIsNone(simulation_section.select_one("#custom-simulator"))
        self.assertIsNone(
            soup.select_one("#custom-simulator").find_parent("section", class_="simulation-section")
        )


        custom = soup.select_one("#custom-simulator")
        method_options = custom.select('select[name="method"] option')
        self.assertEqual([option["value"] for option in method_options], ["dutching", "value"])
        self.assertTrue(method_options[0].has_attr("selected"))
        self.assertEqual(
            int(custom.select_one('input[name="budget"]')["value"]),
            payload["simulation"][0]["general"]["win"]["value"]["pre"]["budget"],
        )
        self.assertEqual(
            float(custom.select_one('input[name="ev_threshold"]')["value"]),
            payload["simulation"][0]["general"]["win"]["value"]["pre"]["settings"]["ev_threshold"],
        )
        self.assertEqual(
            float(custom.select_one('input[name="min_profit_rate"]')["value"]) / 100,
            payload["simulation"][0]["general"]["win"]["dutching"]["pre"]["settings"]["min_profit_rate"],
        )

        self.assertEqual(float(custom.select_one("#custom-kelly-fraction")["value"]),
                         payload["simulation"][0]["general"]["win"]["value"]["pre"]["settings"]["kelly_fraction"])

        embedded = json.loads(soup.select_one("#custom-simulator-data").string)
        self.assertEqual(set(embedded), {"stake_unit", "horses", "methods", "display"})
        self.assertTrue(all(set(item) == {"horse_number", "win_probability", "win_odds"} for item in embedded["horses"]))
        self.assertEqual(set(embedded["methods"]), {"general"})
        self.assertIsNotNone(custom.select_one("#custom-simulator-empty-reason"))

    def test_only_prediction_and_result_tables_are_sortable_with_raw_values(self) -> None:
        payload = self.full_payload()
        rendered = self.render_page(payload)
        result_rendered = self.render_page(payload, "result")
        soup = BeautifulSoup(rendered, "html.parser")
        result_soup = BeautifulSoup(result_rendered, "html.parser")
        sortable_tables = soup.select("table[data-sortable]") + result_soup.select("table[data-sortable]")

        self.assertEqual(len(sortable_tables), 2)
        self.assertFalse(
            any(
                table.has_attr("data-sortable")
                for table in soup.select(
                    ".simulation-table, .evaluation-table, .value-detail-table"
                )
            )
        )

        def header_specs(table):
            return [
                (
                    button["data-sort-type"],
                    button["data-sort-first"],
                    int(button["data-sort-column"]),
                    button.parent["aria-sort"],
                )
                for button in table.select("thead .sort-button")
            ]

        prediction_table, result_table = sortable_tables
        self.assertEqual(
            header_specs(prediction_table),
            [
                ("number", "ascending", 0, "none"),
                ("text", "ascending", 1, "none"),
                ("text", "ascending", 2, "none"),
                ("number", "ascending", 3, "none"),
                ("number", "ascending", 4, "none"),
                ("number", "descending", 5, "none"),
                ("number", "ascending", 6, "none"),
            ],
        )
        self.assertEqual(
            header_specs(result_table),
            [
                ("number", "ascending", 0, "none"),
                ("text", "ascending", 1, "none"),
                ("number", "ascending", 2, "none"),
                ("number", "descending", 3, "none"),
                ("number", "ascending", 4, "none"),
                ("number", "ascending", 5, "none"),
                ("number", "ascending", 6, "none"),
                ("number", "ascending", 7, "none"),
                ("number", "descending", 8, "none"),
            ],
        )
        self.assertIsNone(prediction_table.select("thead th")[-1].find("button"))
        self.assertTrue(
            all(button.get("type") == "button" for button in soup.select(".sort-button"))
        )

        prediction_first = prediction_table.select_one("tbody tr")
        self.assertEqual(
            [cell.get("data-sort-value") for cell in prediction_first.select("td")],
            ["1", None, None, "4.0", "1", "0.3", "1", None],
        )
        result_first = result_table.select_one("tbody tr")
        self.assertEqual(
            [cell.get("data-sort-value") for cell in result_first.select("td")],
            ["1", None, "1", "0.3", "1", "1", "0", "", "400"],
        )

    def test_all_horse_expected_values_are_rendered_without_changing_simulation(self) -> None:
        payload = simulation_payload(
            [(1, 0.02, 60.0), (2, 0.39, 3.0), (3, 0.2, 4.0), (4, 0.1, None)]
        )
        payload = ensure_race_payload(payload)
        payload["simulation"] = calculate_pre_simulation(payload, simulation_config())
        simulation_before = copy.deepcopy(payload["simulation"])
        original_rows = build_race_context(payload)["expected_value_rows"]
        legacy = copy.deepcopy(payload)
        del legacy["simulation"][0]["general"]["win"]["value"]["pre"]["details"]
        self.assertEqual(build_race_context(legacy)["expected_value_rows"], original_rows)
        for horse in payload["horses"][:3]:
            horse["win_odds"] = 1.1
        for horse in payload["prediction"][0]["general"]["horses"][:3]:
            horse["win_probability"] = .01
        self.assertEqual(build_race_context(payload)["expected_value_rows"], original_rows)

        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        soup = BeautifulSoup(rendered, "html.parser")
        table_rows = soup.select("table.value-detail-table tbody tr")

        self.assertEqual(len(table_rows), len(payload["horses"]))
        self.assertEqual(
            [int(row.select("td")[1].get_text(strip=True)) for row in table_rows],
            [1, 2, 3, 4],
        )
        self.assertEqual(payload["simulation"], simulation_before)

    def test_result_table_compares_saved_prediction_and_finish_without_purchase_influence(self):
        for winner in (1, 2):
            with self.subTest(winner=winner):
                payload = simulation_payload([(1, .40, 3.0), (2, .35, 4.0), (3, .25, 5.0)])
                payload["simulation"]["value"]["pre"] = {
                    "budget": 3000, "stake_unit": 100,
                    "settings": {"ev_threshold": 1.0, "kelly_fraction": .5},
                    "selections": [{"horse_number": 3, "stake": 100}],
                }
                payload["result"] = make_result(winner, 500, [1, 2, 3])
                soup = BeautifulSoup(self.render_page(payload, "result"), "html.parser")
                rows = soup.select("table.result-table tbody tr")
                finishes = {h["horse_number"]: h["finish_position"] for h in payload["result"]["horses"]}
                for number, row in enumerate(rows, 1):
                    values = [c.get("data-sort-value") for c in row.select("td")]
                    self.assertEqual(values[0], str(number))
                    self.assertEqual(float(values[3]), payload["prediction"]["horses"][number - 1]["win_probability"])
                    self.assertEqual(values[4:7], [str(number), str(finishes[number]), str(finishes[number] - number)])
                    self.assertEqual(values[8], "500" if number == winner else "")
                self.assertEqual(soup.select_one(".hit-badge") is not None, winner == 1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for table sorting")
    def test_sortable_table_javascript_cycles_stably_and_keeps_missing_last(self) -> None:
        template = (ROOT / "templates" / "race.html.j2").read_text(encoding="utf-8")
        start = template.index("    const initializeSortableTable")
        end = template.index("    (() => {", start)
        functions = template[start:end]
        node_script = functions + r"""
const makeCell = (text, sortValue) => ({
  textContent: text,
  dataset: sortValue === undefined ? {} : {sortValue: String(sortValue)}
});
const makeRow = (id, number, probability, name, className, reason) => ({
  id,
  className,
  reason,
  dataset: {},
  cells: [
    makeCell(String(number), number),
    makeCell(probability === undefined ? "-" : `${probability * 100}%`, probability),
    makeCell(name)
  ]
});
const rows = [
  makeRow("two", 2, 0.4, "カ", "prediction-top", "reason two"),
  makeRow("ten", 10, 0.4, "ア", "", "reason ten"),
  makeRow("one", 1, undefined, "-", "", "reason one"),
  makeRow("three", 3, 0.6, "イ", "result-winner", "reason three")
];
const body = {
  rows,
  appendChild(row) {
    const index = this.rows.indexOf(row);
    if (index >= 0) this.rows.splice(index, 1);
    this.rows.push(row);
  }
};
const makeButton = (column, type, first) => {
  const indicator = {textContent: "↕"};
  const header = {
    attributes: {"aria-sort": "none"},
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; }
  };
  const button = {
    dataset: {sortColumn: String(column), sortType: type, sortFirst: first},
    closest() { return header; },
    querySelector() { return indicator; },
    addEventListener(typeName, handler) { if (typeName === "click") this.handler = handler; },
    click() { this.handler(); },
    header,
    indicator
  };
  return button;
};
const numberButton = makeButton(0, "number", "ascending");
const probabilityButton = makeButton(1, "number", "descending");
const nameButton = makeButton(2, "text", "ascending");
const buttons = [numberButton, probabilityButton, nameButton];
const table = {
  tBodies: [body],
  querySelectorAll() { return buttons; }
};
const ids = () => body.rows.map((row) => row.id);
initializeSortableTable(table);
const output = {initial: ids()};
numberButton.click();
output.numberAscending = ids();
numberButton.click();
output.numberDescending = ids();
numberButton.click();
output.numberRestored = ids();
probabilityButton.click();
output.probabilityDescending = ids();
probabilityButton.click();
output.probabilityAscending = ids();
nameButton.click();
output.nameAscending = ids();
output.switchedHeaders = {
  probability: probabilityButton.header.getAttribute("aria-sort"),
  name: nameButton.header.getAttribute("aria-sort")
};
nameButton.click();
output.nameDescending = ids();
nameButton.click();
output.nameRestored = ids();
output.preserved = {
  className: body.rows[0].className,
  reason: body.rows[0].reason
};
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run(
            [shutil.which("node"), "-"],
            input=node_script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["initial"], ["two", "ten", "one", "three"])
        self.assertEqual(result["numberAscending"], ["one", "two", "three", "ten"])
        self.assertEqual(result["numberDescending"], ["ten", "three", "two", "one"])
        self.assertEqual(result["numberRestored"], result["initial"])
        self.assertEqual(result["probabilityDescending"], ["three", "two", "ten", "one"])
        self.assertEqual(result["probabilityAscending"], ["two", "ten", "three", "one"])
        self.assertEqual(result["nameAscending"], ["ten", "three", "two", "one"])
        self.assertEqual(result["nameDescending"], ["two", "three", "ten", "one"])
        self.assertEqual(result["nameRestored"], result["initial"])
        self.assertEqual(
            result["switchedHeaders"],
            {
                "probability": "none",
                "name": "ascending",
            },
        )
        self.assertEqual(
            result["preserved"],
            {"className": "prediction-top", "reason": "reason two"},
        )


class QuinellaRenderTests(unittest.TestCase):
    def test_legacy_purchase_settings_render_without_mutation(self):
        payload = payload_with_odds()
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            payload["simulation"] = calculate_pre_simulation(payload, load_config())
        for method in ("general", "statistical"):
            for ticket in ("win", "quinella"):
                settings = payload["simulation"][0][method][ticket]["dutching"]["pre"]["settings"]
                settings["require_profit_if_hit"] = True
        payload["simulation"][0]["general"]["win"]["dutching"]["pre"]["settings"].pop("min_profit_rate")
        before = copy.deepcopy(payload)
        soup = BeautifulSoup(build_environment(ROOT).get_template("race.html.j2").render(
            **build_race_context(payload)), "html.parser")
        self.assertIsNotNone(soup.select_one('input[name="min_profit_rate"]'))
        self.assertIsNone(soup.select_one('input[name="require_profit_if_hit"]'))
        self.assertEqual(payload, before)

    def test_result_badges_use_hits_for_each_ai_ticket_and_method(self):
        template = build_environment(ROOT).get_template("race.html.j2")
        for case, hit, stake, refund in (("hit_with_loss", True, 1000, 0), ("miss", False, 1000, 0), ("refund", False, 1000, 1000), ("empty", False, 0, 0)):
            with self.subTest(case=case):
                payload = payload_with_odds()
                with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
                    payload["simulation"] = calculate_pre_simulation(payload, load_config())
                payload["result"] = parse_result(result_html())
                for simulation in [payload["simulation"][0]["general"], payload["simulation"][0]["statistical"]]:
                    for ticket in (simulation["win"], simulation["quinella"]):
                        for method in ("value", "dutching"):
                            ticket[method]["post"] = {
                                "total_stake": stake, "total_refund": refund, "total_return": refund,
                                "profit": refund - stake, "roi": -1 if stake and not refund else 0,
                                "selections": [{"horse_number": 1, "horse_numbers": [1, 2], "stake": stake, "hit": hit, "refund": refund, "payout": 0, "return": refund}] if stake else [],
                            }
                soup = BeautifulSoup(template.render(**build_race_context(payload), page_kind="result"), "html.parser")
                for ai in ("general", "statistical"):
                    for ticket in ("win", "quinella"):
                        panels = soup.select(f"#settlement-{ai}-{ticket} .result-panel")
                        self.assertEqual(len(panels), 2)
                        for panel in panels:
                            self.assertEqual(panel.select_one("h3 .hit-badge") is not None, hit)
                            if not stake:
                                self.assertIsNone(panel.find("table"))

    def test_weather_and_saved_popularity_on_both_ai_result_tables(self):
        payload = payload_with_odds()
        payload["race"]["weather"] = "晴"
        payload["result"] = parse_result(result_html())
        payload["result"]["weather"] = "雨"
        for horse, popularity in zip(payload["horses"], (10, 2, None)):
            horse["popularity"] = popularity
        payload["race"]["going"] = "pre-going"
        payload["result"].update(going="post-going", fetched_at="2026-08-30T07:10:11+00:00")
        before = copy.deepcopy(payload)
        template = build_environment(ROOT).get_template("race.html.j2")
        for kind, weather in (("prediction", "晴"), ("result", "雨")):
            soup = BeautifulSoup(template.render(**build_race_context(payload), page_kind=kind), "html.parser")
            self.assertIn(weather, soup.select_one(".meta").get_text(" ", strip=True))
            if kind == "result":
                basic_info = soup.select_one(".meta").get_text(" ", strip=True)
                self.assertIn("post-going", basic_info)
                self.assertNotIn("pre-going", basic_info)
                self.assertIn("2026-08-30 16:10:11", basic_info)
                for table in soup.select(".result-table"):
                    self.assertEqual([row.select("td")[2].get("data-sort-value") for row in table.select("tbody tr")], ["10", "2", ""])
                    header = table.select("thead .sort-button")[2]
                    self.assertEqual(header["data-sort-type"], "number")
                    self.assertEqual(header["data-sort-column"], "2")
        self.assertEqual(payload, before)
        payload["race"].pop("weather")
        soup = BeautifulSoup(template.render(**build_race_context(payload)), "html.parser")
        self.assertIn("-", soup.select_one(".meta").stripped_strings)

    def test_temp_render_ticket_panels_and_saved_probability_data(self):
        config, payload = load_config(), payload_with_odds()
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            payload["simulation"] = calculate_pre_simulation(payload, config)
        # Display and custom defaults must come from saved pre, not changed current config/odds.
        original = copy.deepcopy(payload)
        config["simulation"]["quinella"]["value"]["kelly_fraction"] = .1
        payload["race"]["quinella_odds"]["pairs"][0]["odds"] = 999
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "templates", root / "templates")
            path = root / config["data_dir"] / "races" / "2026-09-05" / "nakayama_11r.json"
            save_race_json(path, payload)
            stage = render_site(config, "test-quinella-render", root=root)
            pre_path = stage / "races/2026-09-05/nakayama_11r.html"
            soup = BeautifulSoup(pre_path.read_text(encoding="utf-8"), "html.parser")
            self.assertFalse(pre_path.with_name("nakayama_11r_result.html").exists())
            for ai in ("general", "statistical"):
                self.assertFalse(soup.select_one(f'#purchase-{ai}-win').has_attr("hidden"))
                self.assertTrue(soup.select_one(f'#purchase-{ai}-quinella').has_attr("hidden"))
                self.assertEqual(len(soup.select(f'#purchase-{ai}-quinella [data-quinella-method]')), 2)
            data = json.loads(soup.select_one("#custom-simulator-data").string)
            self.assertEqual(data["methods"]["general"]["quinella"]["value"]["settings"]["kelly_fraction"], original["simulation"][0]["general"]["quinella"]["value"]["pre"]["settings"]["kelly_fraction"])
            self.assertEqual(data["methods"]["general"]["quinella"]["pairs"][0]["odds"], original["race"]["quinella_odds"]["pairs"][0]["odds"])
            self.assertEqual([r["probability"] for r in data["methods"]["general"]["quinella"]["pairs"]], [r["probability"] for r in original["simulation"][0]["general"]["quinella"]["probabilities"]])
            payload["result"] = parse_result(result_html())
            for simulation in [payload["simulation"][0]["general"], payload["simulation"][0]["statistical"]]:
                for method in ("value", "dutching"):
                    simulation["win"][method]["post"] = calculate_post(simulation["win"][method]["pre"], payload["result"])
            payload["evaluation"] = build_evaluation(payload)
            save_race_json(path, payload)
            stored = path.read_bytes()
            stage = render_site(config, "test-quinella-result-render", root=root)
            result_soup = BeautifulSoup((stage / "races/2026-09-05/nakayama_11r_result.html").read_text(encoding="utf-8"), "html.parser")
            self.assertEqual(len(result_soup.select('[data-ticket-panel="quinella"] .quinella-pending')), 4)
            self.assertEqual(path.read_bytes(), stored)
            index = BeautifulSoup((stage / "index.html").read_text(encoding="utf-8"), "html.parser")
            self.assertIsNotNone(index.select_one('a[href$="nakayama_11r_result.html"]'))


if __name__ == "__main__":
    unittest.main()
