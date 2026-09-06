from __future__ import annotations

import base64
import copy
import json
import logging
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from itertools import combinations
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collect import collect_races, collect_results, fetch_validated_win_odds, parse_result
from evaluation import build_evaluation
from evaluation_summary import build_evaluation_summary
from predict import build_prediction_chat_input, build_statistical_prediction_input
from quinella import (
    calculate_quinella_post, calculate_quinella_pre, calculate_quinella_purchase,
    harville_probabilities, pair_numbers, validate_pair_odds,
)
from render import build_environment, build_race_context, render_site
from run_pre_collect import export_prediction_chat_input
from simulate import calculate_post, calculate_pre_simulation, calculate_value_details, simulate_file
from utils import ensure_race_payload, load_config, load_race_json, parse_jst_datetime, save_race_json
from test_simulation import make_payload

CAPTURED = "2026-09-05T12:00:00+09:00"


def payload_with_odds() -> dict:
    payload = make_payload([(1, .5, 3.0), (2, .3, 5.0), (3, .2, 8.0)])
    payload["race"].update(date="2026-09-05", start_time="15:30")
    payload["prediction"].update(method="traditional", model_provider="codex", model_name="test", predicted_at=CAPTURED)
    variant = copy.deepcopy(payload["prediction"])
    variant["method"] = "statistical"
    for horse, probability in zip(variant["horses"], [.2, .3, .5]):
        horse["win_probability"] = probability
    payload["prediction"]["variants"] = [variant]
    payload["race"]["quinella_odds"] = {
        "available": True, "reason": None, "fetched_at": CAPTURED,
        "source": "netkeiba", "source_url": "https://example.invalid/odds",
        "official_datetime": "2026-09-05 11:59:00", "api_status": "middle",
        "pairs": [
            {"horse_numbers": [1, 2], "odds": 2.5},
            {"horse_numbers": [1, 3], "odds": 5.0},
            {"horse_numbers": [2, 3], "odds": 8.0},
        ],
    }
    return payload


def result_html(positions=(1, 2, 3), pairs=((1, 2),), amounts=(700,), *, mobile=False) -> str:
    rows = ''.join(f'<tr><td>{position}</td><td>{number}</td><td>4.0</td></tr>' for number, position in enumerate(positions, 1))
    groups = ''.join(f'<ul><li><span>{a}</span></li><li><span>{b}</span></li><li>{"<br/>" if mobile else ""}</li></ul>' for a, b in pairs)
    amounts_html = '<br/>'.join(f'<span>{amount:,}円</span>' for amount in amounts)
    win_numbers = [number for number, position in enumerate(positions, 1) if position == 1]
    return f'''<table><thead><tr><th>着順</th><th>馬番</th><th>オッズ</th></tr></thead><tbody>{rows}</tbody></table>
    <table><tr><th>単勝</th><td>{' '.join(map(str, win_numbers))}</td><td>{' '.join('400円' for _ in win_numbers)}</td></tr>
    <tr class="Umaren"><th>馬連</th><td class="Result">{groups}</td><td class="Payout">{amounts_html}</td><td>1人気</td></tr></table>'''


class OddsTests(unittest.TestCase):
    def api(self, pairs=None, status="middle"):
        body = {"official_datetime": "2026-09-05 11:59:00", "odds": {
            "1": {str(n): [str(2+n), "0.0", n, str(n)] for n in (1, 2, 3)},
            "4": pairs if pairs is not None else {"99": ["2.5", "0.0", 1, "0201"], "1": ["5.0", "0.0", 2, "0103"], "2": ["8.0", "0.0", 3, "0203"]},
        }}
        session = Mock()
        session.get.return_value.text = json.dumps({"status": status, "reason": "", "update_count": "0", "data": base64.b64encode(zlib.compress(json.dumps(body).encode())).decode()})
        return session

    def fetch(self, session):
        race = payload_with_odds()["race"]
        with patch("collect.now_jst_iso", return_value=CAPTURED):
            win = fetch_validated_win_odds(session, "test", race, {1: "A", 2: "B", 3: "C"}, logging.getLogger("test"), "test")
        return win, race["quinella_odds"]

    def test_single_request_uses_pair_column_not_dictionary_keys(self):
        session = self.api()
        win, snapshot = self.fetch(session)
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.kwargs["params"]["type"], "all")
        self.assertEqual(set(win[0]), {1, 2, 3})
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["pairs"][0], {"horse_numbers": [1, 2], "odds": 2.5})
        self.assertEqual(snapshot["fetched_at"], CAPTURED)
        self.assertEqual(snapshot["api_status"], "middle")

    def test_bad_pairs_and_post_odds_do_not_disable_win(self):
        for pairs in ({}, {"1": ["2.0", 0, 1, "0101"]}, {"1": ["NaN", 0, 1, "0102"]}, {"1": [True, 0, 1, "0102"]}):
            with self.subTest(pairs=pairs):
                win, snapshot = self.fetch(self.api(pairs))
                self.assertEqual(len(win[0]), 3)
                self.assertFalse(snapshot["available"])
                self.assertEqual(snapshot["pairs"], [])
        win, snapshot = self.fetch(self.api(status="result"))
        self.assertEqual(len(win[0]), 3)
        self.assertEqual(snapshot["reason"], "odds_not_pre_race")

    def test_grouped_odds_are_numeric_and_can_generate_both_ai_pre(self):
        for text, expected in (("1,025.0", 1025.0), ("12,345.6", 12345.6),
                               ("1,234,567.8", 1234567.8), (" 1,025.0 ", 1025.0),
                               ("2.5", 2.5), (1025.0, 1025.0)):
            with self.subTest(odds=text):
                session = self.api({"1": [text, 0, 1, "0102"],
                                    "2": ["5.0", 0, 2, "0103"],
                                    "3": ["8.0", 0, 3, "0203"]})
                win, snapshot = self.fetch(session)
                session.get.assert_called_once()
                self.assertEqual(set(win[0]), {1, 2, 3})
                self.assertTrue(snapshot["available"])
                self.assertEqual(snapshot["pairs"][0]["odds"], expected)
                payload, config = payload_with_odds(), load_config()
                payload["race"]["quinella_odds"].update(available=False, pairs=[])
                with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
                    payload["simulation"] = calculate_pre_simulation(payload, config)
                    before = copy.deepcopy(payload)
                    payload["race"]["quinella_odds"] = snapshot
                    simulation = calculate_pre_simulation(payload, config)
                self.assertEqual(payload["prediction"], before["prediction"])
                self.assertEqual(payload["horses"], before["horses"])
                for new, old in zip((simulation, *simulation["variants"]),
                                    (before["simulation"], *before["simulation"]["variants"])):
                    self.assertEqual(old["quinella"]["status"], "unavailable")
                    self.assertEqual(new["quinella"]["status"], "ready")
                    self.assertEqual(new["quinella"]["odds_snapshot"], snapshot)
                    for method in ("value", "dutching"):
                        self.assertEqual(new[method], old[method])
                        self.assertIsNotNone(new["quinella"][method]["pre"])

    def test_invalid_odds_reject_the_entire_pair_snapshot(self):
        for odds in ("1,02.5", "1,,025.0", "1,025.0x", "", "---",
                     "NaN", "Infinity", 0, -1, True, None):
            with self.subTest(odds=odds):
                win, snapshot = self.fetch(self.api({
                    "1": ["2.5", 0, 1, "0102"],
                    "2": [odds, 0, 2, "0103"],
                    "3": ["8.0", 0, 3, "0203"],
                }))
                self.assertEqual(set(win[0]), {1, 2, 3})
                self.assertFalse(snapshot["available"])
                self.assertEqual(snapshot["pairs"], [])

    def test_pair_validation_checks_exact_set_and_finite_values(self):
        rows = payload_with_odds()["race"]["quinella_odds"]["pairs"]
        validate_pair_odds(rows, [1, 2, 3])
        self.assertEqual(pair_numbers([3, 1]), (1, 3))
        cases = [rows[:-1], rows + [rows[0]], [*rows[:-1], {"horse_numbers": [2, 4], "odds": 8}]]
        for invalid in (0, -1, float("nan"), float("inf"), True, None):
            cases.append([{**rows[0], "odds": invalid}, *rows[1:]])
        for invalid in ([1, 1], [0, 2], [True, 2], ["1", 2]):
            cases.append([{**rows[0], "horse_numbers": invalid}, *rows[1:]])
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_pair_odds(case, [1, 2, 3])


class ProbabilityAndPurchaseTests(unittest.TestCase):
    def test_harville_sum_lambda_one_and_no_win_odds_filter(self):
        payload = payload_with_odds()
        payload["horses"][0]["win_odds"] = None
        before = copy.deepcopy(payload)
        for exponent in (1.0, .81):
            rows = harville_probabilities(payload["horses"], payload["prediction"], exponent)
            self.assertAlmostEqual(sum(row["probability"] for row in rows), 1)
            self.assertEqual([r["horse_numbers"] for r in rows], [[1, 2], [1, 3], [2, 3]])
            if exponent == 1:
                self.assertAlmostEqual(rows[0]["probability"], .5*.3/(1-.5) + .3*.5/(1-.3))
        self.assertEqual(payload, before)

    def test_invalid_prediction_is_not_normalized_or_imputed(self):
        for values in ([1, 0, 0], [.5, .3, .1], [-.1, .6, .5], [float("nan"), .3, .2], [True, 0, 0]):
            payload = payload_with_odds()
            for horse, value in zip(payload["prediction"]["horses"], values):
                horse["win_probability"] = value
            with self.subTest(values=values), self.assertRaises(ValueError):
                harville_probabilities(payload["horses"], payload["prediction"], .81)
        payload = payload_with_odds()
        payload["prediction"]["horses"][0]["horse_number"] = 8
        with self.assertRaises(ValueError):
            harville_probabilities(payload["horses"], payload["prediction"], .81)

    def test_value_reuses_kelly_allocator_without_forced_unit(self):
        settings = {"ev_threshold": 1.10, "kelly_fraction": .8}
        rows = [{"horse_numbers": [1, 2], "probability": .4, "odds": 3.0}, {"horse_numbers": [1, 3], "probability": .01, "odds": 111.0}, {"horse_numbers": [2, 3], "probability": .59, "odds": 1.0}]
        result = calculate_quinella_purchase(rows, 3000, 100, settings, "value")
        first = next(r for r in result["details"] if r["horse_numbers"] == [1, 2])
        self.assertAlmostEqual(first["full_kelly"], .1)
        self.assertAlmostEqual(first["fractional_kelly"], .08)
        self.assertAlmostEqual(first["theoretical_stake"], 240)
        self.assertEqual(first["stake"], 200)
        self.assertEqual(result["total_stake"], 200)
        self.assertEqual(result["unused_budget"], 2800)
        self.assertEqual(len(result["details"]), 3)
        self.assertEqual(result["selections"][0]["horse_numbers"], [1, 2])
        with patch("simulate.calculate_value_details", wraps=calculate_value_details) as allocator:
            calculate_quinella_purchase(rows, 3000, 100, settings, "value")
        allocator.assert_called_once()

    def test_value_scales_only_when_total_raw_exceeds_budget(self):
        # Large coefficient deliberately exercises the shared allocator's scaling path.
        rows = [{"horse_numbers": list(pair), "probability": 1/3, "odds": 10.0} for pair in combinations([1, 2, 3], 2)]
        for coefficient, total in ((.8, 1800), (2.0, 3000)):
            result = calculate_quinella_purchase(rows, 3000, 100, {"ev_threshold": 1.1, "kelly_fraction": coefficient}, "value")
            self.assertEqual(result["total_stake"], total)
            self.assertEqual([r["stake"] for r in result["selections"]], [total//3]*3)

    def test_dutching_thresholds_and_numeric_pair_ties(self):
        rows = [{"horse_numbers": list(pair), "probability": .25, "odds": 8.0} for pair in [(2, 10), (1, 10), (2, 3), (1, 2)]]
        settings = load_config()["simulation"]["quinella"]["dutching"]
        result = calculate_quinella_purchase(rows, 3000, 100, settings, "dutching")
        self.assertEqual(result["selected_count"], 4)
        self.assertEqual([r["horse_numbers"] for r in result["selections"]], [[1, 2], [1, 10], [2, 3], [2, 10]])
        self.assertEqual([r["stake"] for r in result["selections"]], [800, 800, 700, 700])
        self.assertEqual(result["coverage_probability"], 1)
        self.assertEqual(result["group_expected_value"], 2)
        first = result["evaluated_counts"][0]
        self.assertIn("coverage_probability_below_threshold", first["rejection_reasons"])
        for key, value in (("min_coverage_probability", 1.1), ("min_group_expected_value", 3), ("min_profit_rate", 10)):
            excluded = calculate_quinella_purchase(rows, 3000, 100, {**settings, key: value}, "dutching")
            self.assertEqual(excluded["status"], "no_purchase")
            self.assertEqual(excluded["selections"], [])

    def test_dutching_all_counts_limit_and_no_candidate_renormalization(self):
        rows = [{"horse_numbers": list(pair), "probability": 1/21, "odds": 30.0} for pair in combinations(range(1, 8), 2)]
        settings = load_config()["simulation"]["quinella"]["dutching"]
        result = calculate_quinella_purchase(rows, 3050, 100, settings, "dutching")
        self.assertEqual(len(result["evaluated_counts"]), 10)
        self.assertAlmostEqual(result["evaluated_counts"][0]["coverage_probability"], 1/21)
        self.assertEqual(result["total_stake"], 3000)
        self.assertEqual(result["unused_budget"], 50)
        for r in result["selections"]:
            self.assertEqual(r["predicted_probability"], 1/21)
            self.assertEqual(r["stake"] % 100, 0)

    def test_minimum_profit_rate_uses_actual_stake_and_inclusive_boundary(self):
        settings = {**load_config()["simulation"]["quinella"]["dutching"], "min_group_expected_value": 0, "min_coverage_probability": 0}
        for odds, eligible in ((1.2, True), (1.199, False)):
            result = calculate_quinella_purchase([{"horse_numbers": [1, 2], "probability": 1, "odds": odds}], 3099, 100, settings, "dutching")
            self.assertEqual(result["evaluated_counts"][0]["eligible"], eligible)


class SettlementTests(unittest.TestCase):
    def test_desktop_and_mobile_normal_and_dead_heat_payouts(self):
        for mobile in (False, True):
            for positions, pairs, amounts in (((1, 2, 3), ((1, 2),), (700,)), ((1, 2, 2), ((1, 2), (1, 3)), (700, 500)), ((1, 1, 1), ((1, 2), (1, 3), (2, 3)), (200, 300, 400))):
                with self.subTest(mobile=mobile, pairs=pairs):
                    result = parse_result(result_html(positions, pairs, amounts, mobile=mobile))
                    self.assertEqual(result["quinella_settlement"]["status"], "complete")
                    self.assertEqual(result["payouts"]["quinella"], [{"horse_numbers": list(pair), "payout_per_100": amount} for pair, amount in zip(pairs, amounts)])

    def test_incomplete_duplicate_or_misaligned_payout_is_pending(self):
        for pairs, amounts in ((((1, 2),), (700,)), (((1, 2), (1, 3)), (700,)), (((1, 2), (2, 1)), (700, 500)), ((), ())):
            result = parse_result(result_html((1, 2, 2), pairs, amounts))
            self.assertEqual(result["quinella_settlement"]["status"], "pending")
            self.assertEqual(result["payouts"]["quinella"], [])
            self.assertTrue(result["payouts"]["win"])

    def test_post_multiple_hits_loss_and_refund_use_official_payouts(self):
        result = parse_result(result_html((1, 2, 2, "取消", "除外", "中止", "失格"), ((1, 2), (1, 3)), (700, 500)))
        pre = {"status": "purchased", "total_stake": 700, "selections": [{"horse_numbers": list(pair), "stake": 100, "odds": 9999} for pair in ((1, 2), (1, 3), (2, 3), (1, 4), (1, 5), (1, 6), (1, 7))]}
        before = copy.deepcopy((pre, result))
        result["finish_order"] = list(reversed(result["finish_order"]))
        post = calculate_quinella_post(pre, result)
        self.assertEqual(post["total_stake"], 700)
        self.assertEqual(post["total_return"], 1400)
        self.assertEqual(post["total_refund"], 200)
        self.assertEqual(post["profit"], 700)
        self.assertEqual(post["roi"], 1)
        self.assertEqual([r["hit"] for r in post["selections"]], [True, True, False, False, False, False, False])
        self.assertEqual([r["refund"] for r in post["selections"]], [0, 0, 0, 100, 100, 0, 0])
        self.assertEqual(pre, before[0])

    def test_no_purchase_is_settled_but_unavailable_and_missing_payout_are_not(self):
        result = parse_result(result_html())
        pre = {"status": "no_purchase", "total_stake": 0, "selections": []}
        post = calculate_quinella_post(pre, result)
        self.assertEqual((post["total_stake"], post["profit"], post["roi"]), (0, 0, 0))
        self.assertIsNone(calculate_quinella_post(None, result))
        result["payouts"]["quinella"] = []
        self.assertIsNone(calculate_quinella_post(pre, result))

    @patch("collect.setup_logger", return_value=logging.getLogger("test-quinella-collect"))
    def test_collect_retry_preserves_previous_complete_settlement(self, _logger):
        config = load_config()
        payload = payload_with_odds()
        payload["result"] = parse_result(result_html())
        old = copy.deepcopy(payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "race.json"
            save_race_json(path, payload)
            with patch("collect.fetch_html", return_value=result_html(pairs=(), amounts=())):
                self.assertEqual(collect_results(config, "test-quinella-collect", [path], root), [path])
            saved = load_race_json(path)
        self.assertEqual(saved["result"]["quinella_settlement"], old["result"]["quinella_settlement"])
        self.assertEqual(saved["result"]["payouts"]["quinella"], old["result"]["payouts"]["quinella"])
        self.assertEqual(saved["horses"], old["horses"])
        self.assertEqual(saved["prediction"], old["prediction"])

    @patch("collect.setup_logger", return_value=logging.getLogger("test-quinella-legacy"))
    def test_direct_post_collection_keeps_confirmed_quinella_on_retry(self, _logger):
        payload, config = payload_with_odds(), load_config()
        payload["result"] = parse_result(result_html())
        expected = copy.deepcopy(payload["result"])
        with tempfile.TemporaryDirectory() as temporary:
            root, path = Path(temporary), Path(temporary) / "race.json"
            save_race_json(path, payload)
            with (
                patch("collect.race_json_path", return_value=path),
                patch("collect.fetch_html", side_effect=[("entry", "https://race.netkeiba.com"), result_html(pairs=(), amounts=())]),
                patch("collect.parse_race_overview", return_value=payload["race"]),
                patch("collect.parse_entry_horse_identities", return_value={h["horse_number"]: h["horse_name"] for h in payload["horses"]}),
                patch("collect.fetch_validated_win_odds", return_value=({}, CAPTURED, "netkeiba", "https://example.invalid")),
                patch("collect.parse_horses", return_value=payload["horses"]),
            ):
                self.assertEqual(collect_races(config, "test", "2026-09-05", "post", root, ["202606040111"]), [path])
            saved = load_race_json(path)["result"]
        self.assertEqual(saved["quinella_settlement"], expected["quinella_settlement"])
        self.assertEqual(saved["payouts"]["quinella"], expected["payouts"]["quinella"])


class FlowAndSummaryTests(unittest.TestCase):
    def test_default_settings_and_missing_quinella_do_not_replace_win_history(self):
        config, payload = load_config(), payload_with_odds()
        q_settings = config["simulation"]["quinella"]
        self.assertEqual(q_settings, {"harville_lambda": .81, "value": {"ev_threshold": 1.1, "kelly_fraction": .8}, "dutching": {"max_selection_count": 10, "min_coverage_probability": .4, "min_group_expected_value": .75, "min_profit_rate": .2}})
        old_config = copy.deepcopy(config)
        old_config["simulation"].pop("quinella")
        payload["simulation"] = calculate_pre_simulation(payload, old_config)
        for simulation in [payload["simulation"], *payload["simulation"]["variants"]]:
            for method in ("value", "dutching"):
                simulation[method]["post"] = {"saved_history": True}
        before = copy.deepcopy(payload)
        config["simulation"]["value"]["kelly_fraction"] = .01
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            updated = calculate_pre_simulation(payload, config)
        for old, new in zip([before["simulation"], *before["simulation"]["variants"]], [updated, *updated["variants"]]):
            self.assertEqual(new["quinella"]["status"], "ready")
            for method in ("value", "dutching"):
                self.assertEqual(new[method], old[method])
        self.assertEqual(payload, before)

    @patch("run_pre_collect.setup_logger", return_value=logging.getLogger("test-quinella-export"))
    def test_pre_input_initialization_keeps_quinella_and_excludes_it_from_input(self, _logger):
        config, payload = load_config(), payload_with_odds()
        payload["prediction"] = None
        payload["simulation"]["quinella"] = {"status": "unavailable", "reason": "odds_unavailable"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "race.json"
            save_race_json(path, payload)
            with patch("run_pre_collect.outbox_chat_input_dir", return_value=root / "input"):
                exported = export_prediction_chat_input([path], config, "test-export")
            self.assertEqual(load_race_json(path)["simulation"]["quinella"], payload["simulation"]["quinella"])
            self.assertNotIn("quinella_odds", json.loads(exported[0].read_text(encoding="utf-8"))["race"])

    def test_both_ai_pre_freeze_and_old_config_compatibility(self):
        payload, config = payload_with_odds(), load_config()
        before = copy.deepcopy(payload)
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            simulation = calculate_pre_simulation(payload, config)
        self.assertEqual(payload, before)
        for sim in (simulation, simulation["variants"][0]):
            self.assertEqual(sim["quinella"]["status"], "ready")
            for method in ("value", "dutching"):
                self.assertEqual(sim["quinella"][method]["pre"]["budget"], config["simulation"]["budget"])
                self.assertEqual(sim["quinella"][method]["pre"]["settings"], config["simulation"]["quinella"][method])
        self.assertNotEqual(simulation["quinella"]["probabilities"], simulation["variants"][0]["quinella"]["probabilities"])
        payload["simulation"] = simulation
        config["simulation"]["budget"] = 9000
        payload["race"]["quinella_odds"]["pairs"][0]["odds"] = 1000
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            self.assertEqual(calculate_pre_simulation(payload, config), simulation)
        del payload["simulation"]["quinella"]
        del config["simulation"]["quinella"]
        self.assertNotIn("quinella", calculate_pre_simulation(payload, config))

    def test_pre_rejects_started_result_missing_and_post_snapshot(self):
        config = load_config()
        for now, result in (("2026-09-05T15:30:00+09:00", None), (CAPTURED, parse_result(result_html()))):
            payload = payload_with_odds()
            payload["result"] = result
            with patch("quinella.now_jst", return_value=parse_jst_datetime(now)):
                self.assertIsNone(calculate_quinella_pre(payload, config, payload["prediction"]))
        for changes in ({"pairs": []}, {"api_status": "result"}, {"available": False}, {"official_datetime": "2026-09-05 15:40:00"}):
            payload = payload_with_odds()
            payload["race"]["quinella_odds"].update(changes)
            with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
                pre = calculate_quinella_pre(payload, config, payload["prediction"])
            self.assertEqual(pre["status"], "unavailable")
            self.assertIsNone(pre["value"]["pre"])

    def test_both_inputs_exclude_entire_quinella_snapshot(self):
        payload = ensure_race_payload(payload_with_odds())
        before = copy.deepcopy(payload)
        for context in (build_prediction_chat_input(load_config(), payload), build_statistical_prediction_input(payload)):
            self.assertNotIn("quinella_odds", context["race"])
            self.assertNotIn("official_datetime", json.dumps(context))
            self.assertEqual(set(context), {"meta", "race", "horses"})
        self.assertEqual(payload, before)

    def test_invalid_quinella_settings_do_not_block_win(self):
        for exponent in (None, True, 0, float("nan")):
            config, payload = load_config(), payload_with_odds()
            config["simulation"]["quinella"]["harville_lambda"] = exponent
            if exponent is None:
                del config["simulation"]["quinella"]["harville_lambda"]
            with self.subTest(exponent=exponent), patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
                simulation = calculate_pre_simulation(payload, config)
            self.assertIsNotNone(simulation["value"]["pre"])
            self.assertEqual(simulation["quinella"]["status"], "unavailable")
            self.assertIsNone(simulation["quinella"]["value"]["pre"])

    @patch("simulate.setup_logger", return_value=logging.getLogger("test-quinella-post"))
    def test_save_load_and_post_preserve_pre_and_prediction(self, _logger):
        config, payload = load_config(), payload_with_odds()
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            payload["simulation"] = calculate_pre_simulation(payload, config)
        payload["result"] = parse_result(result_html(pairs=(), amounts=()))
        with tempfile.TemporaryDirectory() as temporary:
            root, path = Path(temporary), Path(temporary) / "race.json"
            save_race_json(path, payload)
            before = load_race_json(path)
            self.assertTrue(simulate_file(path, config, "post", "test-quinella-pending", root))
            pending = load_race_json(path)
            self.assertIsNotNone(build_evaluation(pending))
            for sim in [pending["simulation"], *pending["simulation"]["variants"]]:
                self.assertEqual(sim["quinella"]["post_status"], "awaiting_payouts")
                for method in ("value", "dutching"):
                    self.assertIsNotNone(sim[method]["post"])
                    self.assertIsNone(sim["quinella"][method]["post"])
            pending["result"] = parse_result(result_html())
            save_race_json(path, pending)
            self.assertTrue(simulate_file(path, config, "post", "test-quinella-post", root))
            saved = load_race_json(path)
            self.assertEqual(saved["prediction"], before["prediction"])
            self.assertEqual(saved["horses"], before["horses"])
            for old, new in zip([before["simulation"], *before["simulation"]["variants"]], [saved["simulation"], *saved["simulation"]["variants"]]):
                for method in ("value", "dutching"):
                    self.assertEqual(new[method]["pre"], old[method]["pre"])
                    self.assertEqual(new["quinella"][method]["pre"], old["quinella"][method]["pre"])
                    self.assertEqual(new["quinella"][method]["post"]["status"], "settled")
            saved["result"]["payouts"]["quinella"] = []
            save_race_json(path, saved)
            simulate_file(path, config, "post", "test-quinella-retry", root)
            retried = load_race_json(path)
            self.assertEqual(retried["simulation"]["quinella"], saved["simulation"]["quinella"])

    def test_aggregate_settled_only_independent_ai_and_refund_not_hit(self):
        payload = payload_with_odds()
        result = parse_result(result_html((1, 2, "取消")))
        bought = {"status": "purchased", "total_stake": 100, "selections": [{"horse_numbers": [1, 2], "stake": 100}]}
        refunded = {"status": "purchased", "total_stake": 100, "selections": [{"horse_numbers": [1, 3], "stake": 100}]}
        empty = {"status": "no_purchase", "total_stake": 0, "selections": []}
        payload["simulation"]["quinella"] = {"status": "ready", "dutching": {"pre": bought, "post": calculate_quinella_post(bought, result)}, "value": {"pre": empty, "post": calculate_quinella_post(empty, result)}}
        payload["simulation"]["variants"] = [{"method": "statistical", "model_provider": "codex", "model_name": "test", "quinella": {"status": "ready", "dutching": {"pre": refunded, "post": calculate_quinella_post(refunded, result)}}}]
        pending = copy.deepcopy(payload)
        pending["simulation"]["quinella"]["dutching"]["post"] = None
        pending["simulation"]["variants"] = []
        summary = build_evaluation_summary([payload, pending, payload_with_odds()])
        traditional = summary["simulation"]["quinella"]["dutching"]
        statistical = summary["methods"]["statistical"]["simulation"]["quinella"]["dutching"]
        self.assertEqual((traditional["simulation_races"], traditional["purchase_races"], traditional["hit_races"]), (1, 1, 1))
        self.assertEqual((traditional["total_stake"], traditional["total_return"], traditional["cumulative_profit"], traditional["overall_roi"]), (100, 700, 600, 7))
        self.assertEqual((statistical["simulation_races"], statistical["hit_races"], statistical["total_refund"], statistical["overall_roi"]), (1, 0, 100, 1))
        self.assertEqual(summary["simulation"]["quinella"]["value"]["purchase_races"], 0)
        self.assertIsNone(summary["simulation"]["quinella"]["value"]["overall_roi"])


class HtmlAndBrowserCalculationTests(unittest.TestCase):
    def test_purchase_display_and_legacy_settings_are_compatible(self):
        payload = payload_with_odds()
        payload["race"]["weather"] = "晴"
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            payload["simulation"] = calculate_pre_simulation(payload, load_config())
        for simulation in [payload["simulation"], *payload["simulation"]["variants"]]:
            simulation["dutching"]["pre"]["settings"]["require_profit_if_hit"] = True
            simulation["quinella"]["dutching"]["pre"]["settings"]["require_profit_if_hit"] = True
        before = copy.deepcopy(payload)
        rendered = build_environment(ROOT).get_template("race.html.j2").render(**build_race_context(payload))
        soup = BeautifulSoup(rendered, "html.parser")
        for tooltip in soup.select(".term-tooltip"):
            tooltip.decompose()
        for ai in ("traditional", "statistical"):
            win_panels = soup.select(f"#purchase-{ai}-win .simulation-panel")
            pair_panels = soup.select(f"#purchase-{ai}-quinella .simulation-panel")
            self.assertEqual(
                [panel.h3.get_text(strip=True) for panel in win_panels],
                ["単勝分配方式", "期待値重視方式"],
            )
            self.assertEqual(
                [panel.h3.get_text(strip=True) for panel in pair_panels],
                ["馬連分配方式", "期待値重視方式"],
            )
            for win, pair in zip(win_panels, pair_panels):
                win_labels = [
                    node.get_text(strip=True).replace("自動選択頭数", "選択組数").replace("頭数", "組数")
                    for node in win.select(".metric-grid strong")
                ]
                pair_labels = [node.get_text(strip=True) for node in pair.select(".metric-grid strong")]
                self.assertEqual(pair_labels, win_labels)
            self.assertEqual(
                [node.get_text(strip=True) for node in pair_panels[0].select(".metric-grid strong")],
                ["予算", "最低利益率", "選択組数", "カバー確率", "グループ期待値", "最低払戻額", "最低利益", "合計購入額", "未使用予算"],
            )
            self.assertEqual(pair_panels[1].h3.get_text(strip=True), "期待値重視方式")
        self.assertIsNotNone(soup.select_one('input[name="max_selection_count"]'))
        self.assertIsNone(soup.select_one('input[name="require_profit_if_hit"]'))
        self.assertNotIn("的中時利益必須", soup.get_text())
        self.assertEqual(payload, before)

    def test_result_badges_use_hits_for_each_ai_ticket_and_method(self):
        template = build_environment(ROOT).get_template("race.html.j2")
        for case, hit, stake, refund in (("hit_with_loss", True, 1000, 0), ("miss", False, 1000, 0), ("refund", False, 1000, 1000), ("empty", False, 0, 0)):
            with self.subTest(case=case):
                payload = payload_with_odds()
                with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
                    payload["simulation"] = calculate_pre_simulation(payload, load_config())
                payload["result"] = parse_result(result_html())
                for simulation in [payload["simulation"], *payload["simulation"]["variants"]]:
                    for ticket in (simulation, simulation["quinella"]):
                        for method in ("value", "dutching"):
                            ticket[method]["post"] = {
                                "total_stake": stake, "total_refund": refund, "total_return": refund,
                                "profit": refund - stake, "roi": -1 if stake and not refund else 0,
                                "selections": [{"horse_number": 1, "horse_numbers": [1, 2], "stake": stake, "hit": hit, "refund": refund, "payout": 0, "return": refund}] if stake else [],
                            }
                soup = BeautifulSoup(template.render(**build_race_context(payload), page_kind="result"), "html.parser")
                for ai in ("traditional", "statistical"):
                    result_method = soup.select_one(f"#result-{ai}")
                    self.assertEqual(
                        [tab.get_text(strip=True) for tab in result_method.select(":scope > .ticket-tabs .ai-method-tab")],
                        ["単勝", "馬連"],
                    )
                    for ticket in ("win", "quinella"):
                        panels = soup.select(f"#settlement-{ai}-{ticket} .result-panel")
                        self.assertEqual(len(panels), 2)
                        self.assertEqual(
                            [panel.h3.select_one("span").get_text(strip=True) for panel in panels],
                            (["単勝分配方式のシミュレーション結果", "期待値重視方式のシミュレーション結果"] if ticket == "win" else ["馬連分配方式のシミュレーション結果", "期待値重視方式のシミュレーション結果"]),
                        )
                        for panel in panels:
                            self.assertEqual(panel.select_one("h3 .hit-badge") is not None, hit)

    def test_weather_and_saved_popularity_on_both_ai_result_tables(self):
        payload = payload_with_odds()
        payload["race"]["weather"] = "晴"
        payload["result"] = parse_result(result_html())
        payload["result"]["weather"] = "雨"
        for horse, popularity in zip(payload["horses"], (10, 2, None)):
            horse["popularity"] = popularity
        before = copy.deepcopy(payload)
        template = build_environment(ROOT).get_template("race.html.j2")
        for kind, weather in (("prediction", "晴"), ("result", "雨")):
            soup = BeautifulSoup(template.render(**build_race_context(payload), page_kind=kind), "html.parser")
            weather_label = soup.find("strong", string="天候")
            self.assertEqual(weather_label.parent.get_text(strip=True), "天候" + weather)
            if kind == "result":
                for table in soup.select(".result-table"):
                    self.assertEqual([row.select("td")[2].get("data-sort-value") for row in table.select("tbody tr")], ["10", "2", ""])
                    header = table.select("thead .sort-button")[2]
                    self.assertEqual(header["data-sort-type"], "number")
                    self.assertEqual(header["data-sort-column"], "2")
        self.assertEqual(payload, before)
        payload["race"].pop("weather")
        soup = BeautifulSoup(template.render(**build_race_context(payload)), "html.parser")
        self.assertEqual(soup.find("strong", string="天候").parent.get_text(strip=True), "天候-")

    def test_quinella_minimum_profit_rate_allows_break_even_and_ignores_legacy_key(self):
        pairs = [{"horse_numbers": [1, 2], "probability": .5, "odds": 2.0}, {"horse_numbers": [1, 3], "probability": .5, "odds": 2.0}]
        for rate, eligible in ((0, True), (.2, False)):
            with self.subTest(rate=rate):
                settings = {**load_config()["simulation"]["quinella"]["dutching"], "min_profit_rate": rate}
                result = calculate_quinella_purchase(pairs, 1000, 100, settings, "dutching")
                self.assertEqual(result["evaluated_counts"][1]["minimum_profit"], 0)
                self.assertEqual(result["evaluated_counts"][1]["eligible"], eligible)
                settings["require_profit_if_hit"] = True
                self.assertEqual(calculate_quinella_purchase(pairs, 1000, 100, settings, "dutching"), result)

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
            for ai in ("traditional", "statistical"):
                self.assertFalse(soup.select_one(f'#purchase-{ai}-win').has_attr("hidden"))
                self.assertTrue(soup.select_one(f'#purchase-{ai}-quinella').has_attr("hidden"))
                self.assertEqual(len(soup.select(f'#purchase-{ai}-quinella [data-quinella-method]')), 2)
            data = json.loads(soup.select_one("#custom-simulator-data").string)
            self.assertEqual(data["methods"]["traditional"]["quinella"]["value"]["settings"]["kelly_fraction"], .8)
            self.assertEqual(data["methods"]["traditional"]["quinella"]["pairs"][0]["odds"], original["race"]["quinella_odds"]["pairs"][0]["odds"])
            self.assertEqual([r["probability"] for r in data["methods"]["traditional"]["quinella"]["pairs"]], [r["probability"] for r in original["simulation"]["quinella"]["probabilities"]])
            payload["result"] = parse_result(result_html())
            for simulation in [payload["simulation"], *payload["simulation"]["variants"]]:
                for method in ("value", "dutching"):
                    simulation[method]["post"] = calculate_post(simulation[method]["pre"], payload["result"])
            payload["evaluation"] = build_evaluation(payload)
            save_race_json(path, payload)
            stored = path.read_bytes()
            stage = render_site(config, "test-quinella-result-render", root=root)
            result_soup = BeautifulSoup((stage / "races/2026-09-05/nakayama_11r_result.html").read_text(encoding="utf-8"), "html.parser")
            self.assertEqual(len(result_soup.select('[data-ticket-panel="quinella"] .quinella-pending')), 4)
            self.assertEqual(path.read_bytes(), stored)
            index = BeautifulSoup((stage / "index.html").read_text(encoding="utf-8"), "html.parser")
            self.assertIsNotNone(index.select_one('a[href$="nakayama_11r_result.html"]'))

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser calculation parity")
    def test_javascript_purchase_matches_python(self):
        template = (ROOT / "templates/race.html.j2").read_text(encoding="utf-8")
        start = template.index("const SIMULATION_EPSILON")
        code = template[start:template.index("(() => {", start)]
        settings = load_config()["simulation"]["quinella"]
        rows = [{"horse_numbers": list(pair), "probability": 1/21, "odds": 30.0} for pair in combinations(range(1, 8), 2)]
        cases = []
        for budget in (3000, 3099):
            for method in ("value", "dutching"):
                for fixed in ((0, 4) if method == "dutching" else (0,)):
                    cases.append([rows, budget, 100, settings[method], method, fixed])
        cases.append([[{"horse_numbers": [1, 2], "probability": .8, "odds": 1.0}, {"horse_numbers": [1, 3], "probability": .2, "odds": 5.6}], 3000, 100, settings["value"], "value", 0])
        for rate in (0, .2):
            cases.append([[{"horse_numbers": [1, 2], "probability": .5, "odds": 2.0}, {"horse_numbers": [1, 3], "probability": .5, "odds": 2.0}], 1000, 100, {**settings["dutching"], "min_profit_rate": rate}, "dutching", 0])
        rng = random.Random(20260905)
        for count in range(3, 17):
            probabilities = [rng.random() for _ in range(count)]
            total = sum(probabilities)
            horses = [{"horse_number": n+1} for n in range(count)]
            prediction = {"horses": [{"horse_number": n+1, "win_probability": p/total} for n, p in enumerate(probabilities)]}
            calculated = harville_probabilities(horses, prediction, .81)
            pairs = [{**row, "odds": round(rng.uniform(2, 200), 1)} for row in calculated]
            for method in ("value", "dutching"):
                cases.append([pairs, 3099, 100, settings[method], method, 0])
        script = code + '\nconst cases = JSON.parse(require("fs").readFileSync(0,"utf8")); process.stdout.write(JSON.stringify(cases.map(c => calculateQuinellaSimulation(...c))));'
        completed = subprocess.run(["node", "-e", script], input=json.dumps(cases), text=True, capture_output=True, check=True)
        for case, actual in zip(cases, json.loads(completed.stdout)):
            expected = calculate_quinella_purchase(*case)
            with self.subTest(method=case[4], budget=case[1], fixed=case[5]):
                self.assertEqual(actual["status"], expected["status"])
                self.assertEqual(actual["total_stake"], expected["total_stake"])
                self.assertEqual(actual["unused_budget"], expected["unused_budget"])
                self.assertEqual([(s["horse_numbers"], s["stake"]) for s in actual["selections"]], [(s["horse_numbers"], s["stake"]) for s in expected["selections"]])
                for got, want in zip(actual.get("evaluated_counts", []), expected.get("evaluated_counts", [])):
                    self.assertEqual(got["eligible"], want["eligible"])
                    self.assertEqual(got["rejection_reasons"], want["rejection_reasons"])
                    self.assertAlmostEqual(got["group_expected_value"], want["group_expected_value"], places=12)


if __name__ == "__main__":
    unittest.main()
