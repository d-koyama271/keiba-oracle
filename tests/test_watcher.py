from __future__ import annotations

import sys
import tempfile
import unittest
from logging import NullHandler, getLogger
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import watcher  # noqa: E402
from watcher import archive_processed  # noqa: E402


class ArchiveProcessedTests(unittest.TestCase):
    def test_archives_response_under_race_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response_path = root / "inbox" / "prediction" / "niigata_7r.json"
            response_path.parent.mkdir(parents=True)
            response_path.write_text("{}", encoding="utf-8")
            race_path = root / "data" / "races" / "2026-08-02" / "niigata_7r.json"

            archived = archive_processed(response_path, race_path)

            self.assertEqual(
                archived,
                root
                / "inbox"
                / "prediction"
                / "processed"
                / "2026-08-02"
                / "niigata_7r.json",
            )
            self.assertTrue(archived.exists())
            self.assertFalse(response_path.exists())

    def test_process_once_archives_using_imported_race_path(self) -> None:
        response_path = Path("inbox/prediction/niigata_7r.json")
        race_path = Path("data/races/2026-08-02/niigata_7r.json")
        logger = getLogger("test.watcher.archive")
        logger.handlers.clear()
        logger.addHandler(NullHandler())

        with patch.object(watcher, "setup_logger", return_value=logger), patch.object(
            watcher,
            "inbox_files",
            return_value=[response_path],
        ), patch.object(
            watcher,
            "import_prediction_response",
            return_value=race_path,
        ), patch.object(watcher, "finalize_pre"), patch.object(
            watcher,
            "archive_processed",
        ) as archive:
            processed = watcher.process_once({}, "test-watcher")

        self.assertEqual(processed, 1)
        archive.assert_called_once_with(response_path, race_path)


if __name__ == "__main__":
    unittest.main()
