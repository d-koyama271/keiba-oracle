from __future__ import annotations

import argparse
from pathlib import Path

from predict import (
    build_pending_statistical_inputs,
    load_prediction_inputs,
    predict_paths,
    predict_statistical_paths,
)
from publish import publish_site
from render import render_site
from run_pre_collect import run_pre_collect_flow
from simulate import simulate_paths
from utils import (
    load_config,
    load_race_json,
    log_job,
    save_race_json,
    set_race_status,
    setup_logger,
)


def run_pre_flow(config: dict, target_date: str | None, job_name: str = "pre") -> list[Path]:
    logger = setup_logger(job_name, config)
    paths, input_paths = run_pre_collect_flow(config, target_date, job_name)
    prediction_inputs = load_prediction_inputs(input_paths)
    statistical_inputs = build_pending_statistical_inputs(paths, config)
    pending_race_ids = {
        str(payload["meta"].get("race_id") or "")
        for path in paths
        if (payload := load_race_json(path)) and not payload.get("prediction")
    }
    if set(prediction_inputs) != pending_race_ids:
        raise RuntimeError("pre flow stopped: finalized prediction inputs do not match pending races")

    predicted_paths = predict_paths(paths, config, job_name, prediction_inputs=prediction_inputs)
    if set(predicted_paths) != set(paths):
        raise RuntimeError("pre flow stopped: prediction generation failed")

    statistical_paths = predict_statistical_paths(
        paths,
        config,
        job_name,
        prediction_inputs=statistical_inputs,
    )
    if set(statistical_paths) != set(paths):
        raise RuntimeError("pre flow stopped: statistical prediction generation failed")

    simulated_paths = simulate_paths(statistical_paths, config, "pre", job_name)
    if set(simulated_paths) != set(paths):
        raise RuntimeError("pre flow stopped: simulation generation failed")

    for path in simulated_paths:
        payload = load_race_json(path)
        if not payload:
            raise RuntimeError(f"pre flow stopped: race JSON missing -> {path}")
        set_race_status(payload, pre_status="published")
        save_race_json(path, payload)

    render_site(config, job_name, None)
    public_path = publish_site(config)
    log_job(logger, job_name, None, f"published site -> {public_path}")
    return simulated_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    run_pre_flow(config, args.date)


if __name__ == "__main__":
    main()
