from __future__ import annotations

import argparse

from collect import collect_races
from predict import predict_paths
from publish import publish_site
from render import render_site
from run_pre_collect import export_prediction_chat_input
from simulate import simulate_paths
from utils import load_config, log_job, parse_target_date, setup_logger


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    logger = setup_logger("pre", config)

    paths = collect_races(config, "pre", target_date, "pre")
    if config["llm_provider"] == "manual":
        export_prediction_chat_input(paths, config, "pre")
        log_job(logger, "pre", None, "manual mode prepared prediction chat_input")
        return

    predict_paths(paths, config, "pre")
    simulate_paths(paths, config, "pre", "pre")
    render_site(config, "pre", None)
    public_path = publish_site(config)
    log_job(logger, "pre", None, f"published site -> {public_path}")


if __name__ == "__main__":
    main()
