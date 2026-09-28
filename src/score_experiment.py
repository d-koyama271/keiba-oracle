from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from llm_client import LLMClient
from predict import (
    build_prediction_prompt,
    prediction_input_sha256,
    sha256_text,
    validate_prediction_input,
)
from utils import atomic_write_json, data_dir, load_config, now_jst_iso, parse_target_date, read_text, repo_root


SCORE_SCHEMA = {
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
                "required": ["horse_number", "score", "reason"],
                "properties": {
                    "horse_number": {"type": "integer", "minimum": 1},
                    "score": {"type": "number", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
        "optional_summary": {"type": "string", "minLength": 1},
    },
}


def normalize_score_response(response: dict[str, Any], horses: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != {"horses", "optional_summary"}:
        raise ValueError("score response must contain only horses and optional_summary")
    items, summary = response["horses"], response["optional_summary"]
    if not isinstance(items, list) or not isinstance(summary, str) or not summary.strip():
        raise ValueError("score response requires horses and a nonempty summary")
    expected_numbers = {horse["horse_number"] for horse in horses}
    normalized = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"horse_number", "score", "reason"}:
            raise ValueError("invalid score item fields")
        number, score, reason = item["horse_number"], item["score"], item["reason"]
        if type(number) is not int or number not in expected_numbers:
            raise ValueError(f"unexpected score horse_number: {number}")
        if number in normalized:
            raise ValueError(f"duplicate score horse_number: {number}")
        if type(score) not in (int, float) or not 0 <= score <= 100 or not math.isfinite(score):
            raise ValueError(f"invalid score: {number}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"score reason is missing: {number}")
        normalized[number] = {"horse_number": number, "score": score, "reason": reason.strip()}
    if set(normalized) != expected_numbers:
        raise ValueError(f"missing score horses: {sorted(expected_numbers - set(normalized))}")
    return {"horses": [normalized[number] for number in sorted(normalized)], "optional_summary": summary.strip()}


def run_score_experiment(
    config: dict[str, Any],
    race_date: str | None = None,
    race_id: str | None = None,
    force: bool = False,
    root: Path | None = None,
) -> dict[str, int]:
    root = root or repo_root()
    race_date = parse_target_date(race_date) if race_date else None
    input_dir = data_dir(config, root) / "prediction_inputs"
    output_dir = data_dir(config, root) / "experiments" / "score"
    prompt_template = read_text(root / "config" / "prompt_prediction_score.txt")
    client = LLMClient.from_config(config)
    counts = {"success": 0, "skip": 0, "failure": 0}
    for path in sorted(input_dir.glob("*/*.json")):
        if path.name.endswith(".statistical.json") or (race_date and path.parent.name != race_date):
            continue
        try:
            output_path = output_dir / path.relative_to(input_dir)
            existing_output = output_path.exists() and not force
            if race_id or not existing_output:
                prediction_input = json.loads(read_text(path))
                current_id = str((prediction_input.get("meta") or {}).get("race_id") or "")
                if race_id and current_id != race_id:
                    continue
            if existing_output:
                counts["skip"] += 1
                print(f"SKIP {path}")
                continue
            validate_prediction_input(prediction_input, prediction_input, method="general")
            if prediction_input["race"]["date"] != parse_target_date(path.parent.name):
                raise ValueError("prediction input date does not match its directory")
            input_hash = prediction_input_sha256(prediction_input)
            prompt = build_prediction_prompt(config, prediction_input, root, prompt_template)
            response = client.invoke_json(prompt, output_schema=SCORE_SCHEMA)
            score = normalize_score_response(response, prediction_input["horses"])
            atomic_write_json(output_path, {
                "race_id": current_id,
                "generated_at": now_jst_iso(),
                "provider": client.provider,
                "model": client.model,
                "reasoning_effort": client.reasoning_effort,
                "prompt_sha256": sha256_text(prompt_template),
                "prediction_input_sha256": input_hash,
                **score,
            })
            counts["success"] += 1
            print(f"OK {current_id} -> {output_path}")
        except Exception as exc:
            counts["failure"] += 1
            print(f"FAILED {path}: {exc}")
    if not any(counts.values()):
        print("No matching saved general prediction inputs found.")
    print(f"success={counts['success']} skip={counts['skip']} failure={counts['failure']}")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate experimental scores from saved general prediction inputs.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--race-id", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    counts = run_score_experiment(load_config(), args.date, args.race_id, args.force)
    return 1 if counts["failure"] or not any(counts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
