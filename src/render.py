from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from utils import (
    ensure_dir,
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    parse_jst_datetime,
    parse_target_date,
    public_dir,
    race_html_path,
    race_start_datetime,
    repo_root,
    stage_dir,
    track_name_from_race_id,
)


STATUS_LABELS = {
    "prediction_only": "予想生成",
    "result_published": "結果生成",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, "処理中")


def build_odds_timing(race: dict[str, Any]) -> tuple[str, bool]:
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    captured = parse_jst_datetime(race.get("odds_captured_at"))
    if start is None or captured is None:
        return "-", False

    seconds_from_start = (captured - start).total_seconds()
    minutes = int((abs(seconds_from_start) / 60.0) + 0.5)
    if minutes == 0:
        return "発走時点", seconds_from_start > 0
    if seconds_from_start < 0:
        return f"発走{minutes}分前", False
    return f"発走{minutes}分後", True


def build_environment(root: Path | None = None) -> Environment:
    root = root or repo_root()
    return Environment(
        loader=FileSystemLoader(root / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )


def build_race_context(payload: dict[str, Any]) -> dict[str, Any]:
    race = payload.get("race", {})
    prediction = payload.get("prediction")
    simulation = payload.get("simulation") or {}
    result = payload.get("result")
    evaluation = payload.get("evaluation")
    odds_timing_label, odds_recorded_after_start = build_odds_timing(race)

    prediction_horses = (prediction or {}).get("horses", [])
    prediction_lookup = {item["horse_number"]: item for item in prediction_horses}
    prediction_ranks = {
        item["horse_number"]: rank
        for rank, item in enumerate(
            sorted(
                prediction_horses,
                key=lambda item: (-float(item["win_probability"]), item["horse_number"]),
            ),
            start=1,
        )
    }
    result_lookup = {item["horse_number"]: item["finish_position"] for item in (result or {}).get("horses", [])}
    payout_lookup = {item["horse_number"]: item["payout_per_100"] for item in (result or {}).get("payouts", {}).get("win", [])}

    horse_rows = []
    for horse in sorted(payload.get("horses", []), key=lambda item: item["horse_number"]):
        horse_rows.append(
            {
                **horse,
                "prediction": prediction_lookup.get(horse["horse_number"]),
                "prediction_rank": prediction_ranks.get(horse["horse_number"]),
                "finish_position": result_lookup.get(horse["horse_number"]),
                "payout_per_100": payout_lookup.get(horse["horse_number"]),
            }
        )

    result_rows = [
        {
            "horse_number": horse["horse_number"],
            "finish_position": result_lookup.get(horse["horse_number"]),
            "payout_per_100": payout_lookup.get(horse["horse_number"]),
        }
        for horse in sorted(payload.get("horses", []), key=lambda item: item["horse_number"])
        if horse["horse_number"] in result_lookup
    ]

    value_simulation = simulation.get("value") or {}
    dutching_simulation = simulation.get("dutching") or {}
    value_pre = value_simulation.get("pre")
    dutching_pre = dutching_simulation.get("pre")
    custom_simulation_horses = [
        {
            "horse_number": horse["horse_number"],
            "win_probability": float(horse["prediction"]["win_probability"]),
            "win_odds": float(horse["win_odds"]),
        }
        for horse in horse_rows
        if horse.get("prediction") and horse.get("win_odds") is not None
    ]
    custom_simulation_data = {
        "stake_unit": int((value_pre or dutching_pre or {}).get("stake_unit") or 100),
        "horses": custom_simulation_horses,
    }

    status = "result_published" if result and evaluation else "prediction_only"
    return {
        "race": race,
        "prediction": prediction,
        "simulation_value_pre": value_pre,
        "simulation_value_post": value_simulation.get("post"),
        "simulation_dutching_pre": dutching_pre,
        "simulation_dutching_post": dutching_simulation.get("post"),
        "result": result,
        "evaluation": evaluation,
        "horse_rows": horse_rows,
        "result_rows": result_rows,
        "status": status,
        "status_label": status_label(status),
        "odds_timing_label": odds_timing_label,
        "odds_recorded_after_start": odds_recorded_after_start,
        "custom_simulation_data": custom_simulation_data,
    }


def render_site(
    config: dict[str, Any],
    job_name: str,
    race_date: str | None = None,
    root: Path | None = None,
) -> Path:
    root = root or repo_root()
    env = build_environment(root)
    race_template = env.get_template("race.html.j2")
    index_template = env.get_template("index.html.j2")

    output_dir = stage_dir(config, root)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    current_public_dir = public_dir(config, root)
    if current_public_dir.exists():
        shutil.copytree(current_public_dir, output_dir)
    else:
        ensure_dir(output_dir)

    managed_races_dir = output_dir / "races"
    if managed_races_dir.exists():
        for managed_html in managed_races_dir.rglob("*.html"):
            managed_html.unlink()
    ensure_dir(managed_races_dir)

    race_files = list_race_files(config, race_date, root)
    index_rows = []
    for path in race_files:
        payload = load_race_json(path)
        if not payload or not payload.get("prediction"):
            continue
        context = build_race_context(payload)
        race = payload["race"]
        html = race_template.render(**context)
        relative_path = race_html_path(race["date"], race["track"], race["race_number"])
        target_path = output_dir / relative_path
        ensure_dir(target_path.parent)
        target_path.write_text(html, encoding="utf-8")
        index_rows.append(
            {
                "date": race["date"],
                "track": race["track"],
                "race_name": race["race_name"],
                "status_label": context["status_label"],
                "href": relative_path.as_posix(),
            }
        )

    index_rows.sort(key=lambda item: (item["date"], item["track"]))
    index_html = index_template.render(races=index_rows)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    render_site(config, "render", target_date)


if __name__ == "__main__":
    main()
