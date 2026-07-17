from __future__ import annotations

import argparse

from run_post_collect import run_post_flow
from utils import load_config, parse_target_date


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    run_post_flow(config, target_date, "post")


if __name__ == "__main__":
    main()
