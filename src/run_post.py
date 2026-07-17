from __future__ import annotations

import argparse

from collect import collect_races
from feedback import feedback_paths
from publish import publish_site
from render import render_site
from run_post_collect import export_feedback_chat_input
from simulate import simulate_paths
from utils import load_config, log_job, parse_target_date, setup_logger


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    logger = setup_logger("post", config)

    paths = collect_races(config, "post", target_date, "post")
    simulated_paths = simulate_paths(paths, config, "post", "post")
    if config["llm_provider"] == "manual":
        export_feedback_chat_input(simulated_paths, config, "post")
        log_job(logger, "post", None, "manual mode prepared feedback chat_input")
        return

    feedback_paths(simulated_paths, config, "post")
    render_site(config, "post", None)
    public_path = publish_site(config)
    log_job(logger, "post", None, f"published site -> {public_path}")


if __name__ == "__main__":
    main()
