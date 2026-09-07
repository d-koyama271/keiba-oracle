from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from test_prediction_flow import ROOT, logger, race_payload, valid_prediction
from test_quinella import CAPTURED, payload_with_odds, result_html

import predict
import run_pre
import run_pre_collect
from collect import parse_result
from evaluation import build_evaluation, evaluate_file
from evaluation_summary import build_evaluation_summary, generate_evaluation_summary, load_evaluation_summary
from publish import publish_site
from render import build_race_context, render_site
from simulate import calculate_pre_simulation, simulate_file
from utils import (
    default_race_payload, ensure_race_payload, linked_record, load_config,
    load_race_json, parse_jst_datetime, runtime_prediction_entry, save_race_json,
)


class SchemaV10Tests(unittest.TestCase):
    def setUp(self):
        for module in ("simulate", "evaluation", "evaluation_summary"):
            patcher = patch(f"{module}.setup_logger", return_value=logger(f"schema-{module}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_new_race_uses_arrays_and_fixed_top_level_keys(self):
        payload = default_race_payload("test")
        self.assertEqual(payload["meta"]["schema_version"], 10)
        self.assertEqual(set(payload), {
            "meta", "race", "horses", "prediction", "simulation", "result", "evaluation",
        })
        for section in ("prediction", "simulation", "evaluation"):
            self.assertEqual(payload[section], [])

    def test_prediction_methods_can_be_generated_in_either_order_and_reused(self):
        for order in (("general", "statistical"), ("statistical", "general")):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(ROOT / "config", root / "config")
                path = root / "data/races/2026-08-16/test_11r.json"
                save_race_json(path, race_payload())
                config = load_config()
                response = {k: v for k, v in valid_prediction().items()
                            if k in ("horses", "optional_summary")}
                client = Mock()
                client.invoke_json.return_value = response
                functions = {"general": predict.predict_file,
                             "statistical": predict.predict_statistical_file}
                with patch.object(predict, "setup_logger", return_value=logger("schema-predict")), \
                     patch.object(predict.LLMClient, "from_config", return_value=client), \
                     patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")):
                    for index, method in enumerate(order):
                        self.assertTrue(functions[method](path, config, "schema", root))
                        saved = load_race_json(path)
                        self.assertEqual(len(saved["prediction"]), 1)
                        entry = saved["prediction"][0]
                        self.assertEqual(entry["id"], "p1")
                        self.assertEqual(entry["provider"], "OpenAI")
                        self.assertEqual(entry["family"], "GPT")
                        self.assertEqual(entry["model"], config["llm_model"])
                        self.assertEqual(entry["runtime_provider"], config["llm_provider"])
                        self.assertEqual(entry["reasoning_effort"], config["llm_reasoning_effort"])
                        self.assertTrue(entry[method]["prompt_sha256"])
                        for key in ("model", "model_provider", "model_name", "method", "reasoning_effort"):
                            self.assertNotIn(key, entry[method])
                        if index == 0:
                            self.assertNotIn(order[1], entry)
                            first = copy.deepcopy(entry[method])
                        else:
                            self.assertEqual(entry[order[0]], first)
                    before = path.read_bytes()
                    for method in order:
                        self.assertTrue(functions[method](path, config, "schema", root))
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(client.invoke_json.call_count, 2)

    def test_each_method_combination_runs_pre_post_evaluation_render_publish(self):
        for methods in (("general",), ("statistical",), ("general", "statistical")):
            with self.subTest(methods=methods), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(ROOT / "templates", root / "templates")
                config = load_config()
                config.update(data_dir="data", public_dir="public", stage_dir="stage", log_dir="logs")
                payload = payload_with_odds()
                for method in {"general", "statistical"} - set(methods):
                    payload["prediction"][0].pop(method)
                path = root / "data/races/2026-09-05/test_11r.json"
                save_race_json(path, payload)
                with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
                    self.assertTrue(simulate_file(path, config, "pre", "schema", root))
                pre = load_race_json(path)
                pre["result"] = parse_result(result_html())
                save_race_json(path, pre)
                self.assertTrue(simulate_file(path, config, "post", "schema", root))
                self.assertTrue(evaluate_file(path, config, "schema", root))
                saved = load_race_json(path)
                self.assertEqual(saved["prediction"], payload["prediction"])
                self.assertEqual(saved["result"], pre["result"])
                self.assertEqual(set(saved["evaluation"][0]), {"prediction_id", *methods})
                for method in methods:
                    for ticket in ("win", "quinella"):
                        for purchase in ("value", "dutching"):
                            simulation = saved["simulation"][0][method][ticket][purchase]
                            self.assertEqual(simulation["pre"], pre["simulation"][0][method][ticket][purchase]["pre"])
                            self.assertIsNotNone(simulation["post"])
                before = path.read_bytes()
                summary_path = generate_evaluation_summary(config, "schema", root)
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                for method in ("general", "statistical"):
                    self.assertEqual(summary["methods"][method]["overall"]["evaluated_races"], int(method in methods))
                render_site(config, "schema", root=root)
                published = publish_site(config, root)
                self.assertTrue((published / "index.html").exists())
                self.assertTrue((published / ".nojekyll").exists())
                self.assertEqual(len(list((published / "races").rglob("*.html"))), 2)
                context = build_race_context(saved)
                self.assertEqual([view["prediction_method"] for view in context["ai_views"]], list(methods))
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_v9_normalization_preserves_saved_contents_without_writing(self):
        payload = payload_with_odds()
        with patch("quinella.now_jst", return_value=parse_jst_datetime(CAPTURED)):
            payload["simulation"] = calculate_pre_simulation(payload, load_config())
        payload["result"] = parse_result(result_html())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.json"
            save_race_json(path, payload)
            self.assertTrue(simulate_file(path, load_config(), "post", "schema", Path(directory)))
            canonical = load_race_json(path)
            canonical["evaluation"] = build_evaluation(canonical)
            legacy = copy.deepcopy(canonical)
            legacy["meta"]["schema_version"] = 9
            identity = {"model_provider": "codex", "model_name": "test"}
            entry = canonical["prediction"][0]
            legacy["prediction"] = {**entry["general"], **identity, "variants": [
                {**entry["statistical"], **identity, "method": "statistical"},
            ]}
            simulation = canonical["simulation"][0]
            legacy["simulation"] = {**simulation["general"]["win"], "quinella": simulation["general"]["quinella"], "variants": [
                {**simulation["statistical"]["win"], "quinella": simulation["statistical"]["quinella"], **identity, "method": "statistical"},
            ]}
            evaluation = canonical["evaluation"][0]
            legacy["evaluation"] = {**evaluation["general"], "variants": [
                {**evaluation["statistical"], "method": "statistical"},
            ]}
            path.write_text(json.dumps(legacy), encoding="utf-8")
            before = path.read_bytes()
            normalized = load_race_json(path)
            for section in ("prediction", "simulation", "evaluation", "horses", "race", "result"):
                self.assertEqual(normalized[section], canonical[section], section)
            self.assertEqual(build_race_context(normalized)["ai_views"], build_race_context(canonical)["ai_views"])
            self.assertEqual(build_evaluation(normalized), canonical["evaluation"])
            self.assertEqual(build_evaluation_summary([normalized]), build_evaluation_summary([canonical]))
            self.assertEqual(path.read_bytes(), before)
            save_race_json(path, normalized)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["meta"]["schema_version"], 10)
            for section in ("prediction", "simulation", "evaluation"):
                self.assertEqual(persisted[section], normalized[section])

    def test_references_use_ids_not_array_order_or_model_names(self):
        payload = payload_with_odds()
        payload["result"] = parse_result(result_html())
        first = payload["prediction"][0]
        statistical = first.pop("statistical")
        payload["prediction"].append({**{k: v for k, v in first.items() if k != "general"}, "id": "p2", "statistical": statistical})
        payload["evaluation"] = build_evaluation(payload)
        payload["evaluation"].reverse()
        self.assertIn("general", linked_record(payload, "evaluation", "p1"))
        self.assertIn("statistical", linked_record(payload, "evaluation", "p2"))
        summary = build_evaluation_summary([payload])
        self.assertEqual(summary["paired_comparison"]["compared_races"], 0)
        context = build_race_context(payload)
        self.assertEqual([view["method"] for view in context["ai_views"]], ["p1-general", "p2-statistical"])

    def test_legacy_models_are_linked_to_separate_opaque_ids(self):
        legacy = race_payload(valid_prediction())
        statistical = {**valid_prediction(), "model_name": "gpt-another", "method": "statistical"}
        legacy["prediction"]["variants"] = [statistical]
        legacy["simulation"]["variants"] = [{
            "method": "statistical", "model_provider": "codex", "model_name": "gpt-another",
            "value": {"pre": {"saved": 1}, "post": None},
        }]
        legacy["evaluation"] = {"saved": "general", "variants": [{
            "method": "statistical", "model_provider": "codex", "model_name": "gpt-another",
            "saved": "statistical",
        }]}
        normalized = ensure_race_payload(legacy)
        self.assertEqual([e["id"] for e in normalized["prediction"]], ["p1", "p2"])
        self.assertIn("general", normalized["prediction"][0])
        self.assertNotIn("general", normalized["prediction"][1])
        self.assertEqual(linked_record(normalized, "simulation", "p2")["statistical"]["win"]["value"]["pre"], {"saved": 1})
        self.assertEqual(linked_record(normalized, "evaluation", "p2")["statistical"], {"saved": "statistical"})

    def test_existing_summary_keeps_method_revenue_when_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "evaluation_summary.json"
            old = {"methods": {"traditional": {"simulation": {"value": {"cumulative_profit": 1234}}}}}
            path.write_text(json.dumps(old), encoding="utf-8")
            before = path.read_bytes()
            loaded = load_evaluation_summary({"data_dir": "."}, root)
            self.assertEqual(loaded["methods"]["general"], old["methods"]["traditional"])
            self.assertEqual(path.read_bytes(), before)

    def test_new_method_does_not_inherit_different_recorded_reasoning_effort(self):
        config = load_config()
        payload = default_race_payload("test")
        old = runtime_prediction_entry(payload, {**config, "llm_reasoning_effort": "low"}, create=True)
        old["statistical"] = {"horses": [{"horse_number": 1, "win_probability": 1}]}
        saved = copy.deepcopy(old)
        new = runtime_prediction_entry(payload, config, create=True)
        self.assertEqual(old, saved)
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(new["reasoning_effort"], config["llm_reasoning_effort"])

    def test_general_generation_failure_does_not_prevent_statistical_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.json"
            save_race_json(path, race_payload())
            config = load_config()
            with patch.object(run_pre, "setup_logger", return_value=logger("schema-pre")), \
                 patch.object(run_pre, "run_pre_collect_flow", return_value=([path], [])), \
                 patch.object(run_pre, "load_prediction_inputs", return_value={"202601010111": {}}), \
                 patch.object(run_pre, "build_pending_statistical_inputs", return_value={"202601010111": {}}), \
                 patch.object(run_pre, "predict_paths", return_value=[]), \
                 patch.object(run_pre, "predict_statistical_paths", return_value=[path]) as statistical, \
                 patch.object(run_pre, "simulate_paths", return_value=[path]) as simulate, \
                 patch.object(run_pre, "render_site"), \
                 patch.object(run_pre, "publish_site") as publish:
                self.assertEqual(run_pre.run_pre_flow(config, None, "schema"), [path])
                statistical.assert_called_once()
                simulate.assert_called_once_with([path], config, "pre", "schema")
                publish.assert_called_once()

    def test_changed_settings_reuse_p2_for_either_method_without_p3(self):
        for change in ({"llm_model": "gpt-6"}, {"llm_reasoning_effort": "low"}):
            for order in (("general", "statistical"), ("statistical", "general")):
                with self.subTest(change=change, order=order), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    shutil.copytree(ROOT / "config", root / "config")
                    path = root / "race.json"
                    save_race_json(path, race_payload())
                    config = load_config()
                    functions = {"general": predict.predict_file, "statistical": predict.predict_statistical_file}
                    client = Mock()
                    client.invoke_json.return_value = {k: v for k, v in valid_prediction().items()
                                                       if k in ("horses", "optional_summary")}
                    with patch.object(predict, "setup_logger", return_value=logger("snapshot")), \
                         patch.object(predict.LLMClient, "from_config", return_value=client), \
                         patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")):
                        for method in order:
                            self.assertTrue(functions[method](path, config, "snapshot", root))
                        p1 = load_race_json(path)["prediction"][0]
                        config.update(change)
                        for index, method in enumerate(order):
                            self.assertTrue(functions[method](path, config, "snapshot", root))
                            entries = load_race_json(path)["prediction"]
                            self.assertEqual([entry["id"] for entry in entries], ["p1", "p2"])
                            self.assertEqual(entries[0], p1)
                            self.assertIn(method, entries[1])
                            if index == 0:
                                self.assertNotIn(order[1], entries[1])
                            before = path.read_bytes()
                            self.assertTrue(functions[method](path, config, "snapshot", root))
                            self.assertEqual(path.read_bytes(), before)
                        self.assertEqual(client.invoke_json.call_count, 4)

    def test_identity_requires_all_five_fields_but_not_audit_fields(self):
        config = load_config()
        for key in ("provider", "family", "model", "runtime_provider", "reasoning_effort"):
            with self.subTest(key=key):
                payload = default_race_payload("test")
                original = runtime_prediction_entry(payload, config, create=True)
                original[key] = "different"
                new = runtime_prediction_entry(payload, config, create=True)
                self.assertEqual(new["id"], "p2")
                new["general"] = {"predicted_at": "different", "prompt_sha256": "changed", "prediction_input_sha256": "changed"}
                self.assertIs(runtime_prediction_entry(payload, config, create=True), new)
                self.assertEqual(len(payload["prediction"]), 2)

    def test_general_input_export_uses_current_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config()
            payload = ensure_race_payload(race_payload())
            entry = runtime_prediction_entry(payload, config, create=True)
            entry["general"] = {k: v for k, v in valid_prediction().items()
                                if k in ("horses", "optional_summary", "predicted_at")}
            path = root / "race.json"
            save_race_json(path, payload)
            before = path.read_bytes()
            with patch.object(run_pre_collect, "setup_logger", return_value=logger("snapshot-export")), \
                 patch.object(run_pre_collect, "outbox_chat_input_dir", return_value=root / "outbox"):
                self.assertEqual(run_pre_collect.export_prediction_chat_input([path], config, "snapshot"), [])
                self.assertEqual(path.read_bytes(), before)
                config["llm_model"] = "gpt-6"
                self.assertEqual(len(run_pre_collect.export_prediction_chat_input([path], config, "snapshot")), 1)
                self.assertEqual(load_race_json(path)["prediction"], payload["prediction"])

    def test_pre_flow_publishes_successful_races_and_excludes_both_failed(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config = load_config()
            config.update(data_dir=str(root / "data"), public_dir=str(root / "public"))
            expected = {"general-only": {"general"}, "statistical-only": {"statistical"},
                        "both": {"general", "statistical"}, "failed": set()}
            paths, inputs = [], []
            for number, race_id in enumerate(expected, 1):
                payload = race_payload()
                payload["meta"]["race_id"] = race_id
                payload["race"]["race_number"] = number
                path = root / f"data/races/2026-08-16/test_{number}r.json"
                save_race_json(path, payload)
                input_path = root / f"input_{number}.json"
                input_path.write_text(json.dumps(predict.build_prediction_chat_input(config, payload)), encoding="utf-8")
                paths.append(path)
                inputs.append(input_path)

            def generate(config, prediction_input, horses, prompt_file, *args):
                method = "statistical" if "statistical" in prompt_file else "general"
                if method not in expected[prediction_input["meta"]["race_id"]]:
                    raise ValueError("prediction failed for test")
                return {**{k: v for k, v in valid_prediction().items() if k in ("horses", "optional_summary", "predicted_at")},
                        "prompt_sha256": "a" * 64, "prediction_input_sha256": "b" * 64}

            for module in ("run_pre", "predict", "simulate"):
                stack.enter_context(patch(f"{module}.setup_logger", return_value=logger(f"partial-{module}")))
            stack.enter_context(patch.object(predict, "generate_prediction", side_effect=generate))
            stack.enter_context(patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")))
            collected = stack.enter_context(patch.object(run_pre, "run_pre_collect_flow", return_value=(paths, inputs)))
            self.assertEqual(run_pre.run_pre_flow(config, None, "partial"), paths[:3])
            for path, methods in zip(paths, expected.values()):
                saved = load_race_json(path)
                if methods:
                    self.assertEqual({m for m in ("general", "statistical") if m in saved["prediction"][0]}, methods)
                    self.assertEqual(set(saved["simulation"][0]) - {"prediction_id"}, methods)
                    self.assertEqual(saved["meta"]["pre_status"], "published")
                else:
                    self.assertEqual(saved["prediction"], [])
                    self.assertNotEqual(saved["meta"]["pre_status"], "published")
            public = root / "public"
            self.assertEqual(len(list((public / "races").rglob("*.html"))), 3)
            before = (public / "index.html").read_bytes()
            collected.return_value = ([paths[-1]], [inputs[-1]])
            with self.assertRaisesRegex(RuntimeError, "failed for both methods"):
                run_pre.run_pre_flow(config, None, "failed")
            self.assertEqual((public / "index.html").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
