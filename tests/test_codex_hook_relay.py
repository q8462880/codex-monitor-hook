import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_hook_relay as relay


class CodexHookRelayTest(unittest.TestCase):
    def test_relay_lock_is_released_after_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            lock_file = base_dir / "relay.lock"
            with patch.object(relay, "BASE_DIR", base_dir), patch.object(
                relay, "RELAY_LOCK_FILE", lock_file
            ):
                with relay._relay_instance_lock() as first_acquired:
                    self.assertTrue(first_acquired)

                with relay._relay_instance_lock() as second_acquired:
                    self.assertTrue(second_acquired)

    def test_daemon_command_prefers_packaged_exe_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            daemon_exe = base_dir / "codex_screen_daemon.exe"
            daemon_script = base_dir / "codex_screen_daemon.py"
            daemon_exe.write_text("", encoding="utf-8")
            daemon_script.write_text("", encoding="utf-8")

            with patch.object(relay.os, "name", "nt"), patch.object(
                relay, "DAEMON_EXE", daemon_exe
            ), patch.object(relay, "DAEMON_SCRIPT", daemon_script):
                self.assertEqual(
                    [str(daemon_exe), "--daemon"],
                    relay._daemon_command(),
                )

    def test_daemon_command_keeps_source_fallback_for_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            daemon_exe = base_dir / "codex_screen_daemon.exe"
            daemon_script = base_dir / "codex_screen_daemon.py"
            daemon_script.write_text("", encoding="utf-8")

            with patch.object(relay, "DAEMON_EXE", daemon_exe), patch.object(
                relay, "DAEMON_SCRIPT", daemon_script
            ):
                command = relay._daemon_command()

            self.assertEqual(str(daemon_script), command[1])
            self.assertEqual("--daemon", command[2])


if __name__ == "__main__":
    unittest.main()
