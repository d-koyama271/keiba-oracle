from __future__ import annotations

import argparse
from pathlib import Path

from collect import collect_races
from predict import build_prediction_chat_input
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


def export_prediction_chat_input(paths: list[Path], config: dict, job_name: str) -> list[Path]:
    logger = setup_logger(job_name, config)
    output_dir = outbox_chat_input_dir("prediction")
    exported: list[Path] = []

    for path in paths:
        payload = load_race_json(path)
        if not payload:
            continue
        payload["prediction"] = None
        payload["simulation"]["pre"] = None
        payload["simulation"]["post"] = None
        payload["result"] = None
        payload["feedback"] = None
        payload.setdefault("meta", {})["post_status"] = None
        set_race_status(payload, pre_status="awaiting_prediction")
        save_race_json(path, payload)

        chat_input = build_prediction_chat_input(config, payload)
        output_path = output_dir / f"{path.stem}.json"
        atomic_write_json(output_path, chat_input)
        exported.append(output_path)
        log_job(logger, job_name, payload["meta"].get("race_id"), f"prediction chat_input exported -> {output_path}")
    return exported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    paths = collect_races(config, "pre_collect", target_date, "pre")
    export_prediction_chat_input(paths, config, "pre_collect")


if __name__ == "__main__":
    main()
