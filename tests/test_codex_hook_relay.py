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


if __name__ == "__main__":
    unittest.main()
