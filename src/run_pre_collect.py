from __future__ import annotations

import argparse
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


def grade_rank(race_name: str | None, soup: BeautifulSoup | None = None) -> int:
    return {"G1": 3, "G2": 2, "G3": 1}.get(normalize_class_grade(race_name, soup), 0)


def select_default_races(config: dict) -> tuple[str, list[dict], str]:
    session = requests.Session()
    start_date = date.fromisoformat(today_jst())
    target_tracks = set(config["target_races"])
    first_race_date = None
    graded_candidates = []
    fallback_race_ids = []

    for offset in range(LOOKAHEAD_DAYS + 1):
        target_date = (start_date + timedelta(days=offset)).isoformat()
        graded_race_ids = discover_race_ids(
            session,
            target_date,
            race_number=None,
            graded_only=True,
        )
        main_race_ids = discover_race_ids(session, target_date)
        if not graded_race_ids and not main_race_ids:
            if first_race_date:
                break
            continue
        if first_race_date is None:
            first_race_date = target_date

        for race_id in graded_race_ids:
            track_name = track_name_from_race_id(race_id)
            if track_name not in target_tracks:
                continue
            html = fetch_html(session, SHUTUBA_URL.format(race_id=race_id))
            soup = BeautifulSoup(html, "html.parser")
            entry_table = find_entry_table(soup)
            if entry_table is None:
                raise RuntimeError(f"Race overview is unavailable for graded race {race_id}")
            race = parse_race_overview(
                html,
                race_id,
                target_date,
                int(config["odds_reference_minutes_before_start"]),
            )
            graded_candidates.append(
                {
                    "race_id": race_id,
                    "race": race,
                    "grade_rank": grade_rank(race.get("race_name"), soup),
                }
            )

        fallback_race_ids.extend((target_date, race_id) for race_id in main_race_ids)

    if graded_candidates:
        selected_date = min(item["race"]["date"] for item in graded_candidates)
        return (
            selected_date,
            sorted(
                graded_candidates,
                key=lambda item: (item["race"]["date"], item["race_id"]),
            ),
            "all graded races in next race period",
        )

    fallback_candidates = []
    for target_date, race_id in fallback_race_ids:
        track_name = track_name_from_race_id(race_id)
        if track_name not in target_tracks:
            continue
        html = fetch_html(session, SHUTUBA_URL.format(race_id=race_id))
        soup = BeautifulSoup(html, "html.parser")
        if find_entry_table(soup) is None:
            raise RuntimeError(f"Race overview is unavailable for fallback race {race_id}")
        race = parse_race_overview(
            html,
            race_id,
            target_date,
            int(config["odds_reference_minutes_before_start"]),
        )
        fallback_candidates.append(
            {
                "race_id": race_id,
                "race": race,
                "grade_rank": 0,
            }
        )

    if fallback_candidates:
        selected_date = min(item["race"]["date"] for item in fallback_candidates)
        return (
            selected_date,
            sorted(
                fallback_candidates,
                key=lambda item: (item["race"]["date"], item["race_id"]),
            ),
            "fallback: no graded race in next race period; all 11R",
        )

    raise RuntimeError(
        f"No target JRA graded race or 11R found from "
        f"{start_date.isoformat()} within {LOOKAHEAD_DAYS} days"
    )


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
        if payload.get("prediction"):
            log_job(logger, job_name, payload["meta"].get("race_id"), "prediction input skipped: prediction already exists")
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


def collect_pre_races(config: dict, target_date_value: str | None, job_name: str) -> tuple[str, list[Path]]:
    selected_races = None
    selected_race_ids = None
    if target_date_value:
        target_date = parse_target_date(target_date_value)
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
            race_date = race.get("date") or target_date
            grade = f"G{4 - item['grade_rank']}" if item["grade_rank"] else "-"
            start_time = race.get("start_time") or "-"
            target_time_text = target_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            print(
                f"selected race: {race_date} {track_name} {race_number}R {race_name} "
                f"grade={grade} start={start_time} race_id={race_id} reason={reason}"
            )
            print(f"odds collection target: {target_time_text}")
            if now_jst() < target_time:
                print("warning: collecting before the configured odds target time")
                print("continuing collection")

        config["target_races"] = list(
            dict.fromkeys(item["race"]["track"] for item in selected_races)
        )

    if selected_races:
        collection_targets = {}
        for item in selected_races:
            race_date = item["race"]["date"]
            collection_targets.setdefault(race_date, []).append(item["race_id"])
    else:
        collection_targets = {target_date: selected_race_ids}

    paths = []
    for collection_date, race_ids in sorted(collection_targets.items()):
        paths.extend(
            collect_races(
                config,
                job_name,
                collection_date,
                "pre",
                selected_race_ids=race_ids,
            )
        )
    return target_date, paths


def run_pre_collect_flow(
    config: dict,
    target_date_value: str | None,
    job_name: str,
) -> tuple[list[Path], list[Path]]:
    target_date, paths = collect_pre_races(config, target_date_value, job_name)
    exported = export_prediction_chat_input(paths, config, job_name)
    if not paths:
        raise SystemExit(f"No race JSON updated for {target_date}")
    pending_count = sum(
        1
        for path in paths
        if not (load_race_json(path) or {}).get("prediction")
    )
    if pending_count and len(exported) < pending_count:
        raise SystemExit(f"No prediction chat_input exported for {target_date}")
    return paths, exported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    run_pre_collect_flow(config, args.date, "pre_collect")


if __name__ == "__main__":
    main()
