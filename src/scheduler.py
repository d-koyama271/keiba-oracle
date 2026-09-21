from __future__ import annotations

import argparse
import errno
import json
import os
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
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
from utils import (calculate_phase_times, JST, atomic_write_json, data_dir, load_config, load_race_json, now_jst, prediction_input_path,
                   list_race_files, ensure_race_payload, save_race_json, prediction_entries, linked_record, runtime_prediction_entry,
                   parse_jst_datetime, prediction_for_method, race_json_path,
                   race_start_datetime, track_name_from_race_id)


@dataclass(frozen=True)
class PhaseTask:
    date: str
    race_id: str
    phase: str
    path: Path
    race: dict
    scheduled_at: datetime

    @property
    def key(self) -> tuple[str, str, str]:
        return self.date, self.race_id, self.phase

    @classmethod
    def from_race(cls, race_id: str, race: dict, phase: str, config: dict,
                  root: Path | None = None) -> PhaseTask:
        return cls(race["date"], race_id, phase,
                   race_json_path(config, race["date"], race["track"], race["race_number"], root),
                   dict(race), calculate_phase_times(race, config)[phase])


def create_phase_tasks(races: list[dict], config: dict, root: Path | None = None) -> list[PhaseTask]:
    return [PhaseTask.from_race(item["race_id"], item["race"], phase, config, root)
            for item in races for phase in ("statistical", "general", "result")]


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
                              root: Path | None = None) -> list[PhaseTask]:
    tasks = []
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
                tasks.extend(create_phase_tasks([{"race_id": payload["meta"]["race_id"], "race": payload["race"]}], config, root))
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
        return tasks
    cache_path = data_dir(config, root) / "automation" / "discovery_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    checked = parse_jst_datetime(cache.get("cancellation_checked_at"))
    if checked is not None and timedelta(0) <= now - checked < timedelta(hours=1):
        return tasks
    # Record the attempt before I/O, including failed requests, to avoid ten-minute polling.
    cache["cancellation_checked_at"] = now.isoformat()
    atomic_write_json(cache_path, cache)
    try:
        with requests.Session() as session:
            confirmed_urls = {p["race"].get("cancellation", {}).get("source_url") for p in cancelled}
            notices = fetch_cancellation_notices(session, since=now.date().isoformat(), excluded_urls=confirmed_urls)
    except requests.RequestException as exc:
        print(f"Cancellation notices unavailable: {exc}")
        return tasks
    for path, payload in candidates.items():
        race = payload["race"]
        for url, html in notices:
            record = parse_cancellation_notice(html, race, url)
            if record is None:
                continue
            race_tasks = create_phase_tasks([{"race_id": payload["meta"]["race_id"], "race": race}], config, root)
            # Persist the recovery obligation before saving cancellation evidence.
            _transition_task(race_tasks[-1], "running", config, now, root)
            race.update(cancelled=True, cancellation=record)
            save_race_json(path, payload)
            tasks.extend(race_tasks)
            break
    return tasks


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


def add_pending_tasks(tasks: list[PhaseTask], config: dict, now: datetime,
                      root: Path | None = None) -> list[PhaseTask]:
    current = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    pending = list(tasks)
    seen = {task.key for task in tasks}
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
        for phase in phases:
            task = PhaseTask.from_race(state["race_id"], race, phase, config, root)
            if task.key not in seen:
                pending.append(task)
                seen.add(task.key)
    return pending


def _phase_artifact_complete(payload: dict, phase: str) -> bool:
    return result_phase_complete(payload) if phase == "result" else bool(prediction_for_method(payload, phase))


def decide_phase(task: PhaseTask, config: dict, now: datetime | None = None,
                 root: Path | None = None) -> dict:
    current = now if now is not None else now_jst()
    current = current.replace(tzinfo=JST) if current.tzinfo is None else current.astimezone(JST)
    payload = load_race_json(task.path) or {}
    automation = load_automation_state(task.path, config, root) or {}
    if payload and payload["meta"].get("race_id") != task.race_id:
        raise ValueError("race JSON race_id mismatch")
    if automation and automation["race_id"] != task.race_id:
        raise ValueError("automation state race_id mismatch")
    record = automation.get("phases", {}).get(task.phase, {})
    cancelled = bool(payload.get("race", {}).get("cancelled"))
    artifact_complete = _phase_artifact_complete(payload, task.phase)
    state = record.get("status") or ("completed" if artifact_complete or
                                    (cancelled and task.phase == "result") else "scheduled")
    if state == "in_progress":
        state = "running"

    # Execution mode is independent of lifecycle and race-level eligibility.
    mode, reason = "normal", None
    if task.phase != "result":
        input_path = prediction_input_path(config, task.path, task.phase, root)
        if input_path.is_file():
            try:
                snapshot = json.loads(input_path.read_text(encoding="utf-8"))
                validator = validate_statistical_prediction_input if task.phase == "statistical" else validate_prediction_input
                validator(snapshot, {"meta": {"race_id": task.race_id}, "race": task.race})
                mode = "resume"
            except (OSError, ValueError, TypeError, KeyError):
                reason = "invalid_prediction_input"
    excluded = cancelled and task.phase != "result"
    due = cancelled or current >= task.scheduled_at
    if state == "retry_wait":
        due = due and current >= parse_jst_datetime(record["next_retry_at"])
    if excluded:
        reason = "cancelled"
    elif state not in ("completed", "blocked") and due and reason is None:
        if task.phase == "result":
            if not cancelled and not any(prediction_for_method(payload, method) for method in ("general", "statistical")):
                reason = "no_prediction"
        elif not artifact_complete and payload.get("result"):
            reason = "result_exists"
        elif current >= race_start_datetime(task.date, task.race["start_time"]) and (
                mode != "resume" or not (runtime_prediction_entry(payload or None, config) or {}).get(task.phase)):
            reason = "missed_execution_window"
    return {
        "race_id": task.race_id, "date": task.date, "phase": task.phase,
        "scheduled_at": task.scheduled_at, "state": state, "mode": mode,
        "runnable": state not in ("completed", "blocked") and due and reason is None,
        "excluded": excluded, "reason": reason,
    }


def decide_phases(tasks: list[PhaseTask], config: dict, now: datetime | None = None,
                  root: Path | None = None) -> list[dict]:
    current = now if now is not None else now_jst()
    return [decide_phase(task, config, current, root) for task in tasks]


def _transition_task(task: PhaseTask, state: str, config: dict, now: datetime | None,
                     root: Path | None, error: str = "") -> None:
    if state == "running":
        record_phase_started(task.path, config, task.race_id, task.phase, root)
    elif state == "completed":
        clear_phase_state(task.path, config, task.phase, root)
    else:
        settings = config["automation"]
        record = (load_automation_state(task.path, config, root) or {}).get("phases", {}).get(task.phase, {})
        prefix = "result_" if task.phase == "result" else ""
        blocked = state == "blocked" or record.get("attempts", 0) + 1 >= settings[f"{prefix}max_attempts"]
        current = now if now is not None else now_jst()
        current = current.replace(tzinfo=JST) if current.tzinfo is None else current.astimezone(JST)
        retry_at = current + timedelta(minutes=settings[f"{prefix}retry_interval_minutes"])
        record_failure(task.path, config, task.race_id, task.phase, error,
                       status="blocked" if blocked else "retry_wait",
                       next_retry_at=None if blocked else retry_at.isoformat(), root=root)


def execute_phases(tasks: list[PhaseTask], config: dict, now: datetime | None = None,
                   root: Path | None = None) -> None:
    settings = config["automation"]
    for key in ("retry_interval_minutes", "max_attempts", "result_retry_interval_minutes", "result_max_attempts"):
        if type(settings.get(key)) is not int or settings[key] <= 0:
            raise ValueError(f"automation.{key} must be a positive integer")
    seen = set()
    for task in tasks:
        if task.key in seen:
            continue
        seen.add(task.key)
        # Refresh time, artifacts and state after every preceding task.
        decision = decide_phase(task, config, now, root)
        if decision["excluded"]:
            _transition_task(task, "completed", config, now, root)
            continue
        if decision["reason"] == "missed_execution_window":
            _transition_task(task, "blocked", config, now, root, "missed_execution_window")
            continue
        if not decision["runnable"]:
            continue
        _transition_task(task, "running", config, now, root)
        error = None
        try:
            cancelled = (load_race_json(task.path) or {}).get("race", {}).get("cancelled")
            if task.phase == "result" and cancelled:
                processed = publish_post_results([task.path], config, "cancellation", root)
            elif task.phase == "result":
                processed = run_post_flow(config, task.date, "post", race_id=task.race_id)
            else:
                processed = run_pre_flow(config, task.date, phase=task.phase,
                                         resume=decision["mode"] == "resume", race_id=task.race_id)
            payload = load_race_json(task.path) or {}
            complete = _phase_artifact_complete(payload, task.phase) or (task.phase == "result" and cancelled)
            if task.path not in processed or payload.get("meta", {}).get("race_id") != task.race_id or not complete:
                error = f"{task.phase} artifact missing after execution"
        except (Exception, SystemExit) as exc:
            error = f"{type(exc).__name__}: {exc}"
        if task.phase != "result" and (load_race_json(task.path) or {}).get("race", {}).get("cancelled"):
            error = None
        _transition_task(task, "completed" if error is None else "retry_wait", config, now, root, error or "")


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
        tasks = create_phase_tasks(races, config)
        if args.execute:
            tasks = add_pending_tasks(tasks, config, current)
        decisions = decide_phases(tasks, config, current)
        displayed = set()
        for task, decision in zip(tasks, decisions):
            if (task.date, task.race_id) not in displayed:
                print(f"{task.date} {task.race['track']}{task.race['race_number']}R {task.race['race_name']}")
                displayed.add((task.date, task.race_id))
            detail = decision["reason"] or decision["mode"]
            print(f"{task.phase}: {task.scheduled_at:%Y-%m-%d %H:%M} JST ({decision['state']}, {detail})")
        if args.execute:
            tasks.extend(update_race_cancellations(races, config, current))
            execute_phases(tasks, config)
            deploy_site(config)


if __name__ == "__main__":
    main()
