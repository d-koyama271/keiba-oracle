from __future__ import annotations

import argparse
from pathlib import Path

from collect import collect_races
from evaluation import evaluate_paths
from publish import publish_site
from render import render_site
from simulate import simulate_paths
from utils import (
    load_config,
    load_race_json,
    log_job,
    parse_target_date,
    save_race_json,
    set_race_status,
    setup_logger,
)


def publish_post_results(
    paths: list[Path],
    config: dict,
    job_name: str,
    root: Path | None = None,
) -> list[Path]:
    logger = setup_logger(job_name, config, root)
    evaluated_paths = evaluate_paths(paths, config, job_name, root)
    if not evaluated_paths:
        log_job(logger, job_name, None, "post publish skipped: evaluation missing")
        return []

    render_site(config, job_name, None, root)
    public_path = publish_site(config, root)
    for path in evaluated_paths:
        payload = load_race_json(path)
        if not payload:
            continue
        set_race_status(payload, post_status="published")
        save_race_json(path, payload)
    log_job(logger, job_name, None, f"post published -> {public_path}")
    return evaluated_paths


def run_post_flow(config: dict, target_date: str, job_name: str) -> list[Path]:
    paths = collect_races(config, job_name, target_date, "post")
    simulated_paths = simulate_paths(paths, config, "post", job_name)
    return publish_post_results(simulated_paths, config, job_name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    run_post_flow(config, target_date, "post_collect")


if __name__ == "__main__":
    main()
