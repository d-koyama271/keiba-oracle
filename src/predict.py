from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from llm_client import LLMClient
from utils import (
    STATISTICAL_PREDICTION_METHOD,
    find_variant,
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    now_jst,
    now_jst_iso,
    parse_float,
    parse_int,
    parse_target_date,
    read_text,
    race_start_datetime,
    repo_root,
    save_race_json,
    set_race_status,
    setup_logger,
    prediction_variants,
)

STATISTICAL_PROMPT_FILE = "prompt_prediction_statistical.txt"
STATISTICAL_FORBIDDEN_OUTPUT_TERMS = ("市場", "オッズ", "人気", "market", "odds", "popularity")
STATISTICAL_EXCLUDED_FIELD_NAMES = {
    "source_url",
}
STATISTICAL_AUDIT_FIELDS = (
    "method",
    "model_provider",
    "model_name",
    "predicted_at",
    "prompt_sha256",
    "prediction_input_sha256",
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
        distance_record = (horse.get("career_summaries") or {}).get("current_distance_band_record") or {}
        if distance_record.get("runs", 0) > 0:
            score += 0.3
            reasons.append("近似距離実績")

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
    expected_numbers = set(horse_numbers)
    number_to_item: dict[int, dict[str, Any]] = {}
    for item in items:
        horse_number = parse_int(item.get("horse_number"))
        probability = parse_float(item.get("win_probability"))
        reason = str(item.get("reason", "")).strip()
        if horse_number is None or probability is None:
            raise ValueError("invalid horse prediction item")
        if horse_number not in expected_numbers:
            raise ValueError(f"unexpected horse prediction: {horse_number}")
        if horse_number in number_to_item:
            raise ValueError(f"duplicate horse prediction: {horse_number}")
        if probability < 0:
            raise ValueError("prediction probability must not be negative")
        if not reason:
            raise ValueError(f"prediction reason is missing: {horse_number}")
        number_to_item[horse_number] = {
            "horse_number": horse_number,
            "win_probability": max(0.0, min(probability, 1.0)),
            "reason": reason,
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


def build_prediction_prompt(
    config: dict[str, Any],
    prediction_input: dict[str, Any],
    root: Path | None = None,
    prompt_template: str | None = None,
) -> str:
    root = root or repo_root()
    template = (
        prompt_template
        if prompt_template is not None
        else read_text(root / "config" / "prompt_prediction.txt")
    )
    prompt = template.replace(
        "{{RACE_CONTEXT}}",
        json.dumps(prediction_input, ensure_ascii=False, indent=2),
    )
    return prompt


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prediction_input_sha256(prediction_input: dict[str, Any]) -> str:
    stable_input = dict(prediction_input)
    stable_meta = dict(prediction_input.get("meta") or {})
    stable_meta.pop("generated_at", None)
    stable_input["meta"] = stable_meta
    serialized = json.dumps(
        stable_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256_text(serialized)


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
    }


def is_statistical_excluded_field(key: str) -> bool:
    normalized = key.lower()
    return (
        "odds" in normalized
        or "market" in normalized
        or "popularity" in normalized
        or normalized in STATISTICAL_EXCLUDED_FIELD_NAMES
    )


def without_market_information(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_market_information(item)
            for key, item in value.items()
            if not is_statistical_excluded_field(str(key))
        }
    if isinstance(value, list):
        return [without_market_information(item) for item in value]
    return copy.deepcopy(value)


def build_statistical_prediction_input(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": {
            "race_id": payload["meta"].get("race_id"),
            "kind": "prediction",
            "method": STATISTICAL_PREDICTION_METHOD,
        },
        "race": without_market_information(payload.get("race") or {}),
        "horses": without_market_information(payload.get("horses") or []),
    }


def validate_statistical_prediction_input(
    prediction_input: dict[str, Any],
    race_payload: dict[str, Any],
) -> None:
    if set(prediction_input) != {"meta", "race", "horses"}:
        raise ValueError("statistical prediction input must contain only meta, race, and horses")
    meta = prediction_input.get("meta") or {}
    if meta.get("method") != STATISTICAL_PREDICTION_METHOD:
        raise ValueError("statistical prediction input method is invalid")
    expected = build_statistical_prediction_input(race_payload)
    if prediction_input != expected:
        raise ValueError("statistical prediction input does not match sanitized race JSON")


def ensure_statistical_prediction_is_pre_race(payload: dict[str, Any]) -> None:
    if payload.get("result") is not None:
        raise ValueError("statistical prediction cannot be generated after result collection")
    race = payload.get("race") or {}
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    if start is None:
        raise ValueError("statistical prediction requires a race start datetime")
    if now_jst() >= start:
        raise ValueError("statistical prediction cannot be generated after race start")


def validate_statistical_prediction_text(prediction: dict[str, Any]) -> None:
    texts = [str(item.get("reason") or "") for item in prediction.get("horses", [])]
    texts.append(str(prediction.get("optional_summary") or ""))
    for value in texts:
        lowered = value.lower()
        if any(term in lowered for term in STATISTICAL_FORBIDDEN_OUTPUT_TERMS):
            raise ValueError("statistical prediction contains market-related wording")


def validate_statistical_prediction_metadata(prediction: dict[str, Any]) -> None:
    missing = [key for key in STATISTICAL_AUDIT_FIELDS if not prediction.get(key)]
    if missing:
        raise ValueError(f"statistical prediction audit fields are missing: {missing}")
    if not prediction.get("optional_summary"):
        raise ValueError("statistical prediction summary is missing")


def validate_prediction_input(
    prediction_input: dict[str, Any],
    race_payload: dict[str, Any],
) -> None:
    if set(prediction_input) != {"meta", "race", "horses"}:
        raise ValueError("prediction input must contain only meta, race, and horses")

    race_id = str((prediction_input.get("meta") or {}).get("race_id") or "")
    if race_id != str(race_payload["meta"].get("race_id") or ""):
        raise ValueError("prediction input race_id does not match race JSON")
    if prediction_input.get("race") != race_payload.get("race"):
        raise ValueError("prediction input race does not match race JSON")
    if prediction_input.get("horses") != race_payload.get("horses"):
        raise ValueError("prediction input horses do not match race JSON")


def load_prediction_inputs(paths: list[Path]) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        race_id = str((payload.get("meta") or {}).get("race_id") or "")
        if not race_id:
            raise ValueError(f"prediction input missing race_id: {path}")
        if race_id in inputs:
            raise ValueError(f"duplicate prediction input race_id: {race_id}")
        inputs[race_id] = payload
    return inputs


def generate_prediction(
    config: dict[str, Any],
    prediction_input: dict[str, Any],
    horses: list[dict[str, Any]],
    prompt_file: str,
    root: Path | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    prompt_template = None
    if config["llm_provider"] == "mock":
        response = build_mock_prediction(prediction_input)
    else:
        client = LLMClient.from_config(config)
        prediction_root = root or repo_root()
        prompt_template = read_text(prediction_root / "config" / prompt_file)
        prompt = build_prediction_prompt(
            config,
            prediction_input,
            root,
            prompt_template,
        )
        response = client.invoke_json(prompt, max_retries=2)

    prediction = normalize_prediction_response(response, horses)
    if config["llm_provider"] == "codex" and not prediction.get("optional_summary"):
        raise ValueError("Codex prediction summary is missing")
    prediction["model_provider"] = config["llm_provider"]
    prediction["model_name"] = config["llm_model"]
    prediction["predicted_at"] = now_jst_iso()
    if method is not None:
        prediction["method"] = method
    if prompt_template is not None:
        prediction["prompt_sha256"] = sha256_text(prompt_template)
        prediction["prediction_input_sha256"] = prediction_input_sha256(prediction_input)
    return prediction


def predict_file(
    path: Path,
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
    prediction_input: dict[str, Any] | None = None,
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
        if payload.get("prediction"):
            normalize_prediction_response(payload["prediction"], payload["horses"])
            log_job(logger, job_name, race_id, "prediction reused: existing prediction is valid")
            return True

        prediction_input = prediction_input or build_prediction_chat_input(config, payload, root)
        validate_prediction_input(prediction_input, payload)
        prediction = generate_prediction(
            config,
            prediction_input,
            payload["horses"],
            "prompt_prediction.txt",
            root,
        )
        payload["prediction"] = prediction
        set_race_status(payload, pre_status="prediction_imported")
        save_race_json(path, payload)
        log_job(logger, job_name, race_id, "prediction updated")
        return True
    except Exception as exc:  # noqa: BLE001
        log_job(logger, job_name, race_id, f"prediction failed: {exc}")
        return False


def predict_statistical_file(
    path: Path,
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
    prediction_input: dict[str, Any] | None = None,
) -> bool:
    logger = setup_logger(job_name, config, root)
    payload = load_race_json(path)
    if not payload:
        return False

    race_id = payload["meta"].get("race_id")
    prediction = payload.get("prediction")
    if not isinstance(prediction, dict) or not payload.get("horses"):
        log_job(logger, job_name, race_id, "statistical prediction skipped: traditional prediction or horses missing")
        return False

    try:
        existing = find_variant(
            prediction_variants(payload),
            STATISTICAL_PREDICTION_METHOD,
            config["llm_provider"],
            config["llm_model"],
        )
        if existing is not None:
            normalize_prediction_response(existing, payload["horses"])
            validate_statistical_prediction_text(existing)
            validate_statistical_prediction_metadata(existing)
            log_job(logger, job_name, race_id, "statistical prediction reused: existing prediction is valid")
            return True

        ensure_statistical_prediction_is_pre_race(payload)
        prediction_input = prediction_input or build_statistical_prediction_input(payload)
        validate_statistical_prediction_input(prediction_input, payload)
        statistical = generate_prediction(
            config,
            prediction_input,
            payload["horses"],
            STATISTICAL_PROMPT_FILE,
            root,
            STATISTICAL_PREDICTION_METHOD,
        )
        if not statistical.get("prompt_sha256"):
            statistical_prompt = read_text((root or repo_root()) / "config" / STATISTICAL_PROMPT_FILE)
            statistical["prompt_sha256"] = sha256_text(statistical_prompt)
            statistical["prediction_input_sha256"] = prediction_input_sha256(prediction_input)
        validate_statistical_prediction_text(statistical)
        validate_statistical_prediction_metadata(statistical)
        variants = prediction.get("variants")
        if not isinstance(variants, list):
            variants = []
            prediction["variants"] = variants
        variants.append(statistical)
        save_race_json(path, payload)
        log_job(logger, job_name, race_id, "statistical prediction updated")
        return True
    except Exception as exc:  # noqa: BLE001
        log_job(logger, job_name, race_id, f"statistical prediction failed: {exc}")
        return False


def predict_paths(
    paths: list[Path],
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
    prediction_inputs: dict[str, dict[str, Any]] | None = None,
) -> list[Path]:
    updated = []
    for path in paths:
        payload = load_race_json(path) or {}
        race_id = str((payload.get("meta") or {}).get("race_id") or "")
        prediction_input = (prediction_inputs or {}).get(race_id)
        if predict_file(path, config, job_name, root, prediction_input):
            updated.append(path)
    return updated


def predict_statistical_paths(
    paths: list[Path],
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
    prediction_inputs: dict[str, dict[str, Any]] | None = None,
) -> list[Path]:
    updated = []
    for path in paths:
        payload = load_race_json(path) or {}
        race_id = str((payload.get("meta") or {}).get("race_id") or "")
        prediction_input = (prediction_inputs or {}).get(race_id)
        if predict_statistical_file(path, config, job_name, root, prediction_input):
            updated.append(path)
    return updated


def build_pending_statistical_inputs(
    paths: list[Path],
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    inputs = {}
    for path in paths:
        payload = load_race_json(path)
        if not payload:
            continue
        race_id = str((payload.get("meta") or {}).get("race_id") or "")
        existing = find_variant(
            prediction_variants(payload),
            STATISTICAL_PREDICTION_METHOD,
            config["llm_provider"],
            config["llm_model"],
        )
        if existing is not None:
            continue
        ensure_statistical_prediction_is_pre_race(payload)
        inputs[race_id] = build_statistical_prediction_input(payload)
    return inputs


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
