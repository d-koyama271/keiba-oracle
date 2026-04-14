from __future__ import annotations

import argparse
import base64
import json
import re
import zlib
from datetime import date as date_cls
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from utils import (
    ensure_race_payload,
    load_config,
    load_race_json,
    log_job,
    normalize_space,
    now_jst_iso,
    parse_finish_position,
    parse_float,
    parse_int,
    parse_target_date,
    race_json_path,
    save_race_json,
    setup_logger,
    track_name_from_race_id,
)

RACE_LIST_DATE_URL = "https://race.netkeiba.com/top/race_list_get_date_list.html?kaisai_date={date_key}"
RACE_LIST_SUB_URL = "https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_key}&current_group={current_group}#racelist_top_a"
SHUTUBA_URL = "https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
RESULT_URL = "https://race.netkeiba.com/race/result.html?race_id={race_id}"
HORSE_HISTORY_HEADERS = {"日付", "開催", "距離", "着順"}
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def parse_jsonp_body(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return json.loads(text)


def discover_race_ids(session: requests.Session, target_date: str) -> list[str]:
    date_key = target_date.replace("-", "")
    html = fetch_html(session, RACE_LIST_DATE_URL.format(date_key=date_key))
    soup = BeautifulSoup(html, "html.parser")
    active_node = soup.select_one("li.Active[date]") or soup.select_one("li[date]")
    current_group = active_node.get("group") if active_node else None
    if not current_group:
        return []

    sub_html = fetch_html(session, RACE_LIST_SUB_URL.format(date_key=date_key, current_group=current_group))
    race_ids = set(re.findall(r"race_id=(\d{12})", sub_html))
    return sorted(race_id for race_id in race_ids if race_id.endswith("11"))


def fetch_win_odds(session: requests.Session, race_id: str) -> tuple[dict[int, dict[str, Any]], str | None]:
    response = session.get(
        "https://race.netkeiba.com/api/api_get_jra_odds.html",
        headers=REQUEST_HEADERS,
        params={
            "pid": "api_get_jra_odds",
            "input": "UTF-8",
            "output": "jsonp",
            "race_id": race_id,
            "type": "all",
            "action": "init",
            "sort": "ninki",
            "compress": "1",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = parse_jsonp_body(response.text)
    if payload.get("status") != "result" or not payload.get("data"):
        return {}, None

    body = zlib.decompress(base64.b64decode(payload["data"])).decode("utf-8")
    odds_payload = json.loads(body)
    odds_rows = odds_payload.get("odds", {}).get("1", {})

    odds_map: dict[int, dict[str, Any]] = {}
    for row in odds_rows.values():
        if len(row) < 4:
            continue
        horse_number = parse_int(row[3])
        if horse_number is None:
            continue
        odds_map[horse_number] = {
            "win_odds": parse_float(row[0]),
            "popularity": parse_int(row[2]),
        }
    return odds_map, odds_payload.get("official_datetime")


def normalize_header(value: str | None) -> str:
    text = normalize_space(value)
    text = text.replace("\u3000", " ")
    return text.replace(" ", "")


def extract_headers(table: Any) -> list[str]:
    header_cells = table.select("thead tr th")
    if not header_cells:
        first_row = table.find("tr")
        header_cells = first_row.find_all("th") if first_row else []
    return [normalize_header(cell.get_text(" ", strip=True)) for cell in header_cells]


def rows_from_table(table: Any) -> list[dict[str, Any]]:
    headers = extract_headers(table)
    rows: list[dict[str, Any]] = []
    candidates = table.select("tbody tr") or table.find_all("tr")[1:]
    for row in candidates:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        row_data: dict[str, Any] = {"_row": row}
        for index, header in enumerate(headers[: len(cells)]):
            if not header:
                continue
            row_data[header] = normalize_space(cells[index].get_text(" ", strip=True))
        rows.append(row_data)
    return rows


def find_entry_table(soup: BeautifulSoup) -> Any | None:
    required = {"枠", "馬番", "馬名", "斤量", "騎手", "人気"}
    for table in soup.find_all("table"):
        headers = set(extract_headers(table))
        if required.issubset(headers):
            return table
    return None


def find_history_table(soup: BeautifulSoup) -> Any | None:
    for table in soup.find_all("table"):
        headers = set(extract_headers(table))
        if HORSE_HISTORY_HEADERS.issubset(headers):
            return table
    return None


def parse_distance_surface(distance_text: str) -> tuple[int | None, str | None]:
    distance = parse_int(distance_text)
    if "芝" in distance_text:
        return distance, "芝"
    if "ダ" in distance_text:
        return distance, "ダート"
    if "障" in distance_text:
        return distance, "障害"
    return distance, None


def parse_track_from_holding(holding_text: str) -> str:
    match = re.search(r"[^\d\s]+", holding_text or "")
    return match.group(0) if match else normalize_space(holding_text)


def summarize_record(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "該当なし"
    wins = sum(1 for run in runs if run.get("finish_position") == 1)
    places = sum(1 for run in runs if (run.get("finish_position") or 99) <= 3)
    return f"{len(runs)}戦{wins}勝{places}連対"


def running_style_summary(past_runs: list[dict[str, Any]]) -> str:
    early_positions: list[int] = []
    for run in past_runs[:3]:
        passing = normalize_space(run.get("passing_order"))
        if not passing:
            continue
        first = parse_int(passing.split("-")[0])
        if first is not None:
            early_positions.append(first)
    if not early_positions:
        return "判定材料不足"
    avg_pos = sum(early_positions) / len(early_positions)
    if avg_pos <= 2.5:
        return "先手型"
    if avg_pos <= 5.5:
        return "先行型"
    if avg_pos <= 9.0:
        return "差し型"
    return "追込型"


def build_horse_summaries(
    horse: dict[str, Any],
    current_race: dict[str, Any],
    past_runs: list[dict[str, Any]],
    last_run_jockey: str | None,
) -> None:
    horse["past_runs"] = past_runs
    last_run = past_runs[0] if past_runs else None
    current_distance = parse_int(current_race.get("distance"))
    current_surface = current_race.get("surface")
    current_track = current_race.get("track")
    current_going = current_race.get("going")

    if last_run:
        distance_delta = (current_distance or 0) - (last_run.get("distance") or 0)
        horse["distance_change"] = f"{distance_delta:+d}m"
        last_surface = last_run.get("surface") or "-"
        horse["surface_change"] = "同じ" if current_surface == last_surface else f"{last_surface}->{current_surface or '-'}"
        try:
            race_date = date_cls.fromisoformat(current_race["date"])
            last_date = date_cls.fromisoformat(last_run["date"])
            horse["days_since_last_run"] = (race_date - last_date).days
        except Exception:  # noqa: BLE001
            horse["days_since_last_run"] = None

        last_weight = last_run.get("weight_carried")
        current_weight = horse.get("weight_carried")
        if current_weight is not None and last_weight is not None:
            horse["weight_change_from_last_run"] = round(current_weight - last_weight, 1)
        else:
            horse["weight_change_from_last_run"] = None

        if last_run_jockey:
            horse["jockey_change"] = "同じ" if last_run_jockey == horse.get("jockey") else f"{last_run_jockey}から替わり"
        else:
            horse["jockey_change"] = "不明"
    else:
        horse["distance_change"] = "不明"
        horse["surface_change"] = "不明"
        horse["days_since_last_run"] = None
        horse["weight_change_from_last_run"] = None
        horse["jockey_change"] = "不明"

    same_course = [run for run in past_runs if run.get("track") == current_track and run.get("surface") == current_surface]
    same_distance = [run for run in past_runs if run.get("distance") == current_distance]
    same_going = [run for run in past_runs if run.get("going") == current_going]

    horse["same_course_record_summary"] = summarize_record(same_course)
    horse["same_distance_record_summary"] = summarize_record(same_distance)
    horse["going_record_summary"] = summarize_record(same_going)
    horse["running_style_summary"] = running_style_summary(past_runs)

    recent_finishes = [f"{run['finish_position']}着" for run in past_runs[:3] if run.get("finish_position")]
    horse["recent_form_summary"] = " / ".join(recent_finishes) if recent_finishes else "材料不足"

    change_bits = [horse["distance_change"], horse["surface_change"]]
    horse["condition_change_summary"] = " / ".join(bit for bit in change_bits if bit and bit != "不明")


def parse_race_overview(html: str, race_id: str, target_date: str, odds_reference_minutes: int) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    race_name_node = soup.select_one(".RaceName")
    title_node = race_name_node or soup.select_one("title")
    race_name = normalize_space(title_node.get_text(" ", strip=True)) if title_node else race_id
    if race_name_node is None and title_node:
        race_name = race_name.split("|")[0].replace("出馬表", "").strip()

    detail_text = " ".join(
        normalize_space(node.get_text(" ", strip=True))
        for node in soup.select(".RaceData01, .RaceData02, .RaceData03")
    )
    start_time_match = re.search(r"(\d{1,2}:\d{2})", detail_text)
    distance_match = re.search(r"(芝|ダ|障)\s*([0-9]{3,4})m", detail_text)
    going_match = re.search(r"馬場[:：]\s*([^\s/]+)", detail_text)

    surface = None
    distance = None
    if distance_match:
        surface = "ダート" if distance_match.group(1) == "ダ" else ("障害" if distance_match.group(1) == "障" else "芝")
        distance = int(distance_match.group(2))

    return {
        "date": target_date,
        "track": track_name_from_race_id(race_id),
        "race_number": int(race_id[-2:]),
        "race_name": race_name,
        "start_time": start_time_match.group(1) if start_time_match else None,
        "distance": distance,
        "surface": surface,
        "going": going_match.group(1) if going_match else None,
        "source_url": SHUTUBA_URL.format(race_id=race_id),
        "odds_captured_at": now_jst_iso(),
        "odds_reference_minutes_before_start": odds_reference_minutes,
    }


def parse_horse_history(session: requests.Session, horse_url: str) -> tuple[list[dict[str, Any]], str | None]:
    horse_id_match = re.search(r"/horse/(?:result/)?(\d+)", horse_url)
    if not horse_id_match:
        return [], None

    response = session.get(
        "https://db.netkeiba.com/horse/ajax_horse_results.html",
        headers=REQUEST_HEADERS,
        params={
            "input": "UTF-8",
            "output": "json",
            "id": horse_id_match.group(1),
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    soup = BeautifulSoup(payload.get("data", ""), "html.parser")
    table = find_history_table(soup)
    if table is None:
        return [], None

    runs: list[dict[str, Any]] = []
    previous_jockey: str | None = None
    for row in rows_from_table(table):
        if len(runs) >= 5:
            break

        if previous_jockey is None:
            previous_jockey = row.get("騎手")

        finish_position = parse_finish_position(row.get("着順"))
        distance, surface = parse_distance_surface(row.get("距離", ""))
        runs.append(
            {
                "date": normalize_space(row.get("日付")).replace("/", "-"),
                "track": parse_track_from_holding(row.get("開催", "")),
                "distance": distance,
                "surface": surface,
                "going": row.get("馬場"),
                "finish_position": finish_position,
                "margin": parse_float(row.get("着差")),
                "passing_order": row.get("通過"),
                "last3f": parse_float(row.get("上り")),
                "weight_carried": parse_float(row.get("斤量")),
                "class_name": row.get("レース名"),
            }
        )
    return runs, previous_jockey


def parse_horses(
    session: requests.Session,
    html: str,
    current_race: dict[str, Any],
    odds_map: dict[int, dict[str, Any]],
    existing_horses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_entry_table(soup)
    if table is None:
        return []

    existing_lookup = {horse["horse_number"]: horse for horse in (existing_horses or []) if horse.get("horse_number") is not None}
    horses: list[dict[str, Any]] = []
    for row in rows_from_table(table):
        horse_number = parse_int(row.get("馬番"))
        if horse_number is None:
            continue

        row_node = row["_row"]
        horse_link = row_node.find("a", href=re.compile(r"(/horse/|db\.netkeiba\.com/horse/)"))
        horse_url = urljoin("https://db.netkeiba.com", horse_link["href"]) if horse_link else None

        odds = odds_map.get(horse_number, {})
        previous = existing_lookup.get(horse_number, {})
        horse = {
            "horse_number": horse_number,
            "frame_number": parse_int(row.get("枠")),
            "horse_name": row.get("馬名"),
            "jockey": row.get("騎手"),
            "weight_carried": parse_float(row.get("斤量")),
            "popularity": odds.get("popularity", previous.get("popularity")),
            "win_odds": odds.get("win_odds", previous.get("win_odds")),
        }

        past_runs: list[dict[str, Any]] = []
        last_run_jockey = None
        if horse_url:
            try:
                past_runs, last_run_jockey = parse_horse_history(session, horse_url)
            except Exception:  # noqa: BLE001
                past_runs, last_run_jockey = [], None
        build_horse_summaries(horse, current_race, past_runs, last_run_jockey)
        horses.append(horse)

    horses.sort(key=lambda item: item["horse_number"])
    return horses


def parse_result(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    result_table = None
    for table in soup.find_all("table"):
        headers = set(extract_headers(table))
        if {"着順", "馬番"}.issubset(headers):
            result_table = table
            break
    if result_table is None:
        return None

    horses = []
    finish_order = []
    for row in rows_from_table(result_table):
        horse_number = parse_int(row.get("馬番"))
        finish_position = parse_finish_position(row.get("着順"))
        if horse_number is None or finish_position is None:
            continue
        horses.append({"horse_number": horse_number, "finish_position": finish_position})
        finish_order.append(horse_number)

    payouts: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        text = normalize_space(table.get_text(" ", strip=True))
        if "単勝" not in text:
            continue
        for row in table.find_all("tr"):
            cells = [normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if len(cells) < 3:
                continue
            if cells[0].replace(" ", "") != "単勝":
                continue
            numbers = [int(number) for number in re.findall(r"\d+", cells[1])]
            amounts = [int(amount) for amount in re.findall(r"\d+", cells[2].replace(",", ""))]
            payouts.extend(
                {
                    "horse_number": horse_number,
                    "payout_per_100": payout,
                }
                for horse_number, payout in zip(numbers, amounts)
            )
            break
        if payouts:
            break

    return {
        "fetched_at": now_jst_iso(),
        "finish_order": finish_order,
        "horses": horses,
        "payouts": {
            "win": payouts,
        },
    }


def collect_races(
    config: dict[str, Any],
    job_name: str,
    target_date: str,
    mode: str,
    root: Path | None = None,
) -> list[Path]:
    logger = setup_logger(job_name, config, root)
    target_races = set(config["target_races"])
    processed: list[Path] = []
    session = requests.Session()

    try:
        race_ids = discover_race_ids(session, target_date)
    except Exception as exc:  # noqa: BLE001
        log_job(logger, job_name, None, f"failed to fetch race list: {exc}")
        return []

    for race_id in race_ids:
        track_name = track_name_from_race_id(race_id)
        if track_name not in target_races:
            continue

        try:
            path = race_json_path(config, target_date, track_name, 11, root)
            existing = load_race_json(path)
            entry_html = fetch_html(session, SHUTUBA_URL.format(race_id=race_id))
            race = parse_race_overview(
                entry_html,
                race_id,
                target_date,
                int(config["odds_reference_minutes_before_start"]),
            )
            odds_map, odds_captured_at = fetch_win_odds(session, race_id)
            if odds_captured_at:
                race["odds_captured_at"] = odds_captured_at
            horses = parse_horses(session, entry_html, race, odds_map, (existing or {}).get("horses"))

            payload = ensure_race_payload(existing, race_id)
            payload["race"] = race
            payload["horses"] = horses

            if mode == "post":
                try:
                    result_html = fetch_html(session, RESULT_URL.format(race_id=race_id))
                    payload["result"] = parse_result(result_html)
                except Exception as exc:  # noqa: BLE001
                    log_job(logger, job_name, race_id, f"result scraping skipped: {exc}")

            save_race_json(path, payload)
            processed.append(path)
            log_job(logger, job_name, race_id, f"collected {track_name} 11R -> {path}")
        except Exception as exc:  # noqa: BLE001
            log_job(logger, job_name, race_id, f"scraping skipped: {exc}")

    return processed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--mode", choices=("pre", "post"), default="pre")
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    collect_races(config, args.mode, target_date, args.mode)


if __name__ == "__main__":
    main()
