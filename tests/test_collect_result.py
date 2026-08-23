from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collect import MOBILE_RESULT_URL, RESULT_URL, fetch_html, parse_result  # noqa: E402


class ResultParsingTests(unittest.TestCase):
    def test_desktop_result_failure_uses_mobile_result_page(self) -> None:
        failed = Mock()
        failed.raise_for_status.side_effect = requests.HTTPError("blocked")
        mobile = Mock(text="<html>mobile result</html>", apparent_encoding="utf-8", encoding=None)
        mobile.raise_for_status.return_value = None
        session = Mock()
        session.get.side_effect = [failed, mobile]

        html = fetch_html(session, RESULT_URL.format(race_id="202604030207"))

        self.assertEqual(html, "<html>mobile result</html>")
        self.assertEqual(
            session.get.call_args_list[1].args[0],
            MOBILE_RESULT_URL.format(race_id="202604030207"),
        )

    def test_special_finish_status_is_preserved_without_entering_finish_order(self) -> None:
        html = """
        <html><body>
          <table>
            <thead><tr><th>着順</th><th>馬番</th><th>馬名</th></tr></thead>
            <tbody>
              <tr><td>1</td><td>1</td><td>Winner</td></tr>
              <tr><td>2</td><td>3</td><td>Runner-up</td></tr>
              <tr><td>中止</td><td>2</td><td>Stopped</td></tr>
            </tbody>
          </table>
          <table>
            <tbody><tr><th>単勝</th><td>1</td><td>3,310円</td></tr></tbody>
          </table>
        </body></html>
        """

        result = parse_result(html)

        self.assertIsNotNone(result)
        self.assertEqual(result["finish_order"], [1, 3])
        self.assertEqual(
            result["horses"],
            [
                {"horse_number": 1, "finish_position": 1},
                {"horse_number": 3, "finish_position": 2},
                {"horse_number": 2, "finish_position": "中止"},
            ],
        )
        self.assertEqual(
            result["payouts"]["win"],
            [{"horse_number": 1, "payout_per_100": 3310}],
        )


if __name__ == "__main__":
    unittest.main()
