from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collect import parse_result  # noqa: E402


class ResultParsingTests(unittest.TestCase):
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
