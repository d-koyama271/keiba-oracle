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
    parse_target_date,
    race_html_path,
    repo_root,
    stage_dir,
    track_name_from_race_id,
)


def build_environment(root: Path | None = None) -> Environment:
    root = root or repo_root()
    return Environment(
        loader=FileSystemLoader(root / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )


def build_race_context(payload: dict[str, Any]) -> dict[str, Any]:
    prediction = payload.get("prediction")
    simulation = payload.get("simulation") or {}
    result = payload.get("result")
    feedback = payload.get("feedback")

    prediction_lookup = {item["horse_number"]: item for item in (prediction or {}).get("horses", [])}
    result_lookup = {item["horse_number"]: item["finish_position"] for item in (result or {}).get("horses", [])}
    payout_lookup = {item["horse_number"]: item["payout_per_100"] for item in (result or {}).get("payouts", {}).get("win", [])}

    horse_rows = []
    for horse in sorted(payload.get("horses", []), key=lambda item: item["horse_number"]):
        horse_rows.append(
            {
                **horse,
                "prediction": prediction_lookup.get(horse["horse_number"]),
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

    status = "result_published" if result else "prediction_only"
    return {
        "race": payload.get("race", {}),
        "prediction": prediction,
        "simulation_pre": simulation.get("pre"),
        "simulation_post": simulation.get("post"),
        "result": result,
        "feedback": feedback,
        "horse_rows": horse_rows,
        "result_rows": result_rows,
        "status": status,
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
    ensure_dir(output_dir / "races")

    race_files = list_race_files(config, race_date, root)
    index_rows = []
    for path in race_files:
        payload = load_race_json(path)
        if not payload:
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
                "status": context["status"],
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
