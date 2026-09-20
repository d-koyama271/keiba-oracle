from __future__ import annotations

import argparse
from pathlib import Path

from predict import (
    build_pending_statistical_inputs,
    load_prediction_inputs,
    validate_prediction_input,
    validate_statistical_prediction_input,
    predict_paths,
    predict_statistical_paths,
)
from publish import publish_site
from evaluation_summary import generate_evaluation_summary
from render import render_site
from run_pre_collect import run_pre_collect_flow
from simulate import simulate_paths
from utils import (
    atomic_write_json,
    list_race_files,
    prediction_input_path,
    runtime_prediction_entry,
    load_config,
    load_race_json,
    log_job,
    setup_logger,
)


def run_pre_flow(config: dict, target_date: str | None, job_name: str = "pre", *, phase: str = "all", resume: bool = False, race_id: str | None = None) -> list[Path]:
    if phase not in ("all", "general", "statistical"):
        raise ValueError(f"Unsupported pre phase: {phase}")
    if resume and (phase == "all" or not target_date):
        raise ValueError("resume requires --date and --phase general or statistical")
    logger = setup_logger(job_name, config)
    cancelled_paths = []
    if resume:
        paths = list_race_files(config, target_date)
        if race_id is not None:
            paths = [path for path in paths if (load_race_json(path) or {}).get("meta", {}).get("race_id") == race_id]
            if not paths:
                raise FileNotFoundError(f"No race JSON found for {target_date}: {race_id}")
        if not paths:
            raise RuntimeError(f"No race JSON found for resume: {target_date}")
        cancelled_paths = [path for path in paths if load_race_json(path).get("race", {}).get("cancelled")]
        paths = [path for path in paths if path not in cancelled_paths]
        input_paths = [prediction_input_path(config, path, phase) for path in paths]
        for path in input_paths:
            if not path.is_file():
                raise FileNotFoundError(f"Saved prediction input missing for resume: {path}")
        saved_inputs = load_prediction_inputs(input_paths)
        race_ids = {str(load_race_json(path)["meta"].get("race_id") or "") for path in paths}
        if set(saved_inputs) != race_ids:
            raise RuntimeError("resume input race IDs do not match target races")
        for path in paths:
            payload = load_race_json(path)
            validator = validate_statistical_prediction_input if phase == "statistical" else validate_prediction_input
            validator(saved_inputs[payload["meta"]["race_id"]], payload)
    elif phase == "statistical":
        paths, input_paths = run_pre_collect_flow(config, target_date, job_name, phase=phase, **({"race_id": race_id} if race_id is not None else {}))
    else:
        paths, input_paths = run_pre_collect_flow(config, target_date, job_name, **({"race_id": race_id} if race_id is not None else {}))
    if not resume:
        cancelled_paths = [path for path in paths if load_race_json(path).get("race", {}).get("cancelled")]
        paths = [path for path in paths if path not in cancelled_paths]
    prediction_inputs = (saved_inputs if resume else load_prediction_inputs(input_paths)) if phase != "statistical" else {}
    statistical_inputs = saved_inputs if resume and phase == "statistical" else {}
    if phase != "general" and not resume:
        for path in paths:
            try:
                pending_inputs = build_pending_statistical_inputs([path], config)
                for prediction_input in pending_inputs.values():
                    input_path = prediction_input_path(config, path, "statistical")
                    if input_path.exists():
                        prediction_input = load_prediction_inputs([input_path])[prediction_input["meta"]["race_id"]]
                        validate_statistical_prediction_input(prediction_input, load_race_json(path))
                        pending_inputs[prediction_input["meta"]["race_id"]] = prediction_input
                    else:
                        atomic_write_json(input_path, prediction_input)
                statistical_inputs.update(pending_inputs)
            except (ValueError, KeyError) as exc:
                log_job(logger, job_name, None, f"statistical input unavailable for {path}: {exc}")
    predicted_paths = []
    if phase != "statistical":
        pending_race_ids = {
            str(payload["meta"].get("race_id") or "")
            for path in paths
            if (payload := load_race_json(path)) and not (runtime_prediction_entry(payload, config) or {}).get("general")
        }
        if not resume and set(prediction_inputs) != pending_race_ids:
            raise RuntimeError("pre flow stopped: finalized prediction inputs do not match pending races")
        predicted_paths = predict_paths(paths, config, job_name, prediction_inputs=prediction_inputs)

    statistical_paths = []
    if phase != "general":
        ready_paths = [
            path for path in paths
            if (payload := load_race_json(path)) and (
                str(payload["meta"].get("race_id") or "") in statistical_inputs
                or (runtime_prediction_entry(payload, config) or {}).get("statistical")
            )
        ]
        statistical_paths = predict_statistical_paths(
            ready_paths,
            config,
            job_name,
            prediction_inputs=statistical_inputs,
        )
    successful_paths = set(predicted_paths) | set(statistical_paths)
    methods = "both methods" if phase == "all" else phase
    for path in paths:
        if path not in successful_paths:
            log_job(logger, job_name, None, f"prediction failed for {methods}: {path}")
    if not successful_paths and not cancelled_paths:
        raise RuntimeError(f"pre flow stopped: prediction generation failed for {methods}")
    published_paths = [path for path in paths if path in successful_paths]
    if phase != "statistical":
        published_paths = simulate_paths(published_paths, config, "pre", job_name)
        if set(published_paths) != successful_paths:
            raise RuntimeError("pre flow stopped: simulation generation failed")

    if cancelled_paths:
        generate_evaluation_summary(config, job_name)
    render_site(config, job_name, None, **({"race_id": race_id} if race_id is not None else {}))
    public_path = publish_site(config)
    log_job(logger, job_name, None, f"published site -> {public_path}")
    return published_paths + cancelled_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--phase", choices=("statistical", "general", "all"), default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--race-id", default=None)
    args = parser.parse_args()
    if args.resume and (args.phase == "all" or not args.date):
        parser.error("--resume requires --date and --phase general or statistical")

    config = load_config()
    run_pre_flow(config, args.date, phase=args.phase, resume=args.resume, **({"race_id": args.race_id} if args.race_id is not None else {}))


if __name__ == "__main__":
    main()
