import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_screen_log as screen_log


class CodexScreenLogTest(unittest.TestCase):
    def test_large_log_rotates_with_bounded_backup_count(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "codex_screen.log"
            log_path.write_text("current", encoding="utf-8")
            log_path.with_name("codex_screen.log.1").write_text(
                "backup-1", encoding="utf-8"
            )
            log_path.with_name("codex_screen.log.2").write_text(
                "backup-2", encoding="utf-8"
            )
            log_path.with_name("codex_screen.log.3").write_text(
                "old", encoding="utf-8"
            )

            with patch.object(screen_log, "LOG_PATH", log_path), patch.object(
                screen_log, "MAX_LOG_BYTES", 1
            ), patch.object(screen_log, "MAX_LOG_BACKUPS", 2):
                screen_log._rotate_if_large()

            self.assertEqual(
                "current",
                log_path.with_name("codex_screen.log.1").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                "backup-1",
                log_path.with_name("codex_screen.log.2").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertFalse(log_path.exists())
            self.assertFalse(log_path.with_name("codex_screen.log.3").exists())


if __name__ == "__main__":
    unittest.main()
