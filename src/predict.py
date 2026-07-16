from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from llm_client import LLMClient
from utils import (
    latest_feedback_summaries,
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    now_jst_iso,
    parse_float,
    parse_int,
    parse_target_date,
    read_text,
    repo_root,
    save_race_json,
    setup_logger,
)


def build_mock_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    scored: list[tuple[int, float, str]] = []
    for horse in payload["horses"]:
        score = 1.0
        reasons: list[str] = []

        odds = horse.get("win_odds")
        popularity = horse.get("popularity")
        recent_runs = horse.get("past_runs") or []
        recent_best = min(
            (run.get("finish_position") for run in recent_runs if run.get("finish_position") is not None),
            default=None,
        )

        if odds is not None:
            score += max(0.0, 12.0 - min(float(odds), 12.0)) / 3.0
            reasons.append(f"単勝{odds}")
        if popularity is not None:
            score += max(0.0, 10.0 - min(float(popularity), 10.0)) / 4.0
            reasons.append(f"{popularity}番人気")
        if recent_best is not None:
            score += max(0.0, 5.0 - min(float(recent_best), 5.0)) / 3.0
            reasons.append(f"近走最高{recent_best}着")
        if horse.get("same_distance_record_summary") and horse["same_distance_record_summary"] != "該当なし":
            score += 0.3
            reasons.append("同距離実績")

        scored.append(
            (
                horse["horse_number"],
                score,
                " / ".join(reasons[:2]) if reasons else "近走比較で上位",
            )
        )

    total_score = sum(item[1] for item in scored) or 1.0
    horses = [
        {
            "horse_number": horse_number,
            "win_probability": round(score / total_score, 6),
            "reason": reason,
        }
        for horse_number, score, reason in sorted(scored, key=lambda item: item[0])
    ]
    return {
        "horses": horses,
        "optional_summary": "mock prediction generated for pipeline verification",
    }


def normalize_prediction_response(response: dict[str, Any], horses: list[dict[str, Any]]) -> dict[str, Any]:
    items = response.get("horses")
    if not isinstance(items, list):
        raise ValueError("prediction response missing horses")

    horse_numbers = [horse["horse_number"] for horse in horses]
    number_to_item: dict[int, dict[str, Any]] = {}
    for item in items:
        horse_number = parse_int(item.get("horse_number"))
        probability = parse_float(item.get("win_probability"))
        reason = str(item.get("reason", "")).strip()
        if horse_number is None or probability is None:
            raise ValueError("invalid horse prediction item")
        if probability < 0:
            raise ValueError("prediction probability must not be negative")
        number_to_item[horse_number] = {
            "horse_number": horse_number,
            "win_probability": max(0.0, min(probability, 1.0)),
            "reason": reason or "比較上位",
        }

    missing = [number for number in horse_numbers if number not in number_to_item]
    if missing:
        raise ValueError(f"missing horse predictions: {missing}")

    total_probability = sum(number_to_item[number]["win_probability"] for number in horse_numbers)
    if total_probability <= 0:
        raise ValueError("prediction total probability is zero")

    normalized_horses = []
    for number in sorted(horse_numbers):
        item = number_to_item[number]
        item["win_probability"] = round(item["win_probability"] / total_probability, 6)
        normalized_horses.append(item)

    rounded_total = round(sum(item["win_probability"] for item in normalized_horses), 6)
    rounding_diff = round(1.0 - rounded_total, 6)
    if normalized_horses and rounding_diff:
        target = normalized_horses[-1]
        if rounding_diff < 0 and target["win_probability"] + rounding_diff < 0:
            target = max(normalized_horses, key=lambda item: item["win_probability"])
        target["win_probability"] = round(target["win_probability"] + rounding_diff, 6)

    return {
        "horses": normalized_horses,
        "optional_summary": str(response.get("optional_summary", "")).strip() or None,
    }


def build_prediction_prompt(config: dict[str, Any], payload: dict[str, Any], root: Path | None = None) -> str:
    root = root or repo_root()
    recent_feedback = latest_feedback_summaries(config, payload["race"].get("date"), 3, root)
    template = read_text(root / "config" / "prompt_prediction.txt")
    context = {
        "race": payload["race"],
        "horses": payload["horses"],
    }
    prompt = template.replace(
        "{{RECENT_FEEDBACK}}",
        json.dumps(recent_feedback, ensure_ascii=False, indent=2),
    )
    prompt = prompt.replace(
        "{{RACE_CONTEXT}}",
        json.dumps(context, ensure_ascii=False, indent=2),
    )
    return prompt


def build_prediction_chat_input(
    config: dict[str, Any],
    payload: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    return {
        "meta": {
            "race_id": payload["meta"].get("race_id"),
            "kind": "prediction",
            "generated_at": now_jst_iso(),
        },
        "race": payload["race"],
        "horses": payload["horses"],
        "recent_feedback_summaries": latest_feedback_summaries(
            config,
            payload["race"].get("date"),
            3,
            root,
        ),
    }


def predict_file(
    path: Path,
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
) -> bool:
    logger = setup_logger(job_name, config, root)
    payload = load_race_json(path)
    if not payload:
        return False

    race_id = payload["meta"].get("race_id")
    if not payload.get("horses"):
        log_job(logger, job_name, race_id, "prediction skipped: horses missing")
        return False

    try:
        if config["llm_provider"] == "mock":
            response = build_mock_prediction(payload)
        else:
            client = LLMClient.from_config(config)
            prompt = build_prediction_prompt(config, payload, root)
            response = client.invoke_json(prompt, max_retries=2)
        prediction = normalize_prediction_response(response, payload["horses"])
        prediction["model_provider"] = config["llm_provider"]
        prediction["model_name"] = config["llm_model"]
        prediction["predicted_at"] = now_jst_iso()
        payload["prediction"] = prediction
        save_race_json(path, payload)
        log_job(logger, job_name, race_id, "prediction updated")
        return True
    except Exception as exc:  # noqa: BLE001
        log_job(logger, job_name, race_id, f"prediction failed: {exc}")
        return False


def predict_paths(
    paths: list[Path],
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
) -> list[Path]:
    updated = []
    for path in paths:
        if predict_file(path, config, job_name, root):
            updated.append(path)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    paths = list_race_files(config, target_date)
    predict_paths(paths, config, "pre")


if __name__ == "__main__":
    main()
