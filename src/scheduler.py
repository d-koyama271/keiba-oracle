from __future__ import annotations

import argparse
import errno
import json
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import requests

from collect import SHUTUBA_URL, discover_race_ids, fetch_html, parse_race_overview
from automation_state import clear_phase_state, load_automation_state, record_failure
from run_pre import run_pre_flow
from deploy import deploy_site
from run_post_collect import run_post_flow
from utils import (JST, atomic_write_json, data_dir, load_config, load_race_json, now_jst, outbox_chat_input_dir,
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


def discover_cached_races(config: dict, now: datetime | None = None,
                          root: Path | None = None) -> list[dict]:
    current = now if now is not None else now_jst()
    current = current.replace(tzinfo=JST) if current.tzinfo is None else current.astimezone(JST)
    interval = config["automation"]["discovery_interval_minutes"]
    if type(interval) is not int or interval <= 0:
        raise ValueError("automation.discovery_interval_minutes must be a positive integer")
    dates = [(current.date() + timedelta(days=offset)).isoformat() for offset in (0, 1)]
    path = data_dir(config, root) / "automation" / "discovery_cache.json"
    cached = None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        captured = parse_jst_datetime(cache["discovered_at"])
        if captured is None or not isinstance(cache["dates"], list) or not isinstance(cache["races"], list):
            raise ValueError("invalid discovery cache")
        cached = []
        for item in cache["races"]:
            race = item["race"]
            if not isinstance(item["race_id"], str) or not isinstance(race["race_number"], int):
                raise ValueError("invalid cached race")
            times = calculate_phase_times(race, config)
            if race["date"] in dates and race["track"] in config["target_races"]:
                cached.append({"race_id": item["race_id"], "race": race, "scheduled_at": times})
        if (cache["dates"] == dates and cache.get("target_races") == config["target_races"]
                and timedelta(0) <= current - captured < timedelta(minutes=interval)):
            return cached
    except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
        cached = None
    try:
        races = discover_scheduled_races(config, current)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        if cached is None:
            raise
        return cached
    atomic_write_json(path, {
        "discovered_at": current.isoformat(), "dates": dates,
        "target_races": config["target_races"],
        "races": [{"race_id": item["race_id"], "race": item["race"]} for item in races],
    })
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
            elif phase == "result" and not any(prediction_for_method(payload, method) for method in ("general", "statistical")):
                reason = "no_prediction"
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


def execute_phases(races: list[dict], config: dict, now: datetime | None = None,
                   root: Path | None = None) -> None:
    settings = config["automation"]
    for key in ("retry_interval_minutes", "max_attempts", "result_retry_interval_minutes", "result_max_attempts"):
        if type(settings.get(key)) is not int or settings[key] <= 0:
            raise ValueError(f"automation.{key} must be a positive integer")
    seen = set()
    for item in races:
        race, race_id = item["race"], item["race_id"]
        path = race_json_path(config, race["date"], race["track"], race["race_number"], root)
        for phase in ("statistical", "general", "result"):
            key = (race_id, phase)
            if key in seen:
                continue
            seen.add(key)
            # Refresh both time and saved artifacts after each preceding phase.
            decision = next(d for d in decide_phases([item], config, now, root) if d["phase"] == phase)
            if decision["reason"] == "completed":
                clear_phase_state(path, config, phase, root)
                continue
            if decision["reason"] == "missed_execution_window":
                record_failure(path, config, race_id, phase, "missed_execution_window", status="blocked", root=root)
                continue
            if not decision["runnable"]:
                continue
            error = None
            try:
                if phase == "result":
                    run_post_flow(config, race["date"], "post", race_id=race_id)
                else:
                    run_pre_flow(config, race["date"], phase=phase,
                                 resume=decision["mode"] == "resume", race_id=race_id)
                payload = load_race_json(path) or {}
                completed = bool(payload.get("result")) if phase == "result" else bool(prediction_for_method(payload, phase))
                if payload.get("meta", {}).get("race_id") != race_id or not completed:
                    error = f"{phase} artifact missing after execution"
            except (Exception, SystemExit) as exc:
                error = f"{type(exc).__name__}: {exc}"
            if error is None:
                clear_phase_state(path, config, phase, root)
                continue
            state = load_automation_state(path, config, root) or {}
            attempts = state.get("phases", {}).get(phase, {}).get("attempts", 0)
            prefix = "result_" if phase == "result" else ""
            blocked = attempts + 1 >= settings[f"{prefix}max_attempts"]
            current = now if now is not None else now_jst()
            current = current.replace(tzinfo=JST) if current.tzinfo is None else current.astimezone(JST)
            retry_at = current + timedelta(minutes=settings[f"{prefix}retry_interval_minutes"])
            record_failure(path, config, race_id, phase, error,
                           status="blocked" if blocked else "retry_wait",
                           next_retry_at=None if blocked else retry_at.isoformat(), root=root)


@contextmanager
def scheduler_lock(config: dict):
    path = data_dir(config) / "automation" / "scheduler.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    config = load_config()
    with scheduler_lock(config) if args.execute else nullcontext(True) as acquired:
        if not acquired:
            print("別のschedulerが実行中のためskipします。")
            return
        current = now_jst()
        races = discover_cached_races(config, current) if args.execute else discover_scheduled_races(config, current)
        decisions = decide_phases(races, config, current)
        for item in races:
            race = item["race"]
            print(f"{race['date']} {race['track']}{race['race_number']}R {race['race_name']}")
            for decision in decisions:
                if decision["race_id"] == item["race_id"]:
                    status = decision["mode"] if decision["runnable"] else decision["reason"]
                    print(f"{decision['phase']}: {decision['scheduled_at']:%Y-%m-%d %H:%M} JST ({status})")
        if args.execute:
            execute_phases(races, config)
            deploy_site(config)


if __name__ == "__main__":
    main()
