import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_hook_bootstrap as bootstrap


class CodexHookBootstrapTest(unittest.TestCase):
    def test_detects_json_before_relay_script(self):
        event_json = '{"hook_event_name":"UserPromptSubmit","session_id":"session-a"}'

        invocation = bootstrap._hook_invocation(
            [event_json, r"C:\Users\test\.codex_screen\codex_hook_relay.py"]
        )

        self.assertEqual(
            (event_json, r"C:\Users\test\.codex_screen\codex_hook_relay.py"),
            invocation,
        )

    def test_ignores_normal_python_invocations(self):
        invocation = bootstrap._hook_invocation(
            ["C:\\Users\\test\\script.py", "--help"]
        )

        self.assertIsNone(invocation)

    def test_ignores_json_without_codex_hook_name(self):
        invocation = bootstrap._hook_invocation(
            ['{"message":"normal data"}', "codex_hook_relay.py"]
        )

        self.assertIsNone(invocation)


if __name__ == "__main__":
    unittest.main()
