from __future__ import annotations

from datetime import datetime, timedelta

import requests

from collect import SHUTUBA_URL, discover_race_ids, fetch_html, parse_race_overview
from utils import JST, load_config, now_jst, race_start_datetime, track_name_from_race_id


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


def main() -> None:
    for item in discover_scheduled_races(load_config()):
        race = item["race"]
        print(f"{race['date']} {race['track']}{race['race_number']}R {race['race_name']}")
        for phase, scheduled_at in item["scheduled_at"].items():
            print(f"{phase}: {scheduled_at:%Y-%m-%d %H:%M} JST")


if __name__ == "__main__":
    main()
