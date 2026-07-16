from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

JST = timezone(timedelta(hours=9), name="Asia/Tokyo")
SCHEMA_VERSION = 1
REQUIRED_TOP_LEVEL_KEYS = ("meta", "race", "horses", "prediction", "simulation", "result", "feedback")
TRACK_CODE_TO_NAME = {
    "01": "札幌",
    "02": "函館",
    "03": "福島",
    "04": "新潟",
    "05": "東京",
    "06": "中山",
    "07": "中京",
    "08": "京都",
    "09": "阪神",
    "10": "小倉",
}
TRACK_NAME_TO_SLUG = {
    "札幌": "sapporo",
    "函館": "hakodate",
    "福島": "fukushima",
    "新潟": "niigata",
    "東京": "tokyo",
    "中山": "nakayama",
    "中京": "chukyo",
    "京都": "kyoto",
    "阪神": "hanshin",
    "小倉": "kokura",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def now_jst() -> datetime:
    return datetime.now(JST)


def now_jst_iso() -> str:
    return now_jst().isoformat(timespec="seconds")


def today_jst() -> str:
    return now_jst().date().isoformat()


def parse_jst_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def race_start_datetime(race_date: str | None, start_time: str | None) -> datetime | None:
    if not race_date or not start_time:
        return None
    return parse_jst_datetime(f"{race_date}T{start_time}")


def parse_target_date(value: str | None) -> str:
    if not value:
        return today_jst()
    return date.fromisoformat(value).isoformat()


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else repo_root() / "config" / "app.yaml"
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    required = {
        "target_races",
        "odds_reference_minutes_before_start",
        "race_budget",
        "ev_threshold",
        "kelly_fraction",
        "stake_unit",
        "publish_mode",
        "llm_provider",
        "llm_model",
        "data_dir",
        "public_dir",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(missing)}")
    return config


def resolve_path(value: str | Path, root: Path | None = None) -> Path:
    root = root or repo_root()
    path = Path(value)
    return path if path.is_absolute() else root / path


def data_dir(config: dict[str, Any], root: Path | None = None) -> Path:
    return resolve_path(config["data_dir"], root)


def public_dir(config: dict[str, Any], root: Path | None = None) -> Path:
    return resolve_path(config["public_dir"], root)


def inbox_dir(kind: str, root: Path | None = None) -> Path:
    return resolve_path(Path("inbox") / kind, root or repo_root())


def outbox_chat_input_dir(kind: str, root: Path | None = None) -> Path:
    return resolve_path(Path("outbox") / "chat_input" / kind, root or repo_root())


def stage_dir(config: dict[str, Any], root: Path | None = None) -> Path:
    return data_dir(config, root) / "_site_stage"


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def slugify_track(track_name: str) -> str:
    if track_name in TRACK_NAME_TO_SLUG:
        return TRACK_NAME_TO_SLUG[track_name]
    cleaned = re.sub(r"[^a-z0-9]+", "-", track_name.lower()).strip("-")
    return cleaned or "track"


def track_name_from_race_id(race_id: str) -> str:
    return TRACK_CODE_TO_NAME.get(race_id[4:6], race_id[4:6])


def default_race_payload(race_id: str) -> dict[str, Any]:
    timestamp = now_jst_iso()
    return {
        "meta": {
            "race_id": race_id,
            "schema_version": SCHEMA_VERSION,
            "created_at": timestamp,
            "updated_at": timestamp,
            "pre_status": None,
            "post_status": None,
        },
        "race": {},
        "horses": [],
        "prediction": None,
        "simulation": {
            "pre": None,
            "post": None,
        },
        "result": None,
        "feedback": None,
    }


def ensure_race_payload(payload: dict[str, Any] | None, race_id: str | None = None) -> dict[str, Any]:
    base = default_race_payload(race_id or "")
    payload = payload or {}
    merged = dict(base)
    merged.update(payload)
    merged["meta"] = dict(base["meta"])
    merged["meta"].update(payload.get("meta", {}))
    if race_id:
        merged["meta"]["race_id"] = race_id
    merged["meta"]["schema_version"] = SCHEMA_VERSION
    if not merged["meta"].get("created_at"):
        merged["meta"]["created_at"] = now_jst_iso()
    merged["meta"]["updated_at"] = now_jst_iso()
    if not isinstance(merged.get("simulation"), dict):
        merged["simulation"] = {"pre": None, "post": None}
    else:
        merged["simulation"] = {
            "pre": merged["simulation"].get("pre"),
            "post": merged["simulation"].get("post"),
        }
    for key in REQUIRED_TOP_LEVEL_KEYS:
        merged.setdefault(key, base.get(key))
    return merged


def atomic_write_text(path: str | Path, content: str) -> None:
    destination = Path(path)
    ensure_dir(destination.parent)
    tmp_path = destination.with_name(f"{destination.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(destination)


def atomic_write_json(path: str | Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def load_race_json(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return ensure_race_payload(payload, payload.get("meta", {}).get("race_id"))


def save_race_json(path: str | Path, payload: dict[str, Any]) -> None:
    current = ensure_race_payload(payload, payload.get("meta", {}).get("race_id"))
    atomic_write_json(path, current)


def set_race_status(payload: dict[str, Any], *, pre_status: str | None = None, post_status: str | None = None) -> None:
    meta = payload.setdefault("meta", {})
    if pre_status is not None:
        meta["pre_status"] = pre_status
    if post_status is not None:
        meta["post_status"] = post_status


def race_json_path(
    config: dict[str, Any],
    race_date: str,
    track_name: str,
    race_number: int = 11,
    root: Path | None = None,
) -> Path:
    file_name = f"{slugify_track(track_name)}_{race_number}r.json"
    return data_dir(config, root) / "races" / race_date / file_name


def race_html_path(
    race_date: str,
    track_name: str,
    race_number: int = 11,
) -> Path:
    file_name = f"{slugify_track(track_name)}_{race_number}r.html"
    return Path("races") / race_date / file_name


def list_race_files(config: dict[str, Any], race_date: str | None = None, root: Path | None = None) -> list[Path]:
    base_dir = data_dir(config, root) / "races"
    if not base_dir.exists():
        return []
    if race_date:
        return sorted((base_dir / race_date).glob("*_11r.json"))
    return sorted(base_dir.glob("*/*_11r.json"))


def find_race_file_by_race_id(config: dict[str, Any], race_id: str, root: Path | None = None) -> Path | None:
    for path in list_race_files(config, None, root):
        payload = load_race_json(path)
        if payload and payload.get("meta", {}).get("race_id") == race_id:
            return path
    return None


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def normalize_space(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = normalize_space(str(value)).replace(",", "")
    match = re.search(r"-?\d+", text)
    return int(match.group()) if match else None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = normalize_space(str(value)).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def parse_finish_position(value: Any) -> int | None:
    if value is None:
        return None
    text = normalize_space(str(value))
    if any(flag in text for flag in ("中止", "除外", "取消", "失格")):
        return None
    return parse_int(text)


def latest_feedback_summaries(
    config: dict[str, Any],
    before_date: str | None = None,
    limit: int = 3,
    root: Path | None = None,
) -> list[str]:
    summaries: list[str] = []
    for path in reversed(list_race_files(config, None, root)):
        payload = load_race_json(path)
        if not payload:
            continue
        race_date = payload.get("race", {}).get("date")
        if before_date and race_date and race_date >= before_date:
            continue
        feedback = payload.get("feedback") or {}
        summary = feedback.get("next_prediction_adjustment_summary")
        if summary:
            summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries


def setup_logger(job_name: str, config: dict[str, Any], root: Path | None = None) -> logging.Logger:
    logger_name = f"keiba_oracle.{job_name}"
    logger = logging.getLogger(logger_name)
    log_path = data_dir(config, root) / "job.log"
    desired_log_path = str(log_path)

    if logger.handlers and getattr(logger, "_log_path", None) == desired_log_path:
        return logger

    if logger.handlers:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    ensure_dir(log_path.parent)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = False
    logger._log_path = desired_log_path  # type: ignore[attr-defined]
    return logger


def log_job(logger: logging.Logger, job_name: str, race_id: str | None, message: str) -> None:
    logger.info("[%s][%s] %s", job_name, race_id or "-", message)
