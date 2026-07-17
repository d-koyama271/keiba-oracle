from __future__ import annotations

import argparse
import time
from pathlib import Path

from publish import publish_site
from render import render_site
from response_importer import import_prediction_response
from simulate import simulate_file
from utils import (
    ensure_dir,
    inbox_dir,
    load_config,
    load_race_json,
    log_job,
    save_race_json,
    set_race_status,
    setup_logger,
)


def inbox_files(kind: str) -> list[Path]:
    directory = inbox_dir(kind)
    ensure_dir(directory)
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def archive_processed(path: Path) -> Path:
    processed_dir = ensure_dir(path.parent / "processed")
    target = processed_dir / path.name
    if target.exists():
        target = processed_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
    path.replace(target)
    return target


def finalize_pre(race_path: Path, config: dict, logger_name: str) -> None:
    logger = setup_logger(logger_name, config)
    simulate_file(race_path, config, "pre", logger_name)
    payload = load_race_json(race_path)
    if payload:
        set_race_status(payload, pre_status="published")
        save_race_json(race_path, payload)
    render_site(config, logger_name, None)
    public_path = publish_site(config)
    log_job(logger, logger_name, None, f"manual pre published -> {public_path}")


def process_once(config: dict, logger_name: str) -> int:
    logger = setup_logger(logger_name, config)
    processed_count = 0

    for path in inbox_files("prediction"):
        try:
            race_path = import_prediction_response(path, config, logger_name)
            if race_path:
                finalize_pre(race_path, config, logger_name)
                archive_processed(path)
                processed_count += 1
        except Exception as exc:  # noqa: BLE001
            log_job(logger, logger_name, None, f"prediction processing failed: {path} -> {exc}")

    log_job(logger, logger_name, None, f"watch cycle complete: processed={processed_count}")
    return processed_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config = load_config()
    if args.once:
        process_once(config, "watcher")
        return

    while True:
        process_once(config, "watcher")
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    main()
