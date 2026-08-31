from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from logging import NullHandler, getLogger
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import predict  # noqa: E402
import response_importer  # noqa: E402
import run_pre  # noqa: E402
import run_pre_collect  # noqa: E402
import simulate  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from utils import JST, load_race_json, save_race_json  # noqa: E402


def race_payload(prediction: dict | None = None) -> dict:
    return {
        "meta": {
            "race_id": "202601010111",
            "schema_version": 5,
            "created_at": "2026-08-15T12:00:00+09:00",
            "updated_at": "2026-08-15T12:00:00+09:00",
            "pre_status": "awaiting_prediction",
            "post_status": "awaiting_result",
        },
        "race": {
            "date": "2026-08-16",
            "track": "札幌",
            "race_number": 11,
            "race_name": "テスト重賞",
            "start_time": "15:45",
        },
        "horses": [
            {
                "horse_number": 1,
                "horse_name": "テストホースA",
                "win_odds": 2.0,
                "popularity": 1,
                "past_runs": [],
                "career_summaries": {},
            },
            {
                "horse_number": 2,
                "horse_name": "テストホースB",
                "win_odds": 4.0,
                "popularity": 2,
                "past_runs": [],
                "career_summaries": {},
            },
        ],
        "prediction": prediction,
        "simulation": {
            "value": {"pre": None, "post": None},
            "dutching": {"pre": None, "post": None},
        },
        "result": None,
        "evaluation": None,
    }


def valid_prediction(provider: str = "codex") -> dict:
    return {
        "horses": [
            {"horse_number": 1, "win_probability": 0.6, "reason": "条件実績を評価。"},
            {"horse_number": 2, "win_probability": 0.4, "reason": "相手強化を考慮。"},
        ],
        "optional_summary": "1番を中心に評価。",
        "model_provider": provider,
        "model_name": "gpt-test",
        "predicted_at": "2026-08-15T13:00:00+09:00",
    }


def logger(name: str):
    value = getLogger(name)
    value.handlers.clear()
    value.addHandler(NullHandler())
    return value


class PredictionValidationTests(unittest.TestCase):
    def test_statistical_prompt_is_dedicated_and_contains_no_market_terms(self) -> None:
        prompt_text = (ROOT / "config" / "prompt_prediction_statistical.txt").read_text(
            encoding="utf-8"
        )

        for forbidden in predict.STATISTICAL_FORBIDDEN_OUTPUT_TERMS:
            self.assertNotIn(forbidden, prompt_text.lower())
        self.assertRegex(prompt_text, r"全(?:出走)?馬.*(?:比較|評価)")
        self.assertRegex(prompt_text, r"確率合計.*1\.0")
        self.assertRegex(prompt_text, r"(?:Web|ウェブ).*(?:参照|検索).*(?:ない|しない|禁止)")
        self.assertIn("{{RACE_CONTEXT}}", prompt_text)

    def test_prediction_audit_hashes_are_stable_and_content_sensitive(self) -> None:
        payload = race_payload()
        first = predict.build_prediction_chat_input({}, payload)
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["meta"]["generated_at"] = "2099-01-01T00:00:00+09:00"

        self.assertEqual(predict.sha256_text("same prompt"), predict.sha256_text("same prompt"))
        self.assertNotEqual(predict.sha256_text("same prompt"), predict.sha256_text("changed prompt"))
        self.assertEqual(
            predict.prediction_input_sha256(first),
            predict.prediction_input_sha256(second),
        )
        second["horses"][0]["horse_name"] = "changed horse"
        self.assertNotEqual(
            predict.prediction_input_sha256(first),
            predict.prediction_input_sha256(second),
        )

    def test_duplicate_and_unexpected_horse_numbers_are_rejected(self) -> None:
        horses = race_payload()["horses"]
        with self.assertRaisesRegex(ValueError, "duplicate horse prediction"):
            predict.normalize_prediction_response(
                {
                    "horses": [
                        {"horse_number": 1, "win_probability": 0.5, "reason": "A"},
                        {"horse_number": 1, "win_probability": 0.5, "reason": "B"},
                    ]
                },
                horses,
            )
        with self.assertRaisesRegex(ValueError, "unexpected horse prediction"):
            predict.normalize_prediction_response(
                {
                    "horses": [
                        {"horse_number": 1, "win_probability": 0.5, "reason": "A"},
                        {"horse_number": 3, "win_probability": 0.5, "reason": "B"},
                    ]
                },
                horses,
            )

    def test_codex_prediction_uses_only_finalized_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "prompt_prediction.txt").write_text(
                "Use only this JSON and return JSON: {{RACE_CONTEXT}}",
                encoding="utf-8",
            )
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            payload = race_payload()
            payload["result"] = {"secret": "RESULT_MUST_NOT_LEAK"}
            payload["evaluation"] = {"secret": "EVALUATION_MUST_NOT_LEAK"}
            save_race_json(path, payload)
            prediction_input = predict.build_prediction_chat_input({}, payload, root)
            captured: dict[str, str] = {}

            class FakeClient:
                def invoke_json(self, prompt: str, max_retries: int = 2) -> dict:
                    captured["prompt"] = prompt
                    return {
                        "horses": [
                            {"horse_number": 1, "win_probability": 0.7, "reason": "条件上位。"},
                            {"horse_number": 2, "win_probability": 0.3, "reason": "相手強化。"},
                        ],
                        "optional_summary": "1番を上位評価。",
                    }

            config = {"data_dir": "data", "llm_provider": "codex", "llm_model": "gpt-test"}
            with patch.object(predict, "setup_logger", return_value=logger("test.predict.input")), patch.object(
                predict.LLMClient,
                "from_config",
                return_value=FakeClient(),
            ), patch.object(predict, "now_jst_iso", return_value="2026-08-15T14:00:00+09:00"):
                updated = predict.predict_file(
                    path,
                    config,
                    "test-predict",
                    root,
                    prediction_input,
                )

            self.assertTrue(updated)
            self.assertNotIn("RESULT_MUST_NOT_LEAK", captured["prompt"])
            self.assertNotIn("EVALUATION_MUST_NOT_LEAK", captured["prompt"])
            self.assertIn('"race_id": "202601010111"', captured["prompt"])
            saved = load_race_json(path)
            self.assertEqual(saved["prediction"]["model_provider"], "codex")
            self.assertEqual(saved["prediction"]["model_name"], "gpt-test")
            self.assertEqual(saved["prediction"]["predicted_at"], "2026-08-15T14:00:00+09:00")
            self.assertEqual(
                saved["prediction"]["prompt_sha256"],
                predict.sha256_text("Use only this JSON and return JSON: {{RACE_CONTEXT}}"),
            )
            self.assertEqual(
                saved["prediction"]["prediction_input_sha256"],
                predict.prediction_input_sha256(prediction_input),
            )
            self.assertEqual(len(saved["prediction"]["prompt_sha256"]), 64)
            self.assertEqual(len(saved["prediction"]["prediction_input_sha256"]), 64)
            self.assertAlmostEqual(
                sum(item["win_probability"] for item in saved["prediction"]["horses"]),
                1.0,
            )

    def test_existing_prediction_is_reused_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            save_race_json(path, race_payload(valid_prediction()))
            before = path.read_bytes()
            config = {"data_dir": "data", "llm_provider": "codex", "llm_model": "gpt-test"}

            with patch.object(predict, "setup_logger", return_value=logger("test.predict.reuse")), patch.object(
                predict.LLMClient,
                "from_config",
                side_effect=AssertionError("Codex must not be called"),
            ):
                reused = predict.predict_file(path, config, "test-reuse", root)

            self.assertTrue(reused)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("prompt_sha256", load_race_json(path)["prediction"])
            self.assertNotIn("prediction_input_sha256", load_race_json(path)["prediction"])

    def test_statistical_input_removes_all_market_and_non_input_data(self) -> None:
        payload = race_payload(valid_prediction())
        payload["race"].update(
            {
                "surface": "芝",
                "distance": 2000,
                "odds_captured_at": "2026-08-16T14:45:00+09:00",
                "odds_source": "netkeiba",
                "odds_source_url": "https://example.invalid/odds",
                "odds_reference_minutes_before_start": 60,
                "normalized_market_probability": 0.4,
                "source_url": "https://example.invalid/race",
            }
        )
        payload["horses"][0].update(
            {
                "jockey": "騎手A",
                "past_runs": [
                    {
                        "race_id": "202601010101",
                        "finish_position": 2,
                        "race_time_seconds": 120.4,
                        "win_odds": 3.5,
                        "popularity": 1,
                        "market_probability": 0.25,
                    }
                ],
            }
        )
        payload["simulation"]["value"]["pre"] = {"secret": "simulation"}
        payload["result"] = {"secret": "result"}
        payload["evaluation"] = {"secret": "evaluation"}
        original = copy.deepcopy(payload)

        first = predict.build_statistical_prediction_input(payload)
        second = predict.build_statistical_prediction_input(payload)

        self.assertEqual(first, second)
        self.assertEqual(set(first), {"meta", "race", "horses"})
        self.assertEqual(first["meta"]["method"], "statistical")
        self.assertEqual(first["race"]["surface"], "芝")
        self.assertEqual(first["race"]["distance"], 2000)
        self.assertEqual(first["horses"][0]["jockey"], "騎手A")
        self.assertEqual(first["horses"][0]["past_runs"][0]["finish_position"], 2)
        self.assertEqual(first["horses"][0]["past_runs"][0]["race_time_seconds"], 120.4)

        def all_keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from all_keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from all_keys(item)

        keys = set(all_keys(first))
        for forbidden in (
            "win_odds",
            "popularity",
            "market_probability",
            "normalized_market_probability",
            "odds_captured_at",
            "odds_source",
            "odds_source_url",
            "odds_reference_minutes_before_start",
            "source_url",
            "prediction",
            "simulation",
            "result",
            "evaluation",
        ):
            self.assertNotIn(forbidden, keys)
        self.assertEqual(payload, original)
        self.assertEqual(
            predict.prediction_input_sha256(first),
            predict.prediction_input_sha256(second),
        )

    def test_statistical_prediction_is_saved_as_variant_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_text = "客観データだけを使う: {{RACE_CONTEXT}}"
            (root / "config").mkdir()
            (root / "config" / "prompt_prediction_statistical.txt").write_text(
                prompt_text,
                encoding="utf-8",
            )
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            traditional = valid_prediction()
            payload = race_payload(copy.deepcopy(traditional))
            payload["race"]["odds_source"] = "netkeiba"
            payload["horses"][0]["past_runs"] = [
                {"finish_position": 1, "win_odds": 2.5, "popularity": 1}
            ]
            save_race_json(path, payload)
            frozen_input = predict.build_statistical_prediction_input(payload)
            captured: dict[str, str] = {}

            class FakeClient:
                def invoke_json(self, prompt: str, max_retries: int = 2) -> dict:
                    captured["prompt"] = prompt
                    return {
                        "horses": [
                            {"horse_number": 1, "win_probability": 0.25, "reason": "近走内容を評価。"},
                            {"horse_number": 2, "win_probability": 0.75, "reason": "条件適性を評価。"},
                        ],
                        "optional_summary": "2番を上位評価。",
                    }

            config = {"data_dir": "data", "llm_provider": "codex", "llm_model": "gpt-test"}
            before_start = datetime(2026, 8, 16, 12, 0, tzinfo=JST)
            with patch.object(predict, "setup_logger", return_value=logger("test.statistical")), patch.object(
                predict.LLMClient,
                "from_config",
                return_value=FakeClient(),
            ), patch.object(predict, "now_jst", return_value=before_start), patch.object(
                predict,
                "now_jst_iso",
                return_value="2026-08-16T12:00:00+09:00",
            ):
                self.assertTrue(
                    predict.predict_statistical_file(
                        path,
                        config,
                        "test-statistical",
                        root,
                        frozen_input,
                    )
                )

            saved = load_race_json(path)
            self.assertEqual(saved["meta"]["schema_version"], 8)
            self.assertEqual(
                set(saved),
                {"meta", "race", "horses", "prediction", "simulation", "result", "evaluation"},
            )
            saved_traditional = copy.deepcopy(saved["prediction"])
            variants = saved_traditional.pop("variants")
            self.assertEqual(saved_traditional, traditional)
            self.assertEqual(len(variants), 1)
            statistical = variants[0]
            self.assertEqual(statistical["method"], "statistical")
            self.assertEqual(statistical["model_provider"], "codex")
            self.assertEqual(statistical["model_name"], "gpt-test")
            self.assertEqual(statistical["predicted_at"], "2026-08-16T12:00:00+09:00")
            self.assertEqual(statistical["prompt_sha256"], predict.sha256_text(prompt_text))
            self.assertEqual(
                statistical["prediction_input_sha256"],
                predict.prediction_input_sha256(frozen_input),
            )
            self.assertAlmostEqual(
                sum(item["win_probability"] for item in statistical["horses"]),
                1.0,
            )
            self.assertNotIn("win_odds", captured["prompt"])
            self.assertNotIn("popularity", captured["prompt"])
            self.assertNotIn("odds_source", captured["prompt"])

            before_reuse = path.read_bytes()
            with patch.object(predict, "setup_logger", return_value=logger("test.statistical.reuse")), patch.object(
                predict.LLMClient,
                "from_config",
                side_effect=AssertionError("Codex must not be called"),
            ):
                self.assertTrue(
                    predict.predict_statistical_file(path, config, "test-statistical-reuse", root)
                )
            self.assertEqual(path.read_bytes(), before_reuse)

    def test_statistical_prediction_is_not_backfilled_after_result(self) -> None:
        payload = race_payload(valid_prediction())
        payload["result"] = {"horses": [{"horse_number": 1, "finish_position": 1}]}

        with self.assertRaisesRegex(ValueError, "after result collection"):
            predict.ensure_statistical_prediction_is_pre_race(payload)

        payload["result"] = None
        after_start = datetime(2026, 8, 16, 16, 0, tzinfo=JST)
        with patch.object(predict, "now_jst", return_value=after_start):
            with self.assertRaisesRegex(ValueError, "after race start"):
                predict.ensure_statistical_prediction_is_pre_race(payload)

    def test_statistical_reason_cannot_reference_market_information(self) -> None:
        with self.assertRaisesRegex(ValueError, "market-related wording"):
            predict.validate_statistical_prediction_text(
                {
                    "horses": [
                        {
                            "horse_number": 1,
                            "win_probability": 1.0,
                            "reason": "上位人気を評価。",
                        }
                    ],
                    "optional_summary": "客観比較。",
                }
            )


class CodexClientTests(unittest.TestCase):
    def test_codex_cli_is_isolated_and_uses_structured_output(self) -> None:
        commands: list[list[str]] = []
        run_kwargs: dict = {}

        def run(command: list[str], **kwargs):
            commands.append(command)
            run_kwargs.update(kwargs)
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "horses": [
                            {"horse_number": 1, "win_probability": 1.0, "reason": "test"}
                        ],
                        "optional_summary": "test summary",
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("llm_client.shutil.which", return_value="codex"), patch(
            "llm_client.subprocess.run",
            side_effect=run,
        ):
            response = LLMClient("codex", "gpt-test").invoke_json("ONLY_INPUT", max_retries=0)

        command = commands[0]
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], "gpt-test")
        self.assertIn("--output-schema", command)
        self.assertEqual(run_kwargs["input"], "ONLY_INPUT")
        self.assertNotEqual(Path(run_kwargs["cwd"]), ROOT)
        self.assertEqual(response["horses"][0]["horse_number"], 1)


class FlowAndCompatibilityTests(unittest.TestCase):
    def test_export_does_not_clear_existing_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            payload = race_payload(valid_prediction())
            payload["simulation"]["value"]["pre"] = {"selections": []}
            save_race_json(path, payload)
            before = path.read_bytes()

            with patch.object(
                run_pre_collect,
                "setup_logger",
                return_value=logger("test.export.reuse"),
            ), patch.object(
                run_pre_collect,
                "outbox_chat_input_dir",
                return_value=root / "outbox",
            ):
                exported = run_pre_collect.export_prediction_chat_input([path], {}, "test-export")

            self.assertEqual(exported, [])
            self.assertEqual(path.read_bytes(), before)

    def test_normal_pre_flow_reuses_prediction_through_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            original_prediction = valid_prediction()
            save_race_json(path, race_payload(original_prediction))
            config = {
                "data_dir": str(root / "data"),
                "public_dir": str(root / "public"),
                "llm_provider": "codex",
                "llm_model": "gpt-test",
                "simulation": {
                    "budget": 3000,
                    "stake_unit": 100,
                    "value": {"ev_threshold": 1.0, "kelly_fraction": 0.5},
                    "dutching": {
                        "max_selection_count": 2,
                        "min_coverage_probability": 0.0,
                        "min_group_expected_value": 0.0,
                        "min_profit_rate": 0.20,
                        "require_profit_if_hit": False,
                    },
                },
            }

            class StatisticalClient:
                def invoke_json(self, prompt: str, max_retries: int = 2) -> dict:
                    return {
                        "horses": [
                            {"horse_number": 1, "win_probability": 0.2, "reason": "近走内容を評価。"},
                            {"horse_number": 2, "win_probability": 0.8, "reason": "条件適性を評価。"},
                        ],
                        "optional_summary": "2番を上位評価。",
                    }

            with patch.object(
                run_pre,
                "run_pre_collect_flow",
                return_value=([path], []),
            ), patch.object(run_pre, "setup_logger", return_value=logger("test.pre.flow")), patch.object(
                predict,
                "setup_logger",
                return_value=logger("test.pre.predict"),
            ), patch.object(
                simulate,
                "setup_logger",
                return_value=logger("test.pre.simulate"),
            ), patch.object(
                run_pre,
                "render_site",
            ), patch.object(run_pre, "publish_site", return_value=root / "public"), patch.object(
                predict.LLMClient,
                "from_config",
                return_value=StatisticalClient(),
            ), patch.object(
                predict,
                "now_jst",
                return_value=datetime(2026, 8, 16, 12, 0, tzinfo=JST),
            ):
                processed = run_pre.run_pre_flow(config, None, "test-pre")

            saved = load_race_json(path)
            self.assertEqual(processed, [path])
            saved_traditional = copy.deepcopy(saved["prediction"])
            statistical = saved_traditional.pop("variants")
            self.assertEqual(saved_traditional, original_prediction)
            self.assertEqual(statistical[0]["method"], "statistical")
            self.assertEqual(statistical[0]["horses"][0]["win_probability"], 0.2)
            self.assertIsNotNone(saved["simulation"]["value"]["pre"])
            self.assertIsNotNone(saved["simulation"]["dutching"]["pre"])
            for selection in saved["simulation"]["value"]["pre"]["selections"]:
                expected = next(
                    item["win_probability"]
                    for item in original_prediction["horses"]
                    if item["horse_number"] == selection["horse_number"]
                )
                self.assertEqual(selection["predicted_probability"], expected)
            self.assertEqual(saved["meta"]["pre_status"], "published")

    def test_normal_pre_flow_generates_codex_prediction_then_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            payload = race_payload()
            save_race_json(path, payload)
            prediction_input = predict.build_prediction_chat_input({}, payload, root)
            input_path = root / "outbox" / "sapporo_11r.json"
            input_path.parent.mkdir(parents=True)
            input_path.write_text(json.dumps(prediction_input), encoding="utf-8")
            config = {
                "data_dir": str(root / "data"),
                "public_dir": str(root / "public"),
                "llm_provider": "codex",
                "llm_model": "gpt-test",
                "simulation": {
                    "budget": 3000,
                    "stake_unit": 100,
                    "value": {"ev_threshold": 1.0, "kelly_fraction": 0.5},
                    "dutching": {
                        "max_selection_count": 2,
                        "min_coverage_probability": 0.0,
                        "min_group_expected_value": 0.0,
                        "min_profit_rate": 0.20,
                        "require_profit_if_hit": False,
                    },
                },
            }

            prompts: list[str] = []

            class FakeClient:
                def invoke_json(self, prompt: str, max_retries: int = 2) -> dict:
                    prompts.append(prompt)
                    statistical = '"method": "statistical"' in prompt
                    return {
                        "horses": [
                            {
                                "horse_number": 1,
                                "win_probability": 0.25 if statistical else 0.65,
                                "reason": "条件適性を評価。" if statistical else "条件上位。",
                            },
                            {
                                "horse_number": 2,
                                "win_probability": 0.75 if statistical else 0.35,
                                "reason": "近走内容を評価。" if statistical else "相手強化。",
                            },
                        ],
                        "optional_summary": "2番を上位評価。" if statistical else "1番を上位評価。",
                    }

            with patch.object(
                run_pre,
                "run_pre_collect_flow",
                return_value=([path], [input_path]),
            ), patch.object(run_pre, "setup_logger", return_value=logger("test.pre.new")), patch.object(
                predict,
                "setup_logger",
                return_value=logger("test.pre.new.predict"),
            ), patch.object(
                simulate,
                "setup_logger",
                return_value=logger("test.pre.new.simulate"),
            ), patch.object(
                predict.LLMClient,
                "from_config",
                return_value=FakeClient(),
            ), patch.object(
                predict,
                "now_jst_iso",
                return_value="2026-08-15T15:00:00+09:00",
            ), patch.object(
                predict,
                "now_jst",
                return_value=datetime(2026, 8, 16, 12, 0, tzinfo=JST),
            ), patch.object(run_pre, "render_site") as render_site, patch.object(
                run_pre,
                "publish_site",
                return_value=root / "public",
            ) as publish_site:
                run_pre.run_pre_flow(config, None, "test-pre-new")

            saved = load_race_json(path)
            self.assertEqual(saved["prediction"]["model_provider"], "codex")
            self.assertEqual(saved["prediction"]["model_name"], "gpt-test")
            self.assertEqual(saved["prediction"]["predicted_at"], "2026-08-15T15:00:00+09:00")
            self.assertEqual(
                [item["win_probability"] for item in saved["prediction"]["horses"]],
                [0.65, 0.35],
            )
            self.assertEqual(len(prompts), 2)
            self.assertEqual(len(saved["prediction"]["variants"]), 1)
            self.assertEqual(saved["prediction"]["variants"][0]["method"], "statistical")
            self.assertEqual(
                [
                    item["win_probability"]
                    for item in saved["prediction"]["variants"][0]["horses"]
                ],
                [0.25, 0.75],
            )
            self.assertIsNotNone(saved["simulation"]["value"]["pre"])
            self.assertIsNotNone(saved["simulation"]["dutching"]["pre"])
            self.assertEqual(saved["meta"]["pre_status"], "published")
            self.assertIsNone(saved["result"])
            self.assertIsNone(saved["evaluation"])
            render_site.assert_called_once()
            publish_site.assert_called_once()

    def test_normal_pre_flow_requires_matching_finalized_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            save_race_json(path, race_payload())
            config = {
                "data_dir": str(root / "data"),
                "llm_provider": "codex",
                "llm_model": "gpt-test",
            }

            with patch.object(
                run_pre,
                "run_pre_collect_flow",
                return_value=([path], []),
            ), patch.object(run_pre, "setup_logger", return_value=logger("test.pre.input")), patch.object(
                predict,
                "now_jst",
                return_value=datetime(2026, 8, 16, 12, 0, tzinfo=JST),
            ), patch.object(
                predict.LLMClient,
                "from_config",
                side_effect=AssertionError("Codex must not be called"),
            ):
                with self.assertRaisesRegex(RuntimeError, "finalized prediction inputs"):
                    run_pre.run_pre_flow(config, None, "test-pre-input")

    def test_pre_flow_stops_before_simulation_when_statistical_prediction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            original_prediction = valid_prediction()
            save_race_json(path, race_payload(copy.deepcopy(original_prediction)))
            config = {
                "data_dir": str(root / "data"),
                "llm_provider": "codex",
                "llm_model": "gpt-test",
            }

            with patch.object(
                run_pre,
                "run_pre_collect_flow",
                return_value=([path], []),
            ), patch.object(
                run_pre,
                "build_pending_statistical_inputs",
                return_value={"202601010111": {"meta": {}, "race": {}, "horses": []}},
            ), patch.object(
                run_pre,
                "predict_paths",
                return_value=[path],
            ), patch.object(
                run_pre,
                "predict_statistical_paths",
                return_value=[],
            ), patch.object(run_pre, "simulate_paths") as simulate_paths, patch.object(
                run_pre,
                "render_site",
            ) as render_site, patch.object(run_pre, "publish_site") as publish_site, patch.object(
                run_pre,
                "setup_logger",
                return_value=logger("test.pre.statistical.failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "statistical prediction generation failed"):
                    run_pre.run_pre_flow(config, None, "test-pre-statistical-failure")

            self.assertEqual(load_race_json(path)["prediction"], original_prediction)
            simulate_paths.assert_not_called()
            render_site.assert_not_called()
            publish_site.assert_not_called()

    def test_legacy_import_defaults_to_manual_and_cannot_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data" / "races" / "2026-08-16" / "sapporo_11r.json"
            save_race_json(path, race_payload())
            response_path = root / "manual.json"
            legacy_prediction = valid_prediction()
            for key in ("model_provider", "model_name", "predicted_at"):
                legacy_prediction.pop(key)
            response_path.write_text(
                json.dumps({"meta": {"race_id": "202601010111"}, "prediction": legacy_prediction}),
                encoding="utf-8",
            )
            config = {
                "data_dir": str(root / "data"),
                "llm_provider": "codex",
                "llm_model": "gpt-test",
            }

            with patch.object(
                response_importer,
                "setup_logger",
                return_value=logger("test.import.manual"),
            ):
                imported = response_importer.import_prediction_response(
                    response_path,
                    config,
                    "test-import",
                )
                with self.assertRaisesRegex(ValueError, "prediction already exists"):
                    response_importer.import_prediction_response(
                        response_path,
                        config,
                        "test-import",
                    )

            saved = load_race_json(imported)
            self.assertEqual(saved["prediction"]["model_provider"], "manual")
            self.assertEqual(saved["prediction"]["model_name"], "manual-import")


if __name__ == "__main__":
    unittest.main()
