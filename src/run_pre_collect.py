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


def grade_rank(race_name: str | None) -> int:
    text = (race_name or "").upper()
    text = (
        text.replace("Ｇ", "G")
        .replace("１", "1")
        .replace("２", "2")
        .replace("３", "3")
        .replace("Ⅰ", "I")
        .replace("Ⅱ", "II")
        .replace("Ⅲ", "III")
    )
    if re.search(r"G\s*(1|I)(?!I)", text):
        return 3
    if re.search(r"G\s*(2|II)(?!I)", text):
        return 2
    if re.search(r"G\s*(3|III)", text):
        return 1
    return 0


def select_default_race(config: dict) -> tuple[str, str, dict, str]:
    session = requests.Session()
    start_date = date.fromisoformat(today_jst())

    for offset in range(LOOKAHEAD_DAYS + 1):
        target_date = (start_date + timedelta(days=offset)).isoformat()
        race_ids = discover_race_ids(session, target_date)
        if not race_ids:
            continue

        candidates = []
        target_race_found = False
        for race_id in race_ids:
            track_name = track_name_from_race_id(race_id)
            if track_name not in set(config["target_races"]):
                continue
            target_race_found = True
            html = fetch_html(session, SHUTUBA_URL.format(race_id=race_id))
            entry_table = find_entry_table(BeautifulSoup(html, "html.parser"))
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
                    "grade_rank": grade_rank(race.get("race_name")),
                    "start_minutes": start_time_minutes(race.get("start_time")),
                }
            )

        if target_race_found and not candidates:
            raise RuntimeError(f"Race overview is unavailable for next race date {target_date}")

        if not candidates:
            continue

        graded = [item for item in candidates if item["grade_rank"] > 0]
        pool = graded or candidates
        selected = sorted(
            pool,
            key=lambda item: (item["grade_rank"], item["start_minutes"], item["race_id"]),
            reverse=True,
        )[0]
        reason = "graded race priority" if graded else "latest 11R on next race date"
        race = selected["race"]
        return target_date, selected["race_id"], race, reason

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
    if args.date:
        target_date = parse_target_date(args.date)
    else:
        try:
            target_date, race_id, race, reason = select_default_race(config)
            target_time = target_odds_datetime(
                race,
                int(config["odds_reference_minutes_before_start"]),
            )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from None

        track_name = race["track"]
        race_name = race.get("race_name") or "-"
        start_time = race.get("start_time") or "-"
        target_time_text = target_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        print(
            f"selected race: {target_date} {track_name} 11R {race_name} "
            f"start={start_time} race_id={race_id} reason={reason}"
        )
        print(f"odds collection target: {target_time_text}")
        if now_jst() < target_time:
            print(f"odds collection is not due yet; rerun at or after {target_time_text}")
            return

        config["target_races"] = [track_name]

    paths = collect_races(config, "pre_collect", target_date, "pre")
    exported = export_prediction_chat_input(paths, config, "pre_collect")
    if not paths:
        raise SystemExit(f"No race JSON updated for {target_date}")
    if not exported:
        raise SystemExit(f"No prediction chat_input exported for {target_date}")


if __name__ == "__main__":
    main()
