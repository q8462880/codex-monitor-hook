import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from update_codex_config import (
    END_MARKER,
    HOOK_EVENTS,
    MINIMAL_HOOK_EVENTS,
    START_MARKER,
    build_hook_block,
    merge_hook_block,
)


class HookConfigTest(unittest.TestCase):
    def test_builds_all_supported_lifecycle_hooks(self):
        block = build_hook_block(
            r"C:\Users\test\.codex_screen\codex_hook_relay.exe",
        )

        for event_name in HOOK_EVENTS:
            self.assertIn(f"[[hooks.{event_name}]]", block)
            self.assertIn(f"[[hooks.{event_name}.hooks]]", block)

        self.assertEqual(len(HOOK_EVENTS), block.count('type = "command"'))
        self.assertNotIn("python.exe", block)
        self.assertNotIn("pythonw.exe", block)
        self.assertIn(
            r"""commandWindows = '& "C:\Users\test\.codex_screen\codex_hook_windows_launcher.ps1" -RelayExe "C:\Users\test\.codex_screen\codex_hook_relay.exe"'""",
            block,
        )
        self.assertEqual(1, block.count("# BEGIN codex-monitor-hook"))
        self.assertEqual(1, block.count("# END codex-monitor-hook"))

    def test_builds_minimal_hooks_for_performance_mode(self):
        block = build_hook_block(
            r"C:\Users\test\.codex_screen\codex_hook_relay.exe",
            hook_profile="minimal",
        )

        for event_name in MINIMAL_HOOK_EVENTS:
            self.assertIn(f"[[hooks.{event_name}]]", block)

        self.assertNotIn("[[hooks.PreToolUse]]", block)
        self.assertNotIn("[[hooks.PostToolUse]]", block)
        self.assertEqual(len(MINIMAL_HOOK_EVENTS), block.count('type = "command"'))

    def test_replacing_hooks_preserves_codex_state_and_other_tables(self):
        old_text = "\n".join(
            [
                START_MARKER,
                "[[hooks.SessionStart]]",
                END_MARKER,
                "",
                "[hooks.state]",
                "[hooks.state.'config.toml:session_start:0:0']",
                'trusted_hash = "sha256:test"',
                "",
                "[features]",
                "js_repl = false",
            ]
        )

        merged = merge_hook_block(old_text, build_hook_block("relay.py"))

        self.assertEqual(1, merged.count(START_MARKER))
        self.assertEqual(1, merged.count(END_MARKER))
        self.assertIn("[hooks.state]", merged)
        self.assertIn('trusted_hash = "sha256:test"', merged)
        self.assertIn("[features]", merged)


if __name__ == "__main__":
    unittest.main()
