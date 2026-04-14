from __future__ import annotations

import json
import os
from typing import Any

import requests


class LLMClient:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMClient":
        return cls(config["llm_provider"], config["llm_model"])

    def invoke_json(self, prompt: str, max_retries: int = 2) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(max_retries + 1):
            try:
                raw = self._invoke_text(prompt)
                return json.loads(self._extract_json(raw))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"Failed to parse LLM JSON response: {last_error}") from last_error

    def _invoke_text(self, prompt: str) -> str:
        if self.provider == "openai":
            return self._invoke_openai(prompt)
        raise ValueError(f"Unsupported llm_provider: {self.provider}")

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
