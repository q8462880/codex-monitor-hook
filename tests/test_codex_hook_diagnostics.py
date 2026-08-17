import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from codex_hook_diagnostics import _write_trusted_hashes, summarize_hooks_response


class CodexHookDiagnosticsTest(unittest.TestCase):
    def test_reports_visible_but_untrusted_hooks_as_not_executable(self):
        response = {
            "result": {
                "data": [
                    {
                        "cwd": r"C:\work",
                        "hooks": [
                            {
                                "command": "pythonw.exe codex_hook_relay.py",
                                "enabled": True,
                                "trustStatus": "untrusted",
                            }
                        ],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        }

        summary = summarize_hooks_response(response)

        self.assertEqual(1, summary["loaded"])
        self.assertEqual(0, summary["executable"])
        self.assertEqual(1, summary["untrusted"])

    def test_reports_enabled_and_trusted_hooks_as_executable(self):
        response = {
            "result": {
                "data": [
                    {
                        "cwd": r"C:\work",
                        "hooks": [
                            {
                                "command": "pythonw.exe codex_hook_relay.py",
                                "enabled": True,
                                "trustStatus": "trusted",
                            },
                            {
                                "command": "another-hook.exe",
                                "enabled": True,
                                "trustStatus": "trusted",
                            },
                        ],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        }

        summary = summarize_hooks_response(response)

        self.assertEqual(1, summary["loaded"])
        self.assertEqual(1, summary["executable"])
        self.assertEqual(0, summary["untrusted"])

    def test_preserves_codex_hook_loading_errors(self):
        response = {
            "result": {
                "data": [
                    {
                        "cwd": r"C:\work",
                        "hooks": [],
                        "warnings": ["clamped"],
                        "errors": [{"path": "config.toml", "message": "bad hook"}],
                    }
                ]
            }
        }

        summary = summarize_hooks_response(response)

        self.assertEqual(["clamped"], summary["warnings"])
        self.assertEqual(["config.toml: bad hook"], summary["errors"])

    def test_creates_hooks_state_when_writing_trusted_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text("model = \"gpt-5\"\n", encoding="utf-8")

            _write_trusted_hashes(
                config_path,
                {
                    "config.toml:session_start:0:0": "sha256:start",
                    "config.toml:prompt:0:0": "sha256:prompt",
                },
            )

            text = config_path.read_text(encoding="utf-8")

        self.assertIn("[hooks.state]", text)
        self.assertIn("trusted_hash = \"sha256:start\"", text)
        self.assertIn("trusted_hash = \"sha256:prompt\"", text)


if __name__ == "__main__":
    unittest.main()
