from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from utils import atomic_write_json, data_dir, now_jst_iso, parse_jst_datetime, resolve_path

PHASES = {"statistical", "general", "result"}


def automation_state_path(race_path: str | Path, config: dict, root: Path | None = None) -> Path:
    directory = data_dir(config, root).resolve()
    relative = resolve_path(race_path, root).resolve().relative_to(directory / "races")
    if len(relative.parts) != 2 or relative.suffix != ".json":
        raise ValueError("race path must be data_dir/races/YYYY-MM-DD/<stem>.json")
    if date.fromisoformat(relative.parent.name).isoformat() != relative.parent.name:
        raise ValueError("race path date must be YYYY-MM-DD")
    return directory / "automation" / relative


def validate_automation_state(state: Any) -> None:
    if not isinstance(state, dict) or set(state) != {"race_id", "phases"}:
        raise ValueError("invalid automation state structure")
    if not isinstance(state["race_id"], str) or not state["race_id"].strip():
        raise ValueError("automation state requires race_id")
    if not isinstance(state["phases"], dict):
        raise ValueError("automation phases must be an object")
    for phase, record in state["phases"].items():
        if phase not in PHASES:
            raise ValueError(f"invalid automation phase: {phase}")
        if not isinstance(record, dict) or set(record) != {
            "status", "attempts", "next_retry_at", "last_error", "updated_at",
        }:
            raise ValueError(f"invalid automation phase state: {phase}")
        if record["status"] not in ("retry_wait", "blocked", "in_progress"):
            raise ValueError(f"invalid automation status: {phase}")
        if type(record["attempts"]) is not int or record["attempts"] < (0 if record["status"] == "in_progress" else 1):
            raise ValueError(f"invalid automation attempts: {phase}")
        if not isinstance(record["last_error"], str):
            raise ValueError(f"invalid automation last_error: {phase}")
        timestamps = [record["updated_at"]]
        if record["status"] == "retry_wait":
            timestamps.append(record["next_retry_at"])
        elif record["next_retry_at"] is not None:
            raise ValueError(f"non-waiting phase must have null next_retry_at: {phase}")
        if any(not isinstance(value, str) or parse_jst_datetime(value) is None for value in timestamps):
            raise ValueError(f"invalid automation timestamp: {phase}")


def load_automation_state(race_path: str | Path, config: dict, root: Path | None = None) -> dict | None:
    path = automation_state_path(race_path, config, root)
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    validate_automation_state(state)
    return state


def record_failure(
    race_path: str | Path, config: dict, race_id: str, phase: str, last_error: str,
    *, status: str = "retry_wait", next_retry_at: str | None = None, root: Path | None = None,
) -> dict:
    if status not in ("retry_wait", "blocked"):
        raise ValueError(f"invalid failure status: {status}")
    state = load_automation_state(race_path, config, root)
    if state is None:
        state = {"race_id": race_id, "phases": {}}
    elif state["race_id"] != race_id:
        raise ValueError("automation state race_id mismatch")
    previous = state["phases"].get(phase, {})
    state["phases"][phase] = {
        "status": status,
        "attempts": previous.get("attempts", 0) + 1,
        "next_retry_at": None if status == "blocked" else next_retry_at,
        "last_error": last_error,
        "updated_at": now_jst_iso(),
    }
    validate_automation_state(state)
    atomic_write_json(automation_state_path(race_path, config, root), state)
    return state


def record_phase_started(
    race_path: str | Path, config: dict, race_id: str, phase: str, root: Path | None = None,
) -> None:
    state = load_automation_state(race_path, config, root) or {"race_id": race_id, "phases": {}}
    if state["race_id"] != race_id:
        raise ValueError("automation state race_id mismatch")
    previous = state["phases"].get(phase, {})
    state["phases"][phase] = {
        "status": "in_progress", "attempts": previous.get("attempts", 0),
        "next_retry_at": None, "last_error": previous.get("last_error", ""),
        "updated_at": now_jst_iso(),
    }
    validate_automation_state(state)
    atomic_write_json(automation_state_path(race_path, config, root), state)


def clear_phase_state(race_path: str | Path, config: dict, phase: str, root: Path | None = None) -> None:
    if phase not in PHASES:
        raise ValueError(f"invalid automation phase: {phase}")
    state = load_automation_state(race_path, config, root)
    if state is None or phase not in state["phases"]:
        return
    del state["phases"][phase]
    path = automation_state_path(race_path, config, root)
    if state["phases"]:
        atomic_write_json(path, state)
    else:
        path.unlink()
