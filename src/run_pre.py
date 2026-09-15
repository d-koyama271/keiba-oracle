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
    atomic_write_json,
    list_race_files,
    outbox_chat_input_dir,
    runtime_prediction_entry,
    load_config,
    load_race_json,
    log_job,
    save_race_json,
    set_race_status,
    setup_logger,
)


def run_pre_flow(config: dict, target_date: str | None, job_name: str = "pre", *, phase: str = "all", resume: bool = False) -> list[Path]:
    if phase not in ("all", "general", "statistical"):
        raise ValueError(f"Unsupported pre phase: {phase}")
    if resume and (phase == "all" or not target_date):
        raise ValueError("resume requires --date and --phase general or statistical")
    logger = setup_logger(job_name, config)
    if resume:
        paths = list_race_files(config, target_date)
        if not paths:
            raise RuntimeError(f"No race JSON found for resume: {target_date}")
        suffix = ".statistical.json" if phase == "statistical" else ".json"
        input_paths = [outbox_chat_input_dir("prediction") / f"{path.stem}{suffix}" for path in paths]
        for path in input_paths:
            if not path.is_file():
                raise FileNotFoundError(f"Saved prediction input missing for resume: {path}")
        saved_inputs = load_prediction_inputs(input_paths)
        race_ids = {str(load_race_json(path)["meta"].get("race_id") or "") for path in paths}
        if set(saved_inputs) != race_ids:
            raise RuntimeError("resume input race IDs do not match target races")
    elif phase == "statistical":
        paths, input_paths = run_pre_collect_flow(config, target_date, job_name, phase=phase)
    else:
        paths, input_paths = run_pre_collect_flow(config, target_date, job_name)
    prediction_inputs = (saved_inputs if resume else load_prediction_inputs(input_paths)) if phase != "statistical" else {}
    statistical_inputs = saved_inputs if resume and phase == "statistical" else {}
    if phase != "general" and not resume:
        for path in paths:
            try:
                pending_inputs = build_pending_statistical_inputs([path], config)
                for prediction_input in pending_inputs.values():
                    atomic_write_json(outbox_chat_input_dir("prediction") / f"{path.stem}.statistical.json", prediction_input)
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
    if not successful_paths:
        raise RuntimeError(f"pre flow stopped: prediction generation failed for {methods}")
    published_paths = [path for path in paths if path in successful_paths]
    if phase != "statistical":
        published_paths = simulate_paths(published_paths, config, "pre", job_name)
        if set(published_paths) != successful_paths:
            raise RuntimeError("pre flow stopped: simulation generation failed")

    for path in published_paths:
        payload = load_race_json(path)
        if not payload:
            raise RuntimeError(f"pre flow stopped: race JSON missing -> {path}")
        set_race_status(payload, pre_status="published")
        save_race_json(path, payload)

    render_site(config, job_name, None)
    public_path = publish_site(config)
    log_job(logger, job_name, None, f"published site -> {public_path}")
    return published_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--phase", choices=("statistical", "general", "all"), default="all")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.resume and (args.phase == "all" or not args.date):
        parser.error("--resume requires --date and --phase general or statistical")

    config = load_config()
    run_pre_flow(config, args.date, phase=args.phase, resume=args.resume)


if __name__ == "__main__":
    main()
