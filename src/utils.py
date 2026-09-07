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
SCHEMA_VERSION = 10
REQUIRED_TOP_LEVEL_KEYS = ("meta", "race", "horses", "prediction", "simulation", "result", "evaluation")
GENERAL_PREDICTION_METHOD = "general"
STATISTICAL_PREDICTION_METHOD = "statistical"
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
        "simulation",
        "publish_mode",
        "llm_provider",
        "llm_model",
        "data_dir",
        "public_dir",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(missing)}")

    simulation = config.get("simulation") or {}
    value = simulation.get("value") or {}
    dutching = simulation.get("dutching") or {}
    simulation_required = {"budget", "stake_unit", "value", "dutching"}
    value_required = {"ev_threshold", "kelly_fraction"}
    dutching_required = {
        "max_selection_count",
        "min_coverage_probability",
        "min_group_expected_value",
        "min_profit_rate",
    }
    missing_simulation = sorted(simulation_required - set(simulation))
    missing_value = sorted(value_required - set(value))
    missing_dutching = sorted(dutching_required - set(dutching))
    if missing_simulation or missing_value or missing_dutching:
        missing_paths = [f"simulation.{key}" for key in missing_simulation]
        missing_paths += [f"simulation.value.{key}" for key in missing_value]
        missing_paths += [f"simulation.dutching.{key}" for key in missing_dutching]
        raise ValueError(f"Missing config keys: {', '.join(missing_paths)}")
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
            "post_status": "awaiting_result",
        },
        "race": {},
        "horses": [],
        "prediction": [],
        "simulation": [],
        "result": None,
        "evaluation": [],
    }


def ensure_race_payload(payload: dict[str, Any] | None, race_id: str | None = None) -> dict[str, Any]:
    base = default_race_payload(race_id or "")
    payload = payload or {}
    source_schema_version = payload.get("meta", {}).get("schema_version", 0)
    merged = {key: payload.get(key, base[key]) for key in REQUIRED_TOP_LEVEL_KEYS}
    merged["meta"] = dict(base["meta"])
    merged["meta"].update(payload.get("meta", {}))
    if race_id:
        merged["meta"]["race_id"] = race_id
    merged["meta"]["schema_version"] = SCHEMA_VERSION
    if not merged["meta"].get("created_at"):
        merged["meta"]["created_at"] = now_jst_iso()
    if source_schema_version < 9 and merged.get("evaluation") is None:
        merged["meta"]["post_status"] = "awaiting_result"
    merged["meta"]["updated_at"] = now_jst_iso()
    if not isinstance(merged.get("prediction"), list):
        legacy_prediction = merged.get("prediction") or {}
        predictions = []
        identities = {}
        identity_fields = {"method", "model_provider", "model_name", "reasoning_effort", "variants"}

        def entry_for(record):
            identity = (record.get("model_provider"), record.get("model_name"))
            if identity not in identities:
                runtime, model = identity
                entry = {
                    "id": f"p{len(predictions) + 1}",
                    "provider": "OpenAI" if runtime in ("codex", "openai") or str(model).startswith("gpt-") else None,
                    "family": "GPT" if str(model).startswith("gpt-") else None,
                    "model": model,
                    "runtime_provider": runtime,
                    "reasoning_effort": record.get("reasoning_effort"),
                }
                identities[identity] = entry
                predictions.append(entry)
            return identities[identity]

        legacy_records = ([legacy_prediction] if legacy_prediction.get("horses") else []) + legacy_prediction.get("variants", [])
        for record in legacy_records:
            method = record.get("method", "traditional")
            method = "general" if method == "traditional" else method
            entry_for(record)[method] = {k: v for k, v in record.items() if k not in identity_fields}
        for section in ("simulation", "evaluation"):
            if isinstance(merged.get(section), list):
                continue
            legacy = merged.get(section) or {}
            records = ([legacy] if any(k != "variants" for k in legacy) else []) + legacy.get("variants", [])
            converted = []
            for record in records:
                method = record.get("method", "traditional")
                method = "general" if method == "traditional" else method
                identity = legacy_prediction if record is legacy else record
                # Older evaluation variants sometimes omitted model metadata.
                candidates = [e for e in predictions if method in e and all(identity.get(old) is None or e.get(new) == identity[old] for old, new in (("model_provider", "runtime_provider"), ("model_name", "model")))]
                entry = candidates[0] if len(candidates) == 1 else entry_for(identity)
                linked = next((e for e in converted if e["prediction_id"] == entry["id"]), None)
                if linked is None:
                    linked = {"prediction_id": entry["id"]}
                    converted.append(linked)
                content = {k: v for k, v in record.items() if k not in identity_fields}
                if section == "simulation":
                    linked[method] = {"win": {k: content[k] for k in ("value", "dutching") if k in content}}
                    if "quinella" in content:
                        linked[method]["quinella"] = content["quinella"]
                else:
                    linked[method] = content
            merged[section] = converted
        merged["prediction"] = predictions
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


def prediction_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    prediction = payload.get("prediction")
    return prediction if isinstance(prediction, list) else ensure_race_payload(payload)["prediction"]


def prediction_for_method(payload: dict[str, Any], method: str = "general") -> dict[str, Any] | None:
    return next((entry[method] for entry in prediction_entries(payload) if entry.get(method)), None)


def linked_record(payload: dict[str, Any], section: str, prediction_id: str) -> dict[str, Any]:
    records = payload.get(section)
    if not isinstance(records, list):
        records = ensure_race_payload(payload)[section]
    return next((record for record in records if record["prediction_id"] == prediction_id), {})


def runtime_prediction_entry(payload: dict[str, Any], config: dict[str, Any], create: bool = False) -> dict[str, Any] | None:
    if payload is None:
        return None
    entries = payload["prediction"]
    runtime, model = config.get("llm_provider"), config.get("llm_model")
    metadata = {
        "provider": "OpenAI" if runtime in ("codex", "openai") or str(model).startswith("gpt-") else None,
        "family": "GPT" if str(model).startswith("gpt-") else None,
        "model": model,
        "runtime_provider": runtime,
        "reasoning_effort": config.get("llm_reasoning_effort"),
    }
    entry = next((e for e in entries if all(e.get(key) == value for key, value in metadata.items())), None)
    if entry is None and create:
        number = 1
        while any(e["id"] == f"p{number}" for e in entries):
            number += 1
        entry = {"id": f"p{number}", **metadata}
        entries.append(entry)
    return entry


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


def race_result_html_path(
    race_date: str,
    track_name: str,
    race_number: int = 11,
) -> Path:
    file_name = f"{slugify_track(track_name)}_{race_number}r_result.html"
    return Path("races") / race_date / file_name


def list_race_files(config: dict[str, Any], race_date: str | None = None, root: Path | None = None) -> list[Path]:
    base_dir = data_dir(config, root) / "races"
    if not base_dir.exists():
        return []
    if race_date:
        return sorted((base_dir / race_date).glob("*_*r.json"))
    return sorted(base_dir.glob("*/*_*r.json"))


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
