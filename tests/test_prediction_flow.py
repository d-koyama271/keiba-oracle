from __future__ import annotations

import copy
import json
from contextlib import ExitStack
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from logging import NullHandler, getLogger
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import predict  # noqa: E402
import response_importer  # noqa: E402
import run_pre  # noqa: E402
import run_pre_collect  # noqa: E402
import simulate  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from utils import ensure_race_payload, JST, load_config, load_race_json, save_race_json, parse_jst_datetime  # noqa: E402


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
        } if prediction else [],
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
                def invoke_json(self, prompt: str) -> dict:
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
            self.assertEqual(saved["prediction"][-1]["runtime_provider"], "codex")
            self.assertEqual(saved["prediction"][-1]["model"], "gpt-test")
            self.assertEqual(saved["prediction"][-1]["general"]["predicted_at"], "2026-08-15T14:00:00+09:00")
            self.assertEqual(
                saved["prediction"][-1]["general"]["prompt_sha256"],
                predict.sha256_text("Use only this JSON and return JSON: {{RACE_CONTEXT}}"),
            )
            self.assertEqual(
                saved["prediction"][-1]["general"]["prediction_input_sha256"],
                predict.prediction_input_sha256(prediction_input),
            )
            self.assertEqual(len(saved["prediction"][-1]["general"]["prompt_sha256"]), 64)
            self.assertEqual(len(saved["prediction"][-1]["general"]["prediction_input_sha256"]), 64)
            self.assertAlmostEqual(
                sum(item["win_probability"] for item in saved["prediction"][-1]["general"]["horses"]),
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
            self.assertNotIn("prompt_sha256", load_race_json(path)["prediction"][0]["general"])
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
                def invoke_json(self, prompt: str) -> dict:
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
            self.assertEqual(saved["meta"]["schema_version"], 10)
            self.assertEqual(
                set(saved),
                {"meta", "race", "horses", "prediction", "simulation", "result", "evaluation"},
            )
            self.assertEqual(saved["prediction"][0]["general"], ensure_race_payload({"prediction": traditional})["prediction"][0]["general"])
            statistical = saved["prediction"][0]["statistical"]
            self.assertEqual(saved["prediction"][0]["runtime_provider"], "codex")
            self.assertEqual(saved["prediction"][0]["model"], "gpt-test")
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

    def test_statistical_recovery_after_start_uses_explicit_input_and_actual_timestamp(self) -> None:
        for timestamp in (None, "2026-08-16T16:00:00+09:00"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config").mkdir()
                (root / "config" / "prompt_prediction_statistical.txt").write_text(
                    "Use this input: {{RACE_CONTEXT}}", encoding="utf-8",
                )
                path = root / "race.json"
                payload = race_payload()
                frozen = predict.build_statistical_prediction_input(payload)
                if timestamp is not None:
                    frozen["meta"]["generated_at"] = timestamp
                save_race_json(path, payload)
                config = load_config()
                client = Mock()
                client.invoke_json.return_value = {k: v for k, v in valid_prediction().items()
                                                   if k in ("horses", "optional_summary")}
                generated_at = "2026-08-16T16:00:00+09:00"
                with patch.object(predict, "setup_logger", return_value=logger("test.recovery")), \
                     patch.object(predict, "now_jst", return_value=datetime.fromisoformat(generated_at)), \
                     patch.object(predict, "now_jst_iso", return_value=generated_at), \
                     patch.object(predict.LLMClient, "from_config", return_value=client):
                    self.assertTrue(predict.predict_statistical_file(path, config, "test-recovery", root, frozen))
                saved = load_race_json(path)
                statistical = saved["prediction"][0]["statistical"]
                self.assertEqual(statistical["predicted_at"], generated_at)
                self.assertEqual(statistical["prediction_input_sha256"], predict.prediction_input_sha256(frozen))
                self.assertEqual(saved["race"], payload["race"])
                self.assertEqual(saved["horses"], payload["horses"])
                self.assertIsNone(saved["result"])

    def test_statistical_recovery_rejects_missing_or_inconsistent_input_and_existing_result(self) -> None:
        for case in ("unspecified", "result", "empty_result", "race_id", "race", "horses", "empty_input"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "race.json"
                payload = race_payload()
                frozen = predict.build_statistical_prediction_input(payload)
                if case == "unspecified":
                    frozen = None
                elif case == "result":
                    payload["result"] = {"horses": [{"horse_number": 1, "finish_position": 1}]}
                elif case == "empty_result":
                    payload["result"] = {}
                elif case == "race_id":
                    frozen["meta"]["race_id"] = "wrong-race"
                elif case == "race":
                    frozen["race"]["date"] = "2026-08-23"
                elif case == "horses":
                    frozen["horses"][0]["horse_number"] = frozen["horses"][1]["horse_number"]
                else:
                    frozen = {}
                save_race_json(path, payload)
                before = path.read_bytes()
                with patch.object(predict, "setup_logger", return_value=logger("test.recovery.reject")), \
                     patch.object(predict, "now_jst", return_value=datetime(2026, 8, 16, 16, 0, tzinfo=JST)), \
                     patch.object(predict.LLMClient, "from_config") as client:
                    self.assertFalse(predict.predict_statistical_file(path, load_config(), "test-recovery", Path(directory), frozen))
                    client.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

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
    CONFIG = {"llm_provider": "codex", "llm_model": "gpt-test", "llm_reasoning_effort": "high"}

    def test_from_config_sets_reasoning_effort(self) -> None:
        client = LLMClient.from_config(self.CONFIG)
        self.assertEqual(client.provider, "codex")
        self.assertEqual(client.model, self.CONFIG["llm_model"])
        self.assertEqual(client.reasoning_effort, self.CONFIG["llm_reasoning_effort"])

    def test_from_config_without_reasoning_effort(self) -> None:
        client = LLMClient.from_config({"llm_provider": "codex", "llm_model": "gpt-test"})
        self.assertIsNone(client.reasoning_effort)

    def _invoke_codex_with_environment(self, environment: dict[str, str]) -> tuple[list[str], dict, dict]:
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

        with patch.dict(
            "llm_client.os.environ",
            environment,
            clear=True,
        ), patch("llm_client.shutil.which", return_value="codex"), patch(
            "llm_client.subprocess.run",
            side_effect=run,
        ):
            response = LLMClient.from_config(self.CONFIG).invoke_json(
                "ONLY_INPUT",
            )

        return commands[0], run_kwargs, response

    def test_codex_cli_is_isolated_and_uses_structured_output(self) -> None:
        command, run_kwargs, response = self._invoke_codex_with_environment(
            {"USERPROFILE": r"C:\Users\runner"}
        )

        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], self.CONFIG["llm_model"])
        self.assertEqual(command[command.index("--config") + 1], f'model_reasoning_effort="{self.CONFIG["llm_reasoning_effort"]}"')
        self.assertIn("--output-schema", command)
        self.assertEqual(run_kwargs["input"], "ONLY_INPUT")
        self.assertEqual(run_kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.assertNotEqual(Path(run_kwargs["cwd"]), ROOT)
        self.assertEqual(run_kwargs["env"]["HOME"], r"C:\Users\runner")
        self.assertNotIn("CODEX_HOME", run_kwargs["env"])
        self.assertEqual(response["horses"][0]["horse_number"], 1)

    def test_codex_failure_or_invalid_json_is_not_retried(self):
        for invalid_json in (False, True):
            with self.subTest(invalid_json=invalid_json):
                def run(command, **kwargs):
                    if invalid_json:
                        Path(command[command.index("--output-last-message") + 1]).write_text("invalid JSON")
                        return subprocess.CompletedProcess(command, 0, "", "")
                    return subprocess.CompletedProcess(command, 1, "", "failed")
                with patch("llm_client.shutil.which", return_value="codex"), \
                     patch("llm_client.subprocess.run", side_effect=run) as invoke:
                    with self.assertRaises((RuntimeError, ValueError)):
                        LLMClient.from_config(self.CONFIG).invoke_json("test")
                    self.assertEqual(invoke.call_count, 1)

    def test_codex_cli_supplements_empty_home_from_userprofile(self) -> None:
        _, run_kwargs, _ = self._invoke_codex_with_environment(
            {"USERPROFILE": r"C:\Users\runner", "HOME": ""}
        )

        self.assertEqual(run_kwargs["env"]["HOME"], r"C:\Users\runner")
        self.assertNotIn("CODEX_HOME", run_kwargs["env"])

    def test_codex_cli_uses_existing_profile_directory_only_when_needed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            candidate = profile / ".codex"
            for exists in (False, True):
                if exists:
                    candidate.mkdir()
                for codex_home in (None, "", "existing-home"):
                    with self.subTest(exists=exists, codex_home=codex_home):
                        environment = {"USERPROFILE": str(profile)}
                        if codex_home is not None:
                            environment["CODEX_HOME"] = codex_home
                        before = dict(environment)
                        _, kwargs, _ = self._invoke_codex_with_environment(environment)
                        expected = codex_home if codex_home else (str(candidate) if exists else codex_home)
                        self.assertEqual(kwargs["env"].get("CODEX_HOME"), expected)
                        self.assertEqual(candidate.is_dir(), exists)
                        self.assertEqual(environment, before)

    def test_codex_cli_keeps_existing_home_and_codex_home(self) -> None:
        _, run_kwargs, _ = self._invoke_codex_with_environment(
            {
                "USERPROFILE": r"C:\Users\runner",
                "HOME": r"C:\custom-home",
                "CODEX_HOME": r"C:\custom-codex-home",
            }
        )

        self.assertEqual(run_kwargs["env"]["HOME"], r"C:\custom-home")
        self.assertEqual(run_kwargs["env"]["CODEX_HOME"], r"C:\custom-codex-home")


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
            ):
                exported = run_pre_collect.export_prediction_chat_input([path], {"llm_provider": "codex", "llm_model": "gpt-test"}, "test-export")

            self.assertEqual(exported, [])
            self.assertEqual(path.read_bytes(), before)

    def test_normal_pre_flow_generates_or_reuses_predictions_then_publishes(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                config = load_config()
                config.update(data_dir=str(root / "data"), public_dir=str(root / "public"),
                              llm_provider="codex", llm_model="gpt-test")
                config.pop("llm_reasoning_effort", None)
                config["simulation"]["dutching"].update(max_selection_count=2, min_coverage_probability=0,
                                                        min_group_expected_value=0)
                path = root / "data/races/2026-08-16/sapporo_11r.json"
                original = valid_prediction() if reuse else None
                payload = race_payload(original)
                save_race_json(path, payload)
                input_path = root / "input.json"
                input_path.write_text(json.dumps(predict.build_prediction_chat_input(config, payload)), encoding="utf-8")
                stack.enter_context(patch.object(run_pre, "run_pre_collect_flow", return_value=([path], [] if reuse else [input_path])))
                for module in (run_pre, predict, simulate):
                    stack.enter_context(patch.object(module, "setup_logger", return_value=logger("normal-pre")))
                stack.enter_context(patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")))
                stack.enter_context(patch.object(predict, "now_jst_iso", return_value="2026-08-15T15:00:00+09:00"))
                def response(prompt):
                    statistical = '\"method\": \"statistical\"' in prompt
                    probability = .25 if statistical else .65
                    return {"horses": [{"horse_number": number, "win_probability": p, "reason": "condition"}
                                       for number, p in ((1, probability), (2, 1 - probability))],
                            "optional_summary": "Conditions compared"}
                client = Mock()
                client.invoke_json.side_effect = response
                stack.enter_context(patch.object(predict.LLMClient, "from_config", return_value=client))
                render = stack.enter_context(patch.object(run_pre, "render_site"))
                publish = stack.enter_context(patch.object(run_pre, "publish_site", return_value=root / "public"))
                self.assertEqual(run_pre.run_pre_flow(config, None, "normal-pre"), [path])
                saved = load_race_json(path)
                self.assertEqual(len(saved["prediction"]), 1)
                entry = saved["prediction"][0]
                self.assertEqual((entry["runtime_provider"], entry["model"]), ("codex", "gpt-test"))
                if reuse:
                    self.assertEqual(entry["general"], ensure_race_payload({"prediction": original})["prediction"][0]["general"])
                else:
                    self.assertEqual(entry["general"]["predicted_at"], "2026-08-15T15:00:00+09:00")
                for method, probabilities in (("general", [.6, .4] if reuse else [.65, .35]), ("statistical", [.25, .75])):
                    self.assertEqual([h["win_probability"] for h in entry[method]["horses"]], probabilities)
                    for purchase in ("value", "dutching"):
                        self.assertIsNotNone(saved["simulation"][0][method]["win"][purchase]["pre"])
                    for selection in saved["simulation"][0][method]["win"]["value"]["pre"]["selections"]:
                        self.assertEqual(selection["predicted_probability"], probabilities[selection["horse_number"] - 1])
                self.assertEqual(client.invoke_json.call_count, 1 if reuse else 2)
                self.assertEqual(saved["meta"]["pre_status"], "awaiting_prediction")
                self.assertIsNone(saved["result"])
                self.assertEqual(saved["evaluation"], [])
                render.assert_called_once()
                publish.assert_called_once()

    def test_statistical_then_general_phases_collect_separately_and_preserve_predictions(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config = load_config()
            config.update(data_dir=str(root / "data"), public_dir=str(root / "public"))
            path = root / "data/races/2026-08-16/test_11r.json"
            collections = []

            def collect(config, target_date, job_name):
                payload = load_race_json(path) or race_payload()
                payload["horses"][0]["win_odds"] = 2.0 + len(collections)
                save_race_json(path, payload)
                collections.append(payload["horses"][0]["win_odds"])
                return "2026-08-16", [path]

            client = Mock()
            client.invoke_json.return_value = {k: v for k, v in valid_prediction().items()
                                               if k in ("horses", "optional_summary")}
            for module in ("run_pre", "run_pre_collect", "predict", "simulate"):
                stack.enter_context(patch(f"{module}.setup_logger", return_value=logger(f"phase-{module}")))
            stack.enter_context(patch.object(run_pre_collect, "collect_pre_races", side_effect=collect))
            stack.enter_context(patch.object(predict.LLMClient, "from_config", return_value=client))
            stack.enter_context(patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")))

            with patch.object(run_pre_collect, "export_prediction_chat_input") as export, \
                 patch.object(run_pre, "predict_paths") as general, \
                 patch.object(run_pre, "simulate_paths") as simulate:
                self.assertEqual(run_pre.run_pre_flow(config, "2026-08-16", phase="statistical"), [path])
                export.assert_not_called()
                general.assert_not_called()
                simulate.assert_not_called()
            first = load_race_json(path)
            self.assertNotIn("general", first["prediction"][0])
            self.assertEqual(first["simulation"], [])
            self.assertFalse((root / "data/prediction_inputs/2026-08-16/test_11r.json").exists())
            self.assertTrue((root / "data/prediction_inputs/2026-08-16/test_11r.statistical.json").exists())
            self.assertTrue((root / "public/index.html").exists())

            with patch.object(run_pre, "predict_statistical_paths") as statistical, \
                 patch.object(run_pre, "build_pending_statistical_inputs") as statistical_input:
                self.assertEqual(run_pre.run_pre_flow(config, "2026-08-16", phase="general"), [path])
                statistical.assert_not_called()
                statistical_input.assert_not_called()
            second = load_race_json(path)
            self.assertEqual(second["prediction"][0]["statistical"], first["prediction"][0]["statistical"])
            self.assertIn("general", second["prediction"][0])
            for method in ("general", "statistical"):
                self.assertIsNotNone(second["simulation"][0][method]["win"]["value"]["pre"])
                self.assertIsNotNone(second["simulation"][0][method]["win"]["dutching"]["pre"])
            finalized = json.loads((root / "data/prediction_inputs/2026-08-16/test_11r.json").read_text(encoding="utf-8"))
            self.assertEqual(finalized["horses"][0]["win_odds"], 3.0)

            with patch.object(run_pre_collect, "export_prediction_chat_input") as export, \
                 patch.object(run_pre, "predict_paths") as general, \
                 patch.object(run_pre, "simulate_paths") as simulate:
                run_pre.run_pre_flow(config, "2026-08-16", phase="statistical")
                export.assert_not_called()
                general.assert_not_called()
                simulate.assert_not_called()
            third = load_race_json(path)
            self.assertEqual(third["prediction"], second["prediction"])
            self.assertEqual(third["simulation"], second["simulation"])
            self.assertEqual(collections, [2.0, 3.0, 4.0])
            with patch.object(predict.LLMClient, "from_config", side_effect=AssertionError("saved predictions must be reused")), \
                 patch.object(run_pre, "predict_statistical_paths") as statistical:
                run_pre.run_pre_flow(config, "2026-08-16", phase="general")
                statistical.assert_not_called()
            reused = load_race_json(path)
            self.assertEqual(reused["prediction"], second["prediction"])
            self.assertEqual(reused["simulation"], second["simulation"])
            self.assertEqual(collections, [2.0, 3.0, 4.0, 5.0])

    def test_resume_reuses_saved_input_after_failure_without_collection(self):
        for phase in ("general", "statistical"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                config = load_config()
                config.update(data_dir=str(root / "data"), public_dir=str(root / "public"))
                path = root / "data/races/2026-08-16/test_11r.json"
                save_race_json(path, race_payload())
                for module in ("run_pre", "run_pre_collect", "predict", "simulate"):
                    stack.enter_context(patch(f"{module}.setup_logger", return_value=logger(f"resume-{module}")))
                collect = stack.enter_context(patch.object(run_pre_collect, "collect_pre_races", return_value=("2026-08-16", [path])))
                clock = stack.enter_context(patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-15T18:00:00+09:00" if phase == "statistical" else "2026-08-16T12:00:00+09:00")))
                client = Mock()
                client.invoke_json.side_effect = RuntimeError("test runtime failure")
                stack.enter_context(patch.object(predict.LLMClient, "from_config", return_value=client))
                with self.assertRaisesRegex(RuntimeError, "prediction generation failed"):
                    run_pre.run_pre_flow(config, "2026-08-16", phase=phase)
                collect.assert_called_once()
                suffix = ".statistical.json" if phase == "statistical" else ".json"
                input_path = root / f"data/prediction_inputs/2026-08-16/test_11r{suffix}"
                frozen_bytes = input_path.read_bytes()
                frozen = json.loads(frozen_bytes)
                failed_prompt = client.invoke_json.call_args.args[0]
                self.assertIn(json.dumps(frozen, ensure_ascii=False, indent=2), failed_prompt)
                updated = load_race_json(path)
                updated["race"]["weather"] = "updated after snapshot"
                updated["horses"][0]["horse_name"] = "updated after snapshot"
                updated["horses"][0]["win_odds"] = 99.0
                save_race_json(path, updated)
                collect.side_effect = AssertionError("resume must not collect")
                clock.return_value = parse_jst_datetime("2026-08-16T16:00:00+09:00")
                client.invoke_json.side_effect = None
                client.invoke_json.return_value = {k: v for k, v in valid_prediction().items()
                                                   if k in ("horses", "optional_summary")}
                with patch.object(run_pre, "build_pending_statistical_inputs", side_effect=AssertionError("resume must not rebuild input")):
                    self.assertEqual(run_pre.run_pre_flow(config, "2026-08-16", phase=phase, resume=True), [path])
                self.assertEqual(client.invoke_json.call_args.args[0], failed_prompt)
                self.assertEqual(input_path.read_bytes(), frozen_bytes)
                saved = load_race_json(path)
                self.assertEqual(saved["prediction"][0][phase]["prediction_input_sha256"], predict.prediction_input_sha256(frozen))
                client.invoke_json.side_effect = AssertionError("saved prediction must be reused")
                with patch.object(run_pre, "publish_site", wraps=run_pre.publish_site) as publish:
                    self.assertEqual(run_pre.run_pre_flow(config, "2026-08-16", phase=phase, resume=True), [path])
                    publish.assert_called_once()
                self.assertEqual(load_race_json(path)["prediction"], saved["prediction"])
                input_path.unlink()
                before = path.read_bytes()
                with self.assertRaisesRegex(FileNotFoundError, "Saved prediction input missing"):
                    run_pre.run_pre_flow(config, "2026-08-16", phase=phase, resume=True)
                self.assertEqual(path.read_bytes(), before)

    def test_invalid_saved_input_is_not_rebuilt_or_overwritten(self):
        from utils import prediction_input_path, atomic_write_json
        for method in ("general", "statistical"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                for module in (run_pre, run_pre_collect, predict):
                    stack.enter_context(patch.object(module, "setup_logger", return_value=logger("invalid-input")))
                root = Path(tmp)
                config = load_config()
                config.update(data_dir=str(root / "data"), public_dir=str(root / "public"))
                payload = race_payload()
                path = root / "data/races/2026-08-16/test_11r.json"
                save_race_json(path, payload)
                snapshot = predict.build_statistical_prediction_input(payload) if method == "statistical" else predict.build_prediction_chat_input(config, payload)
                snapshot["meta"]["race_id"] = "old-race"
                input_path = prediction_input_path(config, path, method)
                atomic_write_json(input_path, snapshot)
                before = input_path.read_bytes()
                with patch.object(run_pre, "run_pre_collect_flow") as collect, patch.object(predict.LLMClient, "from_config") as client:
                    with self.assertRaisesRegex(RuntimeError, "race IDs do not match"):
                        run_pre.run_pre_flow(config, "2026-08-16", phase=method, resume=True)
                    collect.assert_not_called()
                    client.assert_not_called()
                self.assertEqual(input_path.read_bytes(), before)
                if method == "general":
                    with self.assertRaisesRegex(ValueError, "race_id mismatch"):
                        run_pre_collect.export_prediction_chat_input([path], config, "invalid-input")
                else:
                    with patch.object(run_pre_collect, "collect_pre_races", return_value=("2026-08-16", [path])), \
                         patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")), \
                         patch.object(predict.LLMClient, "from_config") as client:
                        with self.assertRaises(RuntimeError):
                            run_pre.run_pre_flow(config, "2026-08-16", phase=method)
                        client.assert_not_called()
                self.assertEqual(input_path.read_bytes(), before)

    def test_resume_rejects_all_phase_or_missing_date(self):
        for phase, date in (("all", "2026-08-16"), ("statistical", None), ("general", None)):
            with self.subTest(phase=phase, date=date), patch.object(run_pre, "run_pre_collect_flow") as collect:
                with self.assertRaisesRegex(ValueError, "resume requires"):
                    run_pre.run_pre_flow({}, date, phase=phase, resume=True)
                collect.assert_not_called()

    def test_race_id_limits_pre_and_resume_without_touching_other_race(self):
        from utils import race_html_path, race_result_html_path

        for phase in ("statistical", "general", "all"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                config = load_config()
                config.update(data_dir=str(root / "data"), public_dir=str(root / "public"))
                target = root / "data/races/2026-08-16/sapporo_11r.json"
                other = target.with_name("hakodate_11r.json")
                payload = race_payload()
                race_id = payload["meta"]["race_id"]
                save_race_json(target, payload)
                other_payload = race_payload(valid_prediction())
                other_payload["meta"]["race_id"] = "202602010111"
                other_payload["race"]["track"] = "函館"
                save_race_json(other, other_payload)
                before = other.read_bytes()
                outbox = root / "data/prediction_inputs/2026-08-16"
                outbox.mkdir(parents=True)
                other_input = outbox / "hakodate_11r.json"
                other_input.write_bytes(b"unrelated input must not be read or changed")
                other_pages = [root / "public" / factory("2026-08-16", "函館", 11)
                               for factory in (race_html_path, race_result_html_path)]
                for page in other_pages:
                    page.parent.mkdir(parents=True, exist_ok=True)
                    page.write_bytes(b"existing unrelated HTML")
                for module in ("run_pre", "run_pre_collect", "predict", "simulate"):
                    stack.enter_context(patch(f"{module}.setup_logger", return_value=logger(f"single-{module}")))
                collect = stack.enter_context(patch.object(run_pre_collect, "collect_races", return_value=[target]))
                select = stack.enter_context(patch.object(run_pre_collect, "select_default_races"))
                client = Mock()
                client.invoke_json.return_value = {k: v for k, v in valid_prediction().items()
                                                   if k in ("horses", "optional_summary")}
                stack.enter_context(patch.object(predict.LLMClient, "from_config", return_value=client))
                stack.enter_context(patch.object(predict, "now_jst", return_value=parse_jst_datetime("2026-08-16T12:00:00+09:00")))
                self.assertEqual(run_pre.run_pre_flow(config, "2026-08-16", phase=phase, race_id=race_id), [target])
                collect.assert_called_once_with(config, "pre", "2026-08-16", "pre", selected_race_ids=[race_id])
                select.assert_not_called()
                self.assertEqual(client.invoke_json.call_count, 2 if phase == "all" else 1)
                collect.side_effect = AssertionError("resume must not collect")
                client.invoke_json.side_effect = AssertionError("existing prediction must be reused")
                resume_phase = "general" if phase == "all" else phase
                self.assertEqual(run_pre.run_pre_flow(config, "2026-08-16", phase=resume_phase, resume=True, race_id=race_id), [target])
                self.assertEqual(other.read_bytes(), before)
                self.assertEqual(other_input.read_bytes(), b"unrelated input must not be read or changed")
                for page in other_pages:
                    self.assertEqual(page.read_bytes(), b"existing unrelated HTML")
                with self.assertRaises(FileNotFoundError):
                    run_pre.run_pre_flow(config, "2026-08-16", phase=resume_phase, resume=True, race_id="202601010199")
                collect.side_effect = None
                collect.return_value = []
                with self.assertRaisesRegex(SystemExit, "No race JSON updated"):
                    run_pre.run_pre_flow(config, "2026-08-16", phase=phase, race_id="202601010199")

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
                    self.assertEqual(saved["meta"]["pre_status"], "awaiting_prediction")
                else:
                    self.assertEqual(saved["prediction"], [])
                    self.assertEqual(saved["meta"]["pre_status"], "awaiting_prediction")
            public = root / "public"
            self.assertEqual(len(list((public / "races").rglob("*.html"))), 3)
            before = (public / "index.html").read_bytes()
            collected.return_value = ([paths[-1]], [inputs[-1]])
            with self.assertRaisesRegex(RuntimeError, "failed for both methods"):
                run_pre.run_pre_flow(config, None, "failed")
            self.assertEqual((public / "index.html").read_bytes(), before)


    def test_pre_cli_phase_defaults_to_all_and_accepts_each_phase(self):
        for phase, resume, race_id in ((None, False, None), ("all", False, None),
                                       ("statistical", False, None), ("general", False, None),
                                       ("general", True, "202601010111")):
            with self.subTest(phase=phase, resume=resume):
                argv = ["run_pre.py", "--date", "2026-08-16"]
                if phase is not None:
                    argv += ["--phase", phase]
                options = {"phase": phase or "all", "resume": resume}
                if resume:
                    argv += ["--resume", "--race-id", race_id]
                    options["race_id"] = race_id
                with patch("sys.argv", argv), patch.object(run_pre, "load_config", return_value={}), \
                     patch.object(run_pre, "run_pre_flow") as flow:
                    run_pre.main()
                    flow.assert_called_once_with({}, "2026-08-16", **options)

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
            self.assertEqual(saved["prediction"][-1]["runtime_provider"], "manual")
            self.assertEqual(saved["prediction"][-1]["model"], "manual-import")


if __name__ == "__main__":
    unittest.main()
