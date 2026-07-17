from __future__ import annotations

import argparse
from pathlib import Path

from collect import collect_races
from feedback import build_feedback_chat_input
from simulate import simulate_paths
from utils import (
    atomic_write_json,
    load_config,
    load_race_json,
    log_job,
    outbox_chat_input_dir,
    parse_target_date,
    save_race_json,
    set_race_status,
    setup_logger,
)


def export_feedback_chat_input(paths: list[Path], config: dict, job_name: str) -> list[Path]:
    logger = setup_logger(job_name, config)
    output_dir = outbox_chat_input_dir("feedback")
    exported: list[Path] = []

    for path in paths:
        payload = load_race_json(path)
        simulation = (payload or {}).get("simulation") or {}
        value_post = (simulation.get("value") or {}).get("post")
        dutching_post = (simulation.get("dutching") or {}).get("post")
        if not payload or not payload.get("result") or value_post is None or dutching_post is None:
            continue
        set_race_status(payload, post_status="awaiting_feedback")
        save_race_json(path, payload)

        chat_input = build_feedback_chat_input(payload)
        output_path = output_dir / f"{path.stem}.json"
        atomic_write_json(output_path, chat_input)
        exported.append(output_path)
        log_job(logger, job_name, payload["meta"].get("race_id"), f"feedback chat_input exported -> {output_path}")
    return exported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    paths = collect_races(config, "post_collect", target_date, "post")
    simulated_paths = simulate_paths(paths, config, "post", "post_collect")
    export_feedback_chat_input(simulated_paths, config, "post_collect")


if __name__ == "__main__":
    main()
