from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import requests

from collect import SHUTUBA_URL, discover_race_ids, fetch_html, parse_race_overview
from automation_state import load_automation_state
from utils import (JST, load_config, load_race_json, now_jst, outbox_chat_input_dir,
                   parse_jst_datetime, prediction_for_method, race_json_path,
                   race_start_datetime, track_name_from_race_id)


def calculate_phase_times(race: dict, config: dict) -> dict[str, datetime]:
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    if start is None:
        raise ValueError("phase scheduling requires race date and start_time")
    settings = config["automation"]
    statistical_time = datetime.strptime(settings["statistical_time"], "%H:%M").time()
    before = settings["general_minutes_before_start"]
    after = settings["result_minutes_after_start"]
    if any(type(value) is not int or value < 0 for value in (before, after)):
        raise ValueError("automation minute offsets must be nonnegative integers")
    return {
        "statistical": datetime.combine(start.date() - timedelta(days=1), statistical_time, tzinfo=JST),
        "general": start - timedelta(minutes=before),
        "result": start + timedelta(minutes=after),
    }


def discover_scheduled_races(config: dict, now: datetime | None = None) -> list[dict]:
    current = now if now is not None else now_jst()
    current = current.replace(tzinfo=JST) if current.tzinfo is None else current.astimezone(JST)
    target_tracks = set(config["target_races"])
    races = []
    with requests.Session() as session:
        for offset in (0, 1):
            target_date = (current.date() + timedelta(days=offset)).isoformat()
            race_ids = discover_race_ids(session, target_date, race_number=None, graded_only=True)
            for race_id in race_ids:
                if track_name_from_race_id(race_id) not in target_tracks:
                    continue
                html = fetch_html(session, SHUTUBA_URL.format(race_id=race_id))
                race = parse_race_overview(
                    html, race_id, target_date, int(config["odds_reference_minutes_before_start"]),
                )
                races.append({"race_id": race_id, "race": race, "scheduled_at": calculate_phase_times(race, config)})
    return races


def decide_phases(races: list[dict], config: dict, now: datetime | None = None,
                  root: Path | None = None) -> list[dict]:
    current = now if now is not None else now_jst()
    current = current.replace(tzinfo=JST) if current.tzinfo is None else current.astimezone(JST)
    decisions = []
    for item in races:
        race, race_id = item["race"], item["race_id"]
        path = race_json_path(config, race["date"], race["track"], race["race_number"], root)
        payload = load_race_json(path) or {}
        state = load_automation_state(path, config, root)
        if payload and payload["meta"].get("race_id") != race_id:
            raise ValueError("race JSON race_id mismatch")
        if state and state["race_id"] != race_id:
            raise ValueError("automation state race_id mismatch")
        start = race_start_datetime(race["date"], race["start_time"])
        for phase, scheduled_at in calculate_phase_times(race, config).items():
            saved_input = phase != "result" and (
                outbox_chat_input_dir("prediction", root)
                / f"{path.stem}{'.statistical' if phase == 'statistical' else ''}.json"
            ).is_file()
            record = (state or {}).get("phases", {}).get(phase, {})
            completed = bool(payload.get("result")) if phase == "result" else bool(prediction_for_method(payload, phase))
            reason = None
            if completed:
                reason = "completed"
            elif record.get("status") == "blocked":
                reason = "blocked"
            elif phase != "result" and payload.get("result"):
                reason = "result_exists"
            elif phase != "result" and current >= start and not saved_input:
                reason = "missed_execution_window"
            elif current < scheduled_at:
                reason = "not_scheduled_yet"
            elif record.get("status") == "retry_wait" and current < parse_jst_datetime(record["next_retry_at"]):
                reason = "retry_wait"
            decisions.append({
                "race_id": race_id, "date": race["date"], "phase": phase,
                "scheduled_at": scheduled_at, "mode": "resume" if saved_input else "normal",
                "runnable": reason is None, "reason": reason,
            })
    return decisions


def main() -> None:
    config, current = load_config(), now_jst()
    races = discover_scheduled_races(config, current)
    decisions = decide_phases(races, config, current)
    for item in races:
        race = item["race"]
        print(f"{race['date']} {race['track']}{race['race_number']}R {race['race_name']}")
        for decision in decisions:
            if decision["race_id"] == item["race_id"]:
                status = decision["mode"] if decision["runnable"] else decision["reason"]
                print(f"{decision['phase']}: {decision['scheduled_at']:%Y-%m-%d %H:%M} JST ({status})")


if __name__ == "__main__":
    main()
