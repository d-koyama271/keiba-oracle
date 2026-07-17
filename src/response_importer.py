from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from predict import normalize_prediction_response
from utils import (
    find_race_file_by_race_id,
    load_config,
    load_race_json,
    log_job,
    now_jst_iso,
    save_race_json,
    set_race_status,
    setup_logger,
)


def load_response_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_race_id(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta") or {}
    return meta.get("race_id") or payload.get("race_id")


def import_prediction_response(path: Path, config: dict[str, Any], job_name: str) -> Path | None:
    logger = setup_logger(job_name, config)
    payload = load_response_file(path)
    race_id = extract_race_id(payload)
    if not race_id:
        raise ValueError(f"prediction response missing race_id: {path}")

    race_path = find_race_file_by_race_id(config, race_id)
    if race_path is None:
        raise FileNotFoundError(f"race json not found for race_id={race_id}")

    race_payload = load_race_json(race_path)
    if not race_payload:
        raise FileNotFoundError(f"race json missing: {race_path}")

    response = payload.get("prediction", payload)
    normalized = normalize_prediction_response(response, race_payload["horses"])
    normalized["model_provider"] = config["llm_provider"]
    normalized["model_name"] = config["llm_model"]
    normalized["predicted_at"] = now_jst_iso()
    race_payload["prediction"] = normalized
    set_race_status(race_payload, pre_status="prediction_imported")
    save_race_json(race_path, race_payload)
    log_job(logger, job_name, race_id, f"prediction imported <- {path}")
    return race_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("prediction",), required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()

    config = load_config()
    file_path = Path(args.file)
    import_prediction_response(file_path, config, "importer")


if __name__ == "__main__":
    main()
