from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import requests


CODEX_TIMEOUT_SECONDS = 600
PROCESS_CLEANUP_TIMEOUT_SECONDS = 10


def _terminate_codex_process(process: subprocess.Popen) -> str:
    if process.poll() is not None:
        return ""
    issues = []
    tree_terminated = False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            tree_terminated = completed.returncode == 0
            if not tree_terminated:
                issues.append(f"taskkill exit_code={completed.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            issues.append(f"taskkill {type(exc).__name__}")
    if not tree_terminated:
        try:
            process.kill()
        except OSError as exc:
            issues.append(f"kill {type(exc).__name__}")
    try:
        process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            issues.append(f"wait {type(exc).__name__}")
    except OSError as exc:
        issues.append(f"wait {type(exc).__name__}")
    return "; ".join(issues)


def _cli_error_detail(stderr_path: Path, stdout_path: Path) -> str:
    for path in (stderr_path, stdout_path):
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 4096))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if line.strip():
                return line.strip()[-500:]
    return ""


class LLMClient:
    def __init__(self, provider: str, model: str, reasoning_effort: str | None = None) -> None:
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMClient":
        return cls(config["llm_provider"], config["llm_model"], config.get("llm_reasoning_effort"))

    def invoke_json(
        self, prompt: str, output_schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        started = time.monotonic()
        raw = self._invoke_text(prompt, output_schema)
        try:
            return json.loads(self._extract_json(raw))
        except json.JSONDecodeError as exc:
            if self.provider != "codex":
                raise
            raise ValueError(
                f"Codex CLI invalid JSON: model={self.model} effort={self.reasoning_effort or 'default'} "
                f"elapsed={time.monotonic() - started:.1f}s detail={exc.msg}"
            ) from exc

    def _invoke_text(self, prompt: str, output_schema: dict[str, Any] | None = None) -> str:
        if self.provider == "codex":
            return self._invoke_codex(prompt, output_schema)
        if self.provider == "openai":
            return self._invoke_openai(prompt)
        raise ValueError(f"Unsupported llm_provider: {self.provider}")

    def _invoke_codex(self, prompt: str, output_schema: dict[str, Any] | None = None) -> str:
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("Codex CLI is not available on PATH")

        environment = os.environ.copy()
        if not environment.get("HOME") and environment.get("USERPROFILE"):
            environment["HOME"] = environment["USERPROFILE"]
        if not environment.get("CODEX_HOME") and environment.get("USERPROFILE"):
            codex_home = Path(environment["USERPROFILE"]) / ".codex"
            if codex_home.is_dir():
                environment["CODEX_HOME"] = str(codex_home)

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["horses", "optional_summary"],
            "properties": {
                "horses": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["horse_number", "win_probability", "reason"],
                        "properties": {
                            "horse_number": {"type": "integer", "minimum": 1},
                            "win_probability": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "reason": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "optional_summary": {"type": "string", "minLength": 1},
            },
        }
        if output_schema is not None:
            schema = output_schema

        with tempfile.TemporaryDirectory(prefix="keiba-oracle-codex-", ignore_cleanup_errors=True) as directory:
            working_dir = Path(directory)
            schema_path = working_dir / "prediction.schema.json"
            output_path = working_dir / "prediction.json"
            input_path = working_dir / "prompt.txt"
            stdout_path = working_dir / "stdout.txt"
            stderr_path = working_dir / "stderr.txt"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            input_path.write_text(prompt, encoding="utf-8")

            command = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
            ]
            if self.model and self.model != "default":
                command.extend(["--model", self.model])
            if self.reasoning_effort:
                command.extend(
                    ["--config", f"model_reasoning_effort={json.dumps(self.reasoning_effort)}"]
                )
            command.extend(
                [
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
            )

            started = time.monotonic()

            def failure(reason: str) -> RuntimeError:
                detail = _cli_error_detail(stderr_path, stdout_path)
                message = (
                    f"Codex CLI failed: model={self.model} effort={self.reasoning_effort or 'default'} "
                    f"elapsed={time.monotonic() - started:.1f}s {reason}"
                )
                return RuntimeError(f"{message}; detail={detail}" if detail else message)

            failure_reason = None
            with input_path.open("rb") as stdin, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                try:
                    process = subprocess.Popen(
                        command, stdin=stdin, stdout=stdout, stderr=stderr,
                        cwd=working_dir, env=environment,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                except OSError as exc:
                    failure_reason = f"launch {type(exc).__name__}"
                else:
                    try:
                        process.wait(timeout=CODEX_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        cleanup_issue = _terminate_codex_process(process)
                        failure_reason = f"timeout={CODEX_TIMEOUT_SECONDS}s"
                        if cleanup_issue:
                            failure_reason += f"; cleanup={cleanup_issue}"
                    except BaseException:
                        _terminate_codex_process(process)
                        raise
                    else:
                        if process.returncode != 0:
                            failure_reason = f"exit_code={process.returncode}"
            if failure_reason:
                raise failure(failure_reason)
            if not output_path.exists():
                raise failure("prediction response missing")
            return output_path.read_text(encoding="utf-8")

    def _invoke_openai(self, prompt: str) -> str:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": "Return valid JSON only. Do not wrap the JSON in markdown.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_json(raw: str) -> str:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text
