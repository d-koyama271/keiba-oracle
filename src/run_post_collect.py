from __future__ import annotations

import argparse
from pathlib import Path

from collect import collect_results
from evaluation import evaluate_paths
from evaluation_summary import generate_evaluation_summary
from publish import publish_site
from render import render_site
from simulate import simulate_paths
from utils import (
    load_config,
    load_race_json,
    list_race_files,
    log_job,
    parse_target_date,
    prediction_entries,
    setup_logger,
)


def publish_post_results(
    paths: list[Path],
    config: dict,
    job_name: str,
    root: Path | None = None,
    *, race_id: str | None = None,
) -> list[Path]:
    logger = setup_logger(job_name, config, root)
    cancelled_paths = [path for path in paths if load_race_json(path).get("race", {}).get("cancelled")]
    evaluated_paths = evaluate_paths([path for path in paths if path not in cancelled_paths], config, job_name, root)
    if not evaluated_paths and not cancelled_paths:
        log_job(logger, job_name, None, "post publish skipped: evaluation missing")
        return []

    generate_evaluation_summary(config, job_name, root)
    render_site(config, job_name, None, root, **({"race_id": race_id} if race_id is not None else {}))
    public_path = publish_site(config, root)
    log_job(logger, job_name, None, f"post published -> {public_path}")
    return evaluated_paths + cancelled_paths


def run_post_flow(config: dict, target_date: str, job_name: str, *, race_id: str | None = None) -> list[Path]:
    logger = setup_logger(job_name, config)
    target_paths = []
    found = False
    for path in list_race_files(config, target_date):
        payload = load_race_json(path)
        if race_id is not None and (payload or {}).get("meta", {}).get("race_id") != race_id:
            continue
        found = True
        if payload and (payload.get("race", {}).get("cancelled") or any(entry.get(method) for entry in prediction_entries(payload) for method in ("general", "statistical"))):
            target_paths.append(path)

    if race_id is not None and not found:
        raise FileNotFoundError(f"No race JSON found for {target_date}: {race_id}")
    if not target_paths:
        log_job(logger, job_name, None, f"post collection skipped: no predicted race JSON for {target_date}")
        return []

    paths = collect_results(config, job_name, target_paths)
    cancelled_paths = [path for path in paths if load_race_json(path).get("race", {}).get("cancelled")]
    simulate_paths([path for path in paths if path not in cancelled_paths], config, "post", job_name)
    return publish_post_results(paths, config, job_name, **({"race_id": race_id} if race_id is not None else {}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--race-id", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    run_post_flow(config, target_date, "post_collect", **({"race_id": args.race_id} if args.race_id is not None else {}))


if __name__ == "__main__":
    main()
