from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from collect import (
    SHUTUBA_URL,
    collect_races,
    discover_race_ids,
    fetch_html,
    find_entry_table,
    normalize_class_grade,
    parse_race_overview,
)
from predict import build_prediction_chat_input
from utils import (
    atomic_write_json,
    load_config,
    load_race_json,
    log_job,
    now_jst,
    outbox_chat_input_dir,
    parse_target_date,
    race_start_datetime,
    save_race_json,
    set_race_status,
    setup_logger,
    today_jst,
    track_name_from_race_id,
)

LOOKAHEAD_DAYS = 21


def start_time_minutes(value: str | None) -> int:
    if not value:
        return 0
    match = re.match(r"^(\d{1,2}):(\d{2})$", value)
    if not match:
        return 0
    return int(match.group(1)) * 60 + int(match.group(2))


def grade_rank(race_name: str | None, soup: BeautifulSoup | None = None) -> int:
    return {"G1": 3, "G2": 2, "G3": 1}.get(normalize_class_grade(race_name, soup), 0)


def select_default_races(config: dict) -> tuple[str, list[dict], str]:
    session = requests.Session()
    start_date = date.fromisoformat(today_jst())
    target_tracks = set(config["target_races"])
    fallback_date = None
    fallback_candidates = None

    for offset in range(LOOKAHEAD_DAYS + 1):
        target_date = (start_date + timedelta(days=offset)).isoformat()
        race_ids = discover_race_ids(session, target_date)
        if not race_ids:
            continue

        candidates = []
        target_race_found = False
        for race_id in race_ids:
            track_name = track_name_from_race_id(race_id)
            if track_name not in target_tracks:
                continue
            target_race_found = True
            html = fetch_html(session, SHUTUBA_URL.format(race_id=race_id))
            soup = BeautifulSoup(html, "html.parser")
            entry_table = find_entry_table(soup)
            if entry_table is None:
                continue
            race = parse_race_overview(
                html,
                race_id,
                target_date,
                int(config["odds_reference_minutes_before_start"]),
            )
            candidates.append(
                {
                    "race_id": race_id,
                    "race": race,
                    "grade_rank": grade_rank(race.get("race_name"), soup),
                    "start_minutes": start_time_minutes(race.get("start_time")),
                }
            )

        if target_race_found and not candidates:
            raise RuntimeError(f"Race overview is unavailable for next race date {target_date}")

        if not candidates:
            continue

        if fallback_candidates is None:
            fallback_date = target_date
            fallback_candidates = candidates

        graded = [item for item in candidates if item["grade_rank"] > 0]
        if graded:
            return (
                target_date,
                sorted(graded, key=lambda item: item["race_id"]),
                "graded races on nearest graded race date",
            )

    if fallback_date and fallback_candidates:
        selected = sorted(
            fallback_candidates,
            key=lambda item: (item["grade_rank"], item["start_minutes"], item["race_id"]),
            reverse=True,
        )[0]
        return fallback_date, [selected], "fallback: no graded 11R in lookahead"

    raise RuntimeError(f"No target JRA 11R found from {start_date.isoformat()} within {LOOKAHEAD_DAYS} days")


def target_odds_datetime(race: dict, reference_minutes: int) -> datetime:
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    if start is None:
        raise ValueError("race start datetime is unavailable")
    return start - timedelta(minutes=reference_minutes)


def export_prediction_chat_input(paths: list[Path], config: dict, job_name: str) -> list[Path]:
    logger = setup_logger(job_name, config)
    output_dir = outbox_chat_input_dir("prediction")
    exported: list[Path] = []

    for path in paths:
        payload = load_race_json(path)
        if not payload:
            continue
        if not payload.get("horses"):
            log_job(logger, job_name, payload["meta"].get("race_id"), "prediction chat_input skipped: horses missing")
            continue
        payload["prediction"] = None
        payload["simulation"] = {
            "value": {"pre": None, "post": None},
            "dutching": {"pre": None, "post": None},
        }
        payload["result"] = None
        payload["evaluation"] = None
        payload.setdefault("meta", {})["post_status"] = "awaiting_result"
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
    if args.date:
        target_date = parse_target_date(args.date)
    else:
        try:
            target_date, selected_races, reason = select_default_races(config)
            reference_minutes = int(config["odds_reference_minutes_before_start"])
            target_times = [
                target_odds_datetime(item["race"], reference_minutes)
                for item in selected_races
            ]
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from None

        for item, target_time in zip(selected_races, target_times):
            race_id = item["race_id"]
            race = item["race"]
            track_name = race["track"]
            race_number = race.get("race_number") or 11
            race_name = race.get("race_name") or "-"
            grade = f"G{4 - item['grade_rank']}" if item["grade_rank"] else "-"
            start_time = race.get("start_time") or "-"
            target_time_text = target_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            print(
                f"selected race: {target_date} {track_name} {race_number}R {race_name} "
                f"grade={grade} start={start_time} race_id={race_id} reason={reason}"
            )
            print(f"odds collection target: {target_time_text}")
            if now_jst() < target_time:
                print("warning: collecting before the configured odds target time")
                print("continuing manual collection")

        config["target_races"] = [item["race"]["track"] for item in selected_races]

    paths = collect_races(config, "pre_collect", target_date, "pre")
    exported = export_prediction_chat_input(paths, config, "pre_collect")
    if not paths:
        raise SystemExit(f"No race JSON updated for {target_date}")
    if not exported:
        raise SystemExit(f"No prediction chat_input exported for {target_date}")


if __name__ == "__main__":
    main()
