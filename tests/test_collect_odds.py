from __future__ import annotations

import base64
import json
import logging
import sys
import zlib
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collect import (  # noqa: E402
    NETKEIBA_ODDS_URL,
    discover_jra_race_url,
    fetch_validated_win_odds,
    fetch_win_odds,
    parse_horses,
)


RACE_ID = "202602011211"
JRA_URL = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0102202601121120260719/B4"
EXPECTED_HORSES = {1: "Horse A", 2: "Horse B"}
NETKEIBA_ODDS = {
    1: {"win_odds": 2.5, "popularity": 1},
    2: {"win_odds": 4.0, "popularity": 2},
}
JRA_ODDS = {
    1: {"win_odds": 2.8, "popularity": 1},
    2: {"win_odds": 4.5, "popularity": 2},
}
CAPTURED_AT = "2026-07-18T18:00:00+09:00"


def race() -> dict:
    return {
        "date": "2026-07-19",
        "track": "函館",
        "race_number": 11,
        "race_name": "検証レース",
        "start_time": "15:20",
        "distance": 1200,
        "surface": "芝",
    }


def jra_html(
    *,
    race_date: str = "2026年7月19日",
    track: str = "函館",
    race_number: int = 11,
    horse_names: dict[int, str] | None = None,
    odds: dict[int, tuple[str, str]] | None = None,
) -> str:
    horse_names = horse_names or EXPECTED_HORSES
    odds = odds or {1: ("2.8", "1"), 2: ("4.5", "2")}
    rows = []
    for horse_number, horse_name in horse_names.items():
        win_odds, popularity = odds.get(horse_number, ("---.-", "**"))
        rows.append(
            f"""
            <tr>
              <td class="num">{horse_number}</td>
              <td class="horse">
                <div class="name_line">
                  <div class="name">{horse_name}</div>
                  <div class="odds"><div class="odds_line">
                    <span class="num"><strong>{win_odds}</strong></span>
                    <span class="pop_rank">({popularity}<span>番人気</span>)</span>
                  </div></div>
                </div>
              </td>
            </tr>
            """
        )
    return f"""
    <div id="syutsuba">
      <table>
        <caption><div class="race_header">
          <div class="date_line">
            <div class="date">{race_date}（日曜） 1回{track}12日</div>
            <div class="time"><strong>15時20分</strong></div>
          </div>
          <div class="race_title">
            <div class="race_number"><img alt="{race_number}レース"></div>
            <span class="race_name">検証レース<span class="grade_icon">GIII</span></span>
            <div class="course">コース：1,200 メートル（芝・右）</div>
          </div>
        </div></caption>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """


def logger() -> logging.Logger:
    value = logging.getLogger(f"test.collect.odds.{id(object())}")
    value.handlers.clear()
    value.addHandler(logging.NullHandler())
    return value


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse(self.payload)


class OddsFallbackTests(TestCase):
    def fetch(self):
        return fetch_validated_win_odds(None, RACE_ID, race(), EXPECTED_HORSES, logger(), "test-odds")

    def test_netkeiba_success_does_not_call_jra(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=(NETKEIBA_ODDS, None)))
            discover = stack.enter_context(patch("collect.discover_jra_race_url"))
            stack.enter_context(patch("collect.now_jst_iso", return_value=CAPTURED_AT))
            odds, captured_at, source, source_url = self.fetch()

        self.assertEqual(odds, NETKEIBA_ODDS)
        self.assertEqual(captured_at, CAPTURED_AT)
        self.assertEqual(source, "netkeiba")
        self.assertEqual(source_url, f"{NETKEIBA_ODDS_URL}?race_id={RACE_ID}")
        discover.assert_not_called()

    def test_netkeiba_empty_uses_complete_jra_snapshot(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=({}, None)))
            stack.enter_context(patch("collect.discover_jra_race_url", return_value=JRA_URL))
            stack.enter_context(patch("collect.fetch_html", return_value=jra_html()))
            stack.enter_context(patch("collect.now_jst_iso", return_value=CAPTURED_AT))
            odds, captured_at, source, source_url = self.fetch()

        self.assertEqual(odds, JRA_ODDS)
        self.assertEqual((captured_at, source, source_url), (CAPTURED_AT, "jra", JRA_URL))

    def test_partial_netkeiba_snapshot_is_fully_replaced_by_jra(self) -> None:
        partial = {1: NETKEIBA_ODDS[1]}
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=(partial, None)))
            stack.enter_context(patch("collect.discover_jra_race_url", return_value=JRA_URL))
            stack.enter_context(patch("collect.fetch_html", return_value=jra_html()))
            stack.enter_context(patch("collect.now_jst_iso", return_value=CAPTURED_AT))
            odds, _, source, _ = self.fetch()

        self.assertEqual(odds, JRA_ODDS)
        self.assertNotEqual(odds[1], NETKEIBA_ODDS[1])
        self.assertEqual(source, "jra")

    def test_horse_name_mismatch_rejects_jra_snapshot(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=({}, None)))
            stack.enter_context(patch("collect.discover_jra_race_url", return_value=JRA_URL))
            stack.enter_context(
                patch("collect.fetch_html", return_value=jra_html(horse_names={1: "Horse X", 2: "Horse B"}))
            )
            result = self.fetch()

        self.assertEqual(result, ({}, None, None, None))

    def test_different_jra_race_identity_is_rejected(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=({}, None)))
            stack.enter_context(patch("collect.discover_jra_race_url", return_value=JRA_URL))
            stack.enter_context(patch("collect.fetch_html", return_value=jra_html(race_date="2026年7月18日")))
            result = self.fetch()

        self.assertEqual(result, ({}, None, None, None))

    def test_partial_jra_snapshot_is_rejected(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=({}, None)))
            stack.enter_context(patch("collect.discover_jra_race_url", return_value=JRA_URL))
            stack.enter_context(
                patch("collect.fetch_html", return_value=jra_html(odds={1: ("2.8", "1")}))
            )
            result = self.fetch()

        self.assertEqual(result, ({}, None, None, None))

    def test_before_sale_placeholders_are_not_filled(self) -> None:
        netkeiba_placeholders = {
            1: {"win_odds": None, "popularity": None},
            2: {"win_odds": None, "popularity": None},
        }
        placeholders = {1: ("---.-", "**"), 2: ("---.-", "**")}
        with ExitStack() as stack:
            stack.enter_context(patch("collect.fetch_win_odds", return_value=(netkeiba_placeholders, None)))
            stack.enter_context(patch("collect.discover_jra_race_url", return_value=JRA_URL))
            stack.enter_context(patch("collect.fetch_html", return_value=jra_html(odds=placeholders)))
            result = self.fetch()

        self.assertEqual(result, ({}, None, None, None))

    def test_recollection_does_not_keep_or_mix_previous_odds(self) -> None:
        entry_html = """
        <table><thead><tr>
          <th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th><th>人気</th>
        </tr></thead><tbody>
          <tr><td>1</td><td>1</td><td>Horse A</td><td>55</td><td>Jockey A</td><td></td></tr>
          <tr><td>2</td><td>2</td><td>Horse B</td><td>55</td><td>Jockey B</td><td></td></tr>
        </tbody></table>
        """
        previous = [
            {"horse_number": 1, "win_odds": 9.9, "popularity": 2},
            {"horse_number": 2, "win_odds": 1.5, "popularity": 1},
        ]

        failed = parse_horses(None, entry_html, race(), RACE_ID, {}, previous)
        replaced = parse_horses(None, entry_html, race(), RACE_ID, JRA_ODDS, previous)

        self.assertTrue(all(horse["win_odds"] is None and horse["popularity"] is None for horse in failed))
        self.assertEqual(
            [(horse["win_odds"], horse["popularity"]) for horse in replaced],
            [(2.8, 1), (4.5, 2)],
        )

    def test_netkeiba_middle_status_with_complete_data_is_parsed(self) -> None:
        body = {
            "official_datetime": "2026-07-18 17:15:15",
            "odds": {
                "1": {
                    "a": ["2.5", 0, 1, "01"],
                    "b": ["4.0", 0, 2, "02"],
                }
            },
        }
        compressed = base64.b64encode(zlib.compress(json.dumps(body).encode("utf-8"))).decode("ascii")
        session = FakeSession({"status": "middle", "data": compressed})

        odds, official_datetime = fetch_win_odds(session, RACE_ID)

        self.assertEqual(odds, NETKEIBA_ODDS)
        self.assertEqual(official_datetime, "2026-07-18 17:15:15")

    def test_jra_url_is_discovered_from_official_links(self) -> None:
        anchor_url = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0103202602081120260719/D1"
        home_html = f'<a href="{anchor_url}">福島11R</a>'
        anchor_html = f'<a href="{JRA_URL}">1回函館12日</a>'
        with patch("collect.fetch_html", side_effect=[home_html, anchor_html]):
            actual = discover_jra_race_url(None, RACE_ID, race())

        self.assertEqual(actual, JRA_URL)


if __name__ == "__main__":
    import unittest

    unittest.main()
