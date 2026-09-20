from __future__ import annotations

import argparse
import errno
import json
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import requests

from collect import (SHUTUBA_URL, discover_race_ids, fetch_html, parse_race_overview,
                     fetch_cancellation_notices, parse_cancellation_notice)
from automation_state import clear_phase_state, load_automation_state, record_failure, record_phase_started
from run_pre import run_pre_flow
from predict import validate_prediction_input, validate_statistical_prediction_input
from deploy import deploy_site
from run_post_collect import run_post_flow, publish_post_results
from utils import (JST, atomic_write_json, data_dir, load_config, load_race_json, now_jst, prediction_input_path,
                   list_race_files, ensure_race_payload, save_race_json, prediction_entries, linked_record, runtime_prediction_entry,
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
    dates = [(current.date() + timedelta(days=offset)).isoformat() for offset in (0, 1)]
    path = data_dir(config, root) / "automation" / "discovery_cache.json"
    cached = None
    cache = {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        captured = parse_jst_datetime(cache["discovered_at"])
        if captured is None or not isinstance(cache["dates"], list) or not isinstance(cache["races"], list):
            raise ValueError("invalid discovery cache")
        cached = []
        for item in cache["races"]:
            race = item["race"]
            if (not isinstance(item["race_id"], str) or not item["race_id"]
                    or type(race["race_number"]) is not int or not 1 <= race["race_number"] <= 12
                    or not isinstance(race["track"], str) or not race["track"]
                    or race["date"] not in cache["dates"]):
                raise ValueError("invalid cached race")
            times = calculate_phase_times(race, config)
            if race["date"] in dates and race["track"] in config["target_races"]:
                cached.append({"race_id": item["race_id"], "race": race, "scheduled_at": times})
        evening = datetime.combine(current.date(), datetime.strptime(
            config["automation"]["statistical_time"], "%H:%M").time(), tzinfo=JST)
        if (cache["dates"] == dates and cache.get("target_races") == config["target_races"]
                and not captured < evening <= current):
            return cached
        attempted = parse_jst_datetime(cache.get("discovery_attempted_at"))
        if attempted is not None and timedelta(0) <= current - attempted < timedelta(hours=1):
            return cached
    except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
        cached = None
    if cached is not None:
        cache["discovery_attempted_at"] = current.isoformat()
        atomic_write_json(path, cache)
    try:
        races = discover_scheduled_races(config, current)
        restore_replacement_races(races, config, root)
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError):
        if cached is None:
            raise
        return cached
    atomic_write_json(path, {
        "discovered_at": current.isoformat(), "dates": dates,
        "cancellation_checked_at": cache.get("cancellation_checked_at") if isinstance(cache, dict) else None,
        "target_races": config["target_races"],
        "races": [{"race_id": item["race_id"], "race": item["race"]} for item in races],
    })
    return races


def restore_replacement_races(races: list[dict], config: dict, root: Path | None = None) -> None:
    cancelled = []
    for path in list_race_files(config, None, root):
        payload = load_race_json(path)
        if payload and payload["race"].get("cancelled"):
            cancelled.append(payload)
    # Replacement confirmation uses saved evidence and the discovered schedule only.
    for payload in cancelled:
        race = payload["race"]
        for item in races:
            replacement = item["race"]
            if (item["race_id"] != payload["meta"]["race_id"] or replacement["date"] == race["date"]
                    or replacement["date"] != race.get("cancellation", {}).get("replacement_date")):
                continue
            new_path = race_json_path(config, replacement["date"], replacement["track"], replacement["race_number"], root)
            if not new_path.exists():
                new_payload = ensure_race_payload(None, item["race_id"])
                new_payload["race"] = {**replacement, "rescheduled_from": race["date"]}
                save_race_json(new_path, new_payload)


def update_race_cancellations(races: list[dict], config: dict, now: datetime,
                              root: Path | None = None) -> None:
    now = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    candidates = {}
    cancelled = []
    for path in list_race_files(config, now.date().isoformat(), root):
        payload = load_race_json(path)
        if not payload:
            continue
        if payload["race"].get("cancelled"):
            cancelled.append(payload)
            state = load_automation_state(path, config, root) or {}
            if state.get("phases", {}).get("result"):
                execute_phases([{"race_id": payload["meta"]["race_id"], "race": payload["race"]}], config, now, root)
        elif (payload["race"].get("date") == now.date().isoformat()
              and payload["race"].get("track") in config["target_races"] and not payload.get("result")):
            candidates[path] = payload
    for item in races:
        race = item["race"]
        if race["date"] != now.date().isoformat() or race["track"] not in config["target_races"]:
            continue
        path = race_json_path(config, race["date"], race["track"], race["race_number"], root)
        if path not in candidates:
            payload = load_race_json(path) or ensure_race_payload(None, item["race_id"])
            if not payload.get("race"):
                payload["race"] = dict(race)
            if not payload["race"].get("cancelled") and not payload.get("result"):
                candidates[path] = payload
    if not candidates:
        return
    cache_path = data_dir(config, root) / "automation" / "discovery_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    checked = parse_jst_datetime(cache.get("cancellation_checked_at"))
    if checked is not None and timedelta(0) <= now - checked < timedelta(hours=1):
        return
    # Record the attempt before I/O, including failed requests, to avoid ten-minute polling.
    cache["cancellation_checked_at"] = now.isoformat()
    atomic_write_json(cache_path, cache)
    try:
        with requests.Session() as session:
            confirmed_urls = {p["race"].get("cancellation", {}).get("source_url") for p in cancelled}
            notices = fetch_cancellation_notices(session, since=now.date().isoformat(), excluded_urls=confirmed_urls)
    except requests.RequestException as exc:
        print(f"Cancellation notices unavailable: {exc}")
        return
    changed = []
    for path, payload in candidates.items():
        race = payload["race"]
        for url, html in notices:
            record = parse_cancellation_notice(html, race, url)
            if record is None:
                continue
            record_phase_started(path, config, payload["meta"]["race_id"], "result", root)
            race.update(cancelled=True, cancellation=record)
            save_race_json(path, payload)
            changed.append(path)
            for phase in ("general", "statistical"):
                clear_phase_state(path, config, phase, root)
            break
    if changed:
        try:
            published = publish_post_results(changed, config, "cancellation", root)
            if set(published) != set(changed):
                raise RuntimeError("cancellation publication incomplete")
        except (Exception, SystemExit) as exc:
            for path in changed:
                record_phase_failure(path, config, load_race_json(path)["meta"]["race_id"],
                                     "result", f"{type(exc).__name__}: {exc}", now, root)
        else:
            for path in changed:
                clear_phase_state(path, config, "result", root)


def result_phase_complete(payload: dict) -> bool:
    if not payload.get("result"):
        return False
    for prediction in prediction_entries(payload):
        evaluation = linked_record(payload, "evaluation", prediction["id"])
        for method in ("general", "statistical"):
            if prediction.get(method) and not evaluation.get(method):
                return False
    for entry in payload.get("simulation") or []:
        for method in ("general", "statistical"):
            simulation = entry.get(method) or {}
            for ticket in ("win", "quinella"):
                for purchase in ("value", "dutching"):
                    phases = (simulation.get(ticket) or {}).get(purchase) or {}
                    if phases.get("pre") is not None and phases.get("post") is None:
                        return False
            quinella = simulation.get("quinella") or {}
            if quinella.get("status") == "ready" and quinella.get("post_status") != "settled":
                return False
    return True


def add_pending_races(races: list[dict], config: dict, now: datetime,
                      root: Path | None = None) -> list[dict]:
    current = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    pending = list(races)
    seen = {(item["race_id"], item["race"]["date"]) for item in races}
    for state_path in sorted((data_dir(config, root) / "automation").glob("*/*.json")):
        if state_path.parent.name > current.date().isoformat():
            continue
        path = data_dir(config, root) / "races" / state_path.parent.name / state_path.name
        state = load_automation_state(path, config, root)
        phases = [phase for phase in ("statistical", "general", "result")
                  if state["phases"].get(phase, {}).get("status") in ("retry_wait", "in_progress")]
        if not phases:
            continue
        payload = load_race_json(path)
        if not payload:
            continue
        race = payload["race"]
        if (payload["meta"]["race_id"] != state["race_id"] or race["date"] != state_path.parent.name
                or race_json_path(config, race["date"], race["track"], race["race_number"], root) != path):
            raise ValueError("pending race identity mismatch")
        key = (state["race_id"], race["date"])
        if key not in seen:
            pending.append({"race_id": state["race_id"], "race": race, "phases": phases})
            seen.add(key)
    return pending


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
            if phase not in item.get("phases", ("statistical", "general", "result")):
                continue
            saved_input = False
            invalid_input = False
            if phase != "result":
                input_path = prediction_input_path(config, path, phase, root)
                if input_path.is_file():
                    try:
                        snapshot = json.loads(input_path.read_text(encoding="utf-8"))
                        validator = validate_statistical_prediction_input if phase == "statistical" else validate_prediction_input
                        validator(snapshot, {"meta": {"race_id": race_id}, "race": race})
                        saved_input = True
                    except (OSError, ValueError, TypeError, KeyError):
                        invalid_input = True
            record = (state or {}).get("phases", {}).get(phase, {})
            completed = result_phase_complete(payload) if phase == "result" else bool(prediction_for_method(payload, phase))
            reason = None
            cancelled = payload.get("race", {}).get("cancelled")
            if cancelled and (phase != "result" or not record):
                reason = "cancelled"
            elif record.get("status") == "blocked":
                reason = "blocked"
            elif record.get("status") == "retry_wait" and current < parse_jst_datetime(record["next_retry_at"]):
                reason = "retry_wait"
            elif completed and not record:
                reason = "completed"
            elif invalid_input:
                reason = "invalid_prediction_input"
            elif phase == "result" and not cancelled and not any(prediction_for_method(payload, method) for method in ("general", "statistical")):
                reason = "no_prediction"
            elif phase != "result" and not completed and payload.get("result"):
                reason = "result_exists"
            elif phase != "result" and current >= start and (not saved_input or not (runtime_prediction_entry(payload or None, config) or {}).get(phase)):
                reason = "missed_execution_window"
            elif current < scheduled_at and not cancelled:
                reason = "not_scheduled_yet"
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
        for phase in item.get("phases", ("statistical", "general", "result")):
            key = (race_id, race["date"], phase)
            if key in seen:
                continue
            seen.add(key)
            # Refresh both time and saved artifacts after each preceding phase.
            decision = next(d for d in decide_phases([item], config, now, root) if d["phase"] == phase)
            if decision["reason"] in ("completed", "cancelled"):
                clear_phase_state(path, config, phase, root)
                continue
            if decision["reason"] == "missed_execution_window":
                record_failure(path, config, race_id, phase, "missed_execution_window", status="blocked", root=root)
                continue
            if not decision["runnable"]:
                continue
            record_phase_started(path, config, race_id, phase, root)
            error = None
            try:
                cancelled = (load_race_json(path) or {}).get("race", {}).get("cancelled")
                if phase == "result" and cancelled:
                    processed = publish_post_results([path], config, "cancellation", root)
                elif phase == "result":
                    processed = run_post_flow(config, race["date"], "post", race_id=race_id)
                else:
                    processed = run_pre_flow(config, race["date"], phase=phase,
                                 resume=decision["mode"] == "resume", race_id=race_id)
                payload = load_race_json(path) or {}
                completed = result_phase_complete(payload) if phase == "result" else bool(prediction_for_method(payload, phase))
                if path not in processed or payload.get("meta", {}).get("race_id") != race_id or not (completed or (phase == "result" and cancelled)):
                    error = f"{phase} artifact missing after execution"
            except (Exception, SystemExit) as exc:
                error = f"{type(exc).__name__}: {exc}"
            if phase != "result" and (load_race_json(path) or {}).get("race", {}).get("cancelled"):
                clear_phase_state(path, config, phase, root)
                continue
            if error is None:
                clear_phase_state(path, config, phase, root)
                continue
            record_phase_failure(path, config, race_id, phase, error, now, root)


def record_phase_failure(path: Path, config: dict, race_id: str, phase: str, error: str,
                         now: datetime | None = None, root: Path | None = None) -> None:
    settings = config["automation"]
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
        if args.execute:
            races = add_pending_races(races, config, current)
        decisions = decide_phases(races, config, current)
        for item in races:
            race = item["race"]
            print(f"{race['date']} {race['track']}{race['race_number']}R {race['race_name']}")
            for decision in decisions:
                if decision["race_id"] == item["race_id"] and decision["date"] == race["date"]:
                    status = decision["mode"] if decision["runnable"] else decision["reason"]
                    print(f"{decision['phase']}: {decision['scheduled_at']:%Y-%m-%d %H:%M} JST ({status})")
        if args.execute:
            update_race_cancellations(races, config, current)
            execute_phases(races, config)
            deploy_site(config)


if __name__ == "__main__":
    main()
