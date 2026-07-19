from __future__ import annotations

import argparse
import base64
import json
import math
import re
import zlib
from datetime import date as date_cls
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

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
NETKEIBA_ODDS_URL = "https://race.netkeiba.com/api/api_get_jra_odds.html"
JRA_HOME_URL = "https://www.jra.go.jp/"
JRA_RACE_URL_PATTERN = re.compile(
    r"CNAME=pw01dde01(?P<track>\d{2})(?P<year>\d{4})(?P<meeting>\d{2})"
    r"(?P<meeting_day>\d{2})(?P<race_number>\d{2})(?P<date>\d{8})/[0-9A-Fa-f]{2}"
)
HORSE_HISTORY_HEADERS = {"日付", "開催", "距離", "着順"}
DISTANCE_BAND_METERS = 200
JRA_TRACK_CODES = {f"{code:02d}" for code in range(1, 11)}
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
        NETKEIBA_ODDS_URL,
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
    if not payload.get("data"):
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
        if horse_number in odds_map:
            raise ValueError(f"duplicate horse number in netkeiba odds: {horse_number}")
        odds_map[horse_number] = {
            "win_odds": parse_float(row[0]),
            "popularity": parse_int(row[2]),
        }
    return odds_map, odds_payload.get("official_datetime")


def normalize_horse_name(value: str | None) -> str:
    return normalize_space(value).replace(" ", "")


def parse_entry_horse_identities(html: str) -> dict[int, str]:
    table = find_entry_table(BeautifulSoup(html, "html.parser"))
    if table is None:
        return {}

    horses: dict[int, str] = {}
    for row in rows_from_table(table):
        horse_number = parse_int(row.get("馬番"))
        horse_name = normalize_space(row.get("馬名"))
        if horse_number is None or not horse_name:
            continue
        if horse_number in horses:
            raise ValueError(f"duplicate horse number in entry table: {horse_number}")
        horses[horse_number] = horse_name
    return horses


def validate_odds_snapshot(
    odds_map: dict[int, dict[str, Any]],
    expected_horses: dict[int, str],
    source_horses: dict[int, str],
) -> str | None:
    expected_numbers = set(expected_horses)
    source_numbers = set(source_horses)
    odds_numbers = set(odds_map)
    if not expected_numbers:
        return "target entry horses are unavailable"
    if source_numbers != expected_numbers:
        return f"horse numbers mismatch: expected={sorted(expected_numbers)} source={sorted(source_numbers)}"
    if odds_numbers != expected_numbers:
        return f"odds horse numbers mismatch: expected={sorted(expected_numbers)} actual={sorted(odds_numbers)}"

    popularities: list[int] = []
    horse_count = len(expected_horses)
    for horse_number in sorted(expected_numbers):
        expected_name = expected_horses[horse_number]
        source_name = source_horses[horse_number]
        if normalize_horse_name(expected_name) != normalize_horse_name(source_name):
            return (
                f"horse name mismatch: horse_number={horse_number} "
                f"expected={expected_name} source={source_name}"
            )

        row = odds_map[horse_number]
        odds = row.get("win_odds")
        popularity = row.get("popularity")
        if isinstance(odds, bool) or not isinstance(odds, (int, float)):
            return f"invalid win odds: horse_number={horse_number} value={odds}"
        if not math.isfinite(float(odds)) or float(odds) < 1.0:
            return f"invalid win odds: horse_number={horse_number} value={odds}"
        if isinstance(popularity, bool) or not isinstance(popularity, int):
            return f"invalid popularity: horse_number={horse_number} value={popularity}"
        if popularity < 1 or popularity > horse_count:
            return f"invalid popularity: horse_number={horse_number} value={popularity}"
        popularities.append(popularity)

    if len(set(popularities)) != horse_count:
        return f"duplicate popularity values: {popularities}"
    return None


def jra_race_url_identity(url: str) -> tuple[str, str, str, str, str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.jra.go.jp":
        return None
    match = JRA_RACE_URL_PATTERN.search(unquote(url))
    if not match:
        return None
    return (
        match.group("year"),
        match.group("track"),
        match.group("meeting"),
        match.group("meeting_day"),
        match.group("race_number"),
        match.group("date"),
    )


def expected_jra_race_identity(race_id: str, race_date: str) -> tuple[str, str, str, str, str, str]:
    return (
        race_id[0:4],
        race_id[4:6],
        race_id[6:8],
        race_id[8:10],
        race_id[10:12],
        race_date.replace("-", ""),
    )


def jra_race_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"])
        if jra_race_url_identity(url) is not None:
            links.append(url)
    return list(dict.fromkeys(links))


def discover_jra_race_url(session: requests.Session, race_id: str, race: dict[str, Any]) -> str:
    expected = expected_jra_race_identity(race_id, race["date"])
    target_date_key = expected[-1]
    home_links = jra_race_links(fetch_html(session, JRA_HOME_URL), JRA_HOME_URL)
    for url in home_links:
        if jra_race_url_identity(url) == expected:
            return url

    target_date_links = [url for url in home_links if jra_race_url_identity(url)[-1] == target_date_key]
    if not target_date_links:
        raise ValueError(f"JRA official race link is unavailable for {race['date']}")

    anchor_url = target_date_links[0]
    page_links = jra_race_links(fetch_html(session, anchor_url), anchor_url)
    for url in page_links:
        if jra_race_url_identity(url) == expected:
            return url
    raise ValueError(f"JRA official race page was not identified for race_id={race_id}")


def parse_jra_win_odds(
    html: str,
) -> tuple[dict[str, Any], dict[int, str], dict[int, dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#syutsuba table")
    date_node = soup.select_one("#syutsuba .race_header .date_line .date")
    race_number_node = soup.select_one("#syutsuba .race_number img[alt]")
    if table is None or date_node is None or race_number_node is None:
        raise ValueError("JRA official entry table or race header is unavailable")

    date_text = normalize_space(date_node.get_text(" ", strip=True))
    date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_text)
    holding_match = re.search(r"(\d+)回(.+?)(\d+)日", date_text)
    race_number = parse_int(race_number_node.get("alt"))
    if not date_match or not holding_match or race_number is None:
        raise ValueError("JRA official race identity is unavailable")

    start_node = soup.select_one("#syutsuba .date_line .time strong")
    start_match = re.search(r"(\d{1,2})時(\d{2})分", normalize_space(start_node.get_text()) if start_node else "")
    course_node = soup.select_one("#syutsuba .race_title .course")
    course_text = normalize_space(course_node.get_text(" ", strip=True)) if course_node else ""
    distance_match = re.search(r"([\d,]+)\s*メートル", course_text)
    surface = "ダート" if "ダート" in course_text else ("障害" if "障害" in course_text else ("芝" if "芝" in course_text else None))
    race_name_node = soup.select_one("#syutsuba .race_name")
    race_name = None
    if race_name_node is not None:
        race_name = normalize_space(
            " ".join(str(child) for child in race_name_node.contents if getattr(child, "name", None) is None)
        ) or None

    identity = {
        "date": (
            f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        ),
        "track": holding_match.group(2),
        "meeting_number": int(holding_match.group(1)),
        "meeting_day": int(holding_match.group(3)),
        "race_number": race_number,
        "race_name": race_name,
        "start_time": f"{int(start_match.group(1)):02d}:{int(start_match.group(2)):02d}" if start_match else None,
        "distance": int(distance_match.group(1).replace(",", "")) if distance_match else None,
        "surface": surface,
    }

    horses: dict[int, str] = {}
    odds_map: dict[int, dict[str, Any]] = {}
    for row in table.select("tbody > tr"):
        direct_cells = row.find_all("td", recursive=False)
        number_cell = next((cell for cell in direct_cells if "num" in (cell.get("class") or [])), None)
        horse_cell = next((cell for cell in direct_cells if "horse" in (cell.get("class") or [])), None)
        horse_number = parse_int(number_cell.get_text(" ", strip=True)) if number_cell else None
        name_node = horse_cell.select_one(".name_line .name") if horse_cell else None
        horse_name = normalize_space(name_node.get_text(" ", strip=True)) if name_node else ""
        if horse_number is None or not horse_name:
            continue
        if horse_number in horses:
            raise ValueError(f"duplicate horse number in JRA official entry table: {horse_number}")

        odds_node = horse_cell.select_one(".odds_line span.num strong")
        popularity_node = horse_cell.select_one(".odds_line .pop_rank")
        popularity_match = re.search(
            r"(\d+)\s*番人気",
            normalize_space(popularity_node.get_text(" ", strip=True)) if popularity_node else "",
        )
        horses[horse_number] = horse_name
        odds_map[horse_number] = {
            "win_odds": parse_float(odds_node.get_text(" ", strip=True)) if odds_node else None,
            "popularity": int(popularity_match.group(1)) if popularity_match else None,
        }
    return identity, horses, odds_map


def validate_jra_race_identity(race_id: str, race: dict[str, Any], identity: dict[str, Any]) -> str | None:
    required = {
        "date": race.get("date"),
        "track": race.get("track"),
        "race_number": int(race.get("race_number") or race_id[-2:]),
        "meeting_number": int(race_id[6:8]),
        "meeting_day": int(race_id[8:10]),
    }
    for field, expected in required.items():
        if identity.get(field) != expected:
            return f"{field} mismatch: expected={expected} actual={identity.get(field)}"

    for field in ("start_time", "distance", "surface"):
        expected = race.get(field)
        if expected is not None and identity.get(field) != expected:
            return f"{field} mismatch: expected={expected} actual={identity.get(field)}"
    return None


def fetch_validated_win_odds(
    session: requests.Session,
    race_id: str,
    race: dict[str, Any],
    expected_horses: dict[int, str],
    logger: Any,
    job_name: str,
) -> tuple[dict[int, dict[str, Any]], str | None, str | None, str | None]:
    log_job(logger, job_name, race_id, "netkeiba win odds fetch started")
    try:
        netkeiba_odds, _ = fetch_win_odds(session, race_id)
        log_job(logger, job_name, race_id, f"netkeiba win odds fetched: {len(netkeiba_odds)} horses")
        reason = validate_odds_snapshot(netkeiba_odds, expected_horses, expected_horses)
    except Exception as exc:  # noqa: BLE001
        netkeiba_odds = {}
        log_job(logger, job_name, race_id, "netkeiba win odds fetched: 0 horses")
        reason = str(exc)

    if reason is None:
        captured_at = now_jst_iso()
        source_url = f"{NETKEIBA_ODDS_URL}?race_id={race_id}"
        log_job(logger, job_name, race_id, f"odds source adopted: netkeiba captured_at={captured_at}")
        return netkeiba_odds, captured_at, "netkeiba", source_url

    log_job(logger, job_name, race_id, f"netkeiba odds rejected: {reason}")
    log_job(logger, job_name, race_id, "JRA official win odds fallback started")
    try:
        jra_url = discover_jra_race_url(session, race_id, race)
        log_job(logger, job_name, race_id, f"JRA official race page identified: {jra_url}")
        identity, jra_horses, jra_odds = parse_jra_win_odds(fetch_html(session, jra_url))
        log_job(logger, job_name, race_id, f"JRA official win odds fetched: {len(jra_odds)} horses")
        identity_reason = validate_jra_race_identity(race_id, race, identity)
        if identity_reason is not None:
            raise ValueError(f"JRA race identity validation failed: {identity_reason}")
        log_job(logger, job_name, race_id, "JRA race identity validation succeeded")
        reason = validate_odds_snapshot(jra_odds, expected_horses, jra_horses)
        if reason is not None:
            raise ValueError(reason)
    except Exception as exc:  # noqa: BLE001
        log_job(
            logger,
            job_name,
            race_id,
            f"odds unavailable after JRA fallback: {exc}; odds_captured_at=null",
        )
        return {}, None, None, None

    captured_at = now_jst_iso()
    log_job(logger, job_name, race_id, f"odds source adopted: jra captured_at={captured_at}")
    return jra_odds, captured_at, "jra", jra_url


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


def history_race_id(row_node: Any) -> str | None:
    for link in row_node.find_all("a", href=True):
        match = re.search(r"/race/([0-9A-Za-z]+)/?(?:$|[?#])", link["href"])
        if match:
            return match.group(1)
    return None


def normalize_weather(value: Any) -> str | None:
    text = normalize_space(value)
    if not text:
        return None
    if "雪" in text:
        return "snow"
    if "雨" in text:
        return "rain"
    if "曇" in text:
        return "cloudy"
    if "晴" in text:
        return "sunny"
    return None


def normalize_going(value: Any) -> str | None:
    text = normalize_space(value)
    if not text:
        return None
    if "不良" in text:
        return "不良"
    if "稍重" in text or text == "稍":
        return "稍重"
    if "重" in text:
        return "重"
    if "良" in text:
        return "良"
    return text


def normalize_class_grade(value: Any, soup: BeautifulSoup | None = None) -> str:
    if soup:
        icon_grades = {
            "Icon_GradeType1": "G1",
            "Icon_GradeType2": "G2",
            "Icon_GradeType3": "G3",
            "Icon_GradeType4": "Listed",
            "Icon_GradeType5": "Open",
        }
        for class_name, grade in icon_grades.items():
            if soup.select_one(f".RaceName .{class_name}"):
                return grade

    text = normalize_space(value).upper()
    text = (
        text.replace("Ｇ", "G")
        .replace("１", "1")
        .replace("２", "2")
        .replace("３", "3")
        .replace("Ⅰ", "I")
        .replace("Ⅱ", "II")
        .replace("Ⅲ", "III")
    )
    if re.search(r"(?:G|JPN)\s*(?:1|I)(?!I)", text):
        return "G1"
    if re.search(r"(?:G|JPN)\s*(?:2|II)(?!I)", text):
        return "G2"
    if re.search(r"(?:G|JPN)\s*(?:3|III)", text):
        return "G3"
    if "リステッド" in text or "LISTED" in text or re.search(r"\(L\)", text):
        return "Listed"
    if "オープン" in text or re.search(r"\(OP\)", text):
        return "Open"
    if "3勝クラス" in text or "1600万下" in text:
        return "3-win"
    if "2勝クラス" in text or "1000万下" in text:
        return "2-win"
    if "1勝クラス" in text or "500万下" in text:
        return "1-win"
    if "未勝利" in text:
        return "Maiden"
    if "新馬" in text:
        return "Newcomer"
    return "Other"


def parse_race_time(value: Any) -> tuple[str | None, float | None]:
    text = normalize_space(value)
    match = re.fullmatch(r"(\d+):([0-5]\d(?:\.\d{1,2})?)", text)
    if not match:
        return None, None
    seconds = int(match.group(1)) * 60 + float(match.group(2))
    return text, round(seconds, 2)


def parse_body_weight(value: Any) -> tuple[int | None, int | None]:
    text = normalize_space(value)
    match = re.fullmatch(r"(\d+)(?:\(([+-]?\d+)\))?", text)
    if not match:
        return None, None
    body_weight = int(match.group(1))
    change = int(match.group(2)) if match.group(2) is not None else None
    return body_weight, change


def parse_history_run(row: dict[str, Any]) -> dict[str, Any]:
    distance, surface = parse_distance_surface(row.get("距離", ""))
    race_time, race_time_seconds = parse_race_time(row.get("タイム"))
    body_weight, body_weight_change = parse_body_weight(row.get("馬体重"))
    class_name = normalize_space(row.get("レース名")) or None
    return {
        "race_id": history_race_id(row["_row"]),
        "date": normalize_space(row.get("日付")).replace("/", "-"),
        "track": parse_track_from_holding(row.get("開催", "")),
        "race_number": parse_int(row.get("R")),
        "weather": normalize_space(row.get("天気")) or None,
        "distance": distance,
        "surface": surface,
        "going": normalize_going(row.get("馬場")),
        "field_size": parse_int(row.get("頭数")),
        "frame_number": parse_int(row.get("枠番")),
        "horse_number": parse_int(row.get("馬番")),
        "win_odds": parse_float(row.get("オッズ")),
        "popularity": parse_int(row.get("人気")),
        "finish_position": parse_finish_position(row.get("着順")),
        "jockey": normalize_space(row.get("騎手")) or None,
        "weight_carried": parse_float(row.get("斤量")),
        "race_time": race_time,
        "race_time_seconds": race_time_seconds,
        "margin": parse_float(row.get("着差")),
        "passing_order": normalize_space(row.get("通過")) or None,
        "pace": normalize_space(row.get("ペース")) or None,
        "last3f": parse_float(row.get("上り")),
        "body_weight": body_weight,
        "body_weight_change": body_weight_change,
        "class_name": class_name,
        "class_grade": normalize_class_grade(class_name),
    }


def is_jra_history(run: dict[str, Any]) -> bool:
    race_id = run.get("race_id")
    return bool(
        isinstance(race_id, str)
        and re.fullmatch(r"\d{12}", race_id)
        and race_id[4:6] in JRA_TRACK_CODES
    )


def record_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    finishes = [run["finish_position"] for run in runs if isinstance(run.get("finish_position"), int)]
    return {
        "runs": len(runs),
        "wins": sum(finish == 1 for finish in finishes),
        "top3": sum(finish <= 3 for finish in finishes),
        "average_finish_position": round(sum(finishes) / len(finishes), 4) if finishes else None,
    }


def build_career_summaries(
    all_runs: list[dict[str, Any]],
    current_race: dict[str, Any],
    current_jockey: str | None,
) -> dict[str, Any]:
    jra_runs = [run for run in all_runs if is_jra_history(run)]
    current_track = current_race.get("track")
    current_surface = current_race.get("surface")
    current_distance = parse_int(current_race.get("distance"))
    current_going = normalize_going(current_race.get("going"))
    current_weather = normalize_weather(current_race.get("weather"))
    current_class = current_race.get("class_grade")

    track_runs = [
        run
        for run in jra_runs
        if run.get("track") == current_track and run.get("surface") == current_surface
    ]
    surface_runs = [run for run in jra_runs if run.get("surface") == current_surface]
    distance_min = current_distance - DISTANCE_BAND_METERS if current_distance is not None else None
    distance_max = current_distance + DISTANCE_BAND_METERS if current_distance is not None else None
    distance_runs = [
        run
        for run in surface_runs
        if distance_min is not None
        and distance_max is not None
        and run.get("distance") is not None
        and distance_min <= run["distance"] <= distance_max
    ]
    going_runs = [
        run
        for run in surface_runs
        if current_going is not None and normalize_going(run.get("going")) == current_going
    ]
    weather_runs = [
        run
        for run in surface_runs
        if current_weather is not None and normalize_weather(run.get("weather")) == current_weather
    ]
    class_runs = [
        run
        for run in jra_runs
        if current_class not in (None, "Other") and run.get("class_grade") == current_class
    ]
    jockey_runs = [
        run
        for run in jra_runs
        if current_jockey and normalize_space(run.get("jockey")) == normalize_space(current_jockey)
    ]

    distance_record = {
        "surface": current_surface,
        "distance_min": distance_min,
        "distance_max": distance_max,
        **record_summary(distance_runs),
    }
    return {
        "overall_record": record_summary(jra_runs),
        "current_track_record": record_summary(track_runs),
        "current_surface_record": record_summary(surface_runs),
        "current_distance_band_record": distance_record,
        "current_going_record": record_summary(going_runs),
        "current_weather_record": record_summary(weather_runs) if weather_runs else None,
        "current_class_record": (
            record_summary(class_runs) if current_class not in (None, "Other") else None
        ),
        "current_jockey_combo_record": record_summary(jockey_runs),
    }


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
    career_summaries: dict[str, Any],
    last_run_jockey: str | None,
) -> None:
    horse["past_runs"] = past_runs
    horse["career_summaries"] = career_summaries
    last_run = past_runs[0] if past_runs else None
    current_distance = parse_int(current_race.get("distance"))
    current_surface = current_race.get("surface")

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
    weather_match = re.search(r"(?:天候|天気)[:：]\s*([^\s/]+)", detail_text)

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
        "going": normalize_going(going_match.group(1)) if going_match else None,
        "weather": weather_match.group(1) if weather_match else None,
        "class_grade": normalize_class_grade(f"{race_name} {detail_text}", soup),
        "source_url": SHUTUBA_URL.format(race_id=race_id),
        "odds_captured_at": None,
        "odds_source": None,
        "odds_source_url": None,
        "odds_reference_minutes_before_start": odds_reference_minutes,
    }


def parse_horse_history(
    session: requests.Session,
    horse_url: str,
    current_race_id: str,
    current_race: dict[str, Any],
    current_jockey: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    empty_summaries = build_career_summaries([], current_race, current_jockey)
    horse_id_match = re.search(r"/horse/(?:result/)?(\d+)", horse_url)
    if not horse_id_match:
        return [], empty_summaries, None

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
        return [], empty_summaries, None

    all_runs: list[dict[str, Any]] = []
    for row in rows_from_table(table):
        run = parse_history_run(row)
        if run["race_id"] == current_race_id:
            continue
        all_runs.append(run)

    all_runs.sort(key=lambda run: run.get("date") or "", reverse=True)
    past_runs = all_runs[:5]
    previous_jockey = past_runs[0].get("jockey") if past_runs else None
    return (
        past_runs,
        build_career_summaries(all_runs, current_race, current_jockey),
        previous_jockey,
    )


def parse_horses(
    session: requests.Session,
    html: str,
    current_race: dict[str, Any],
    current_race_id: str,
    odds_map: dict[int, dict[str, Any]],
    _existing_horses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_entry_table(soup)
    if table is None:
        return []

    horses: list[dict[str, Any]] = []
    for row in rows_from_table(table):
        horse_number = parse_int(row.get("馬番"))
        if horse_number is None:
            continue

        row_node = row["_row"]
        horse_link = row_node.find("a", href=re.compile(r"(/horse/|db\.netkeiba\.com/horse/)"))
        horse_url = urljoin("https://db.netkeiba.com", horse_link["href"]) if horse_link else None

        odds = odds_map.get(horse_number, {})
        horse = {
            "horse_number": horse_number,
            "frame_number": parse_int(row.get("枠")),
            "horse_name": row.get("馬名"),
            "jockey": row.get("騎手"),
            "weight_carried": parse_float(row.get("斤量")),
            "popularity": odds.get("popularity"),
            "win_odds": odds.get("win_odds"),
        }

        past_runs: list[dict[str, Any]] = []
        career_summaries = build_career_summaries([], current_race, horse.get("jockey"))
        last_run_jockey = None
        if horse_url:
            try:
                past_runs, career_summaries, last_run_jockey = parse_horse_history(
                    session,
                    horse_url,
                    current_race_id,
                    current_race,
                    horse.get("jockey"),
                )
            except Exception:  # noqa: BLE001
                past_runs, last_run_jockey = [], None
        build_horse_summaries(horse, current_race, past_runs, career_summaries, last_run_jockey)
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
        finish_text = normalize_space(row.get("着順"))
        finish_position = parse_finish_position(finish_text)
        if horse_number is None:
            continue
        if finish_position is None:
            status = next(
                (flag for flag in ("中止", "除外", "取消", "失格") if flag in finish_text),
                None,
            )
            if status is None:
                continue
            horses.append({"horse_number": horse_number, "finish_position": status})
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
            expected_horses = parse_entry_horse_identities(entry_html)
            odds_map, odds_captured_at, odds_source, odds_source_url = fetch_validated_win_odds(
                session,
                race_id,
                race,
                expected_horses,
                logger,
                job_name,
            )
            race["odds_captured_at"] = odds_captured_at
            race["odds_source"] = odds_source
            race["odds_source_url"] = odds_source_url
            horses = parse_horses(
                session,
                entry_html,
                race,
                race_id,
                odds_map,
                (existing or {}).get("horses"),
            )

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
