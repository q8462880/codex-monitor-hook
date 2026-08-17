import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from update_codex_config import (
    END_MARKER,
    HOOK_EVENTS,
    MINIMAL_HOOK_EVENTS,
    QUOTA_HOOK_EVENTS,
    START_MARKER,
    build_hook_block,
    merge_hook_block,
    powershell_command,
)


class HookConfigTest(unittest.TestCase):
    def test_builds_all_supported_lifecycle_hooks(self):
        block = build_hook_block(
            r"C:\Python\pythonw.exe",
            r"C:\Users\test\.codex_screen\codex_hook_relay.py",
            hook_profile="full",
            platform_name="windows",
        )

        for event_name in HOOK_EVENTS:
            self.assertIn(f"[[hooks.{event_name}]]", block)
            self.assertIn(f"[[hooks.{event_name}.hooks]]", block)

        self.assertEqual(len(HOOK_EVENTS), block.count('type = "command"'))
        self.assertIn(
            r"""command = '"C:\Python\pythonw.exe" "C:\Users\test\.codex_screen\codex_hook_relay.py"'""",
            block,
        )
        parsed = tomllib.loads(block)
        self.assertEqual(
            r"& 'C:\Python\pythonw.exe' 'C:\Users\test\.codex_screen\codex_hook_relay.py'",
            parsed["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"],
        )
        self.assertEqual(10, block.count("timeout = 5"))
        self.assertEqual(1, block.count("timeout = 3"))
        self.assertEqual(1, block.count("# BEGIN codex-monitor-hook"))
        self.assertEqual(1, block.count("# END codex-monitor-hook"))

    def test_default_profile_contains_only_quota_refresh_hooks(self):
        block = build_hook_block(
            r"C:\Python\pythonw.exe",
            r"C:\Users\test\.codex_screen\codex_hook_relay.py",
        )

        for event_name in QUOTA_HOOK_EVENTS:
            self.assertIn(f"[[hooks.{event_name}]]", block)
        for event_name in set(HOOK_EVENTS) - set(QUOTA_HOOK_EVENTS):
            self.assertNotIn(f"[[hooks.{event_name}]]", block)
        self.assertEqual(len(QUOTA_HOOK_EVENTS), block.count('type = "command"'))

    def test_builds_minimal_hooks_for_performance_mode(self):
        block = build_hook_block(
            r"C:\Python\pythonw.exe",
            r"C:\Users\test\.codex_screen\codex_hook_relay.py",
            hook_profile="minimal",
        )

        for event_name in MINIMAL_HOOK_EVENTS:
            self.assertIn(f"[[hooks.{event_name}]]", block)

        self.assertNotIn("[[hooks.PreToolUse]]", block)
        self.assertNotIn("[[hooks.PostToolUse]]", block)
        self.assertEqual(len(MINIMAL_HOOK_EVENTS), block.count('type = "command"'))

    def test_powershell_command_escapes_path_apostrophes(self):
        command = powershell_command(r"C:\O'Brien\pythonw.exe", r"C:\O'Brien\relay.py")

        self.assertEqual(
            "& 'C:\\O''Brien\\pythonw.exe' 'C:\\O''Brien\\relay.py'",
            command,
        )

    def test_builds_posix_command_without_windows_shell_field(self):
        block = build_hook_block(
            "/opt/homebrew/bin/python3",
            "/Users/test/.codex_screen/codex_hook_relay.py",
            platform_name="posix",
        )

        parsed = tomllib.loads(block)
        hook = parsed["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(
            "/opt/homebrew/bin/python3 /Users/test/.codex_screen/codex_hook_relay.py",
            hook["command"],
        )
        self.assertNotIn("commandWindows", hook)

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

        merged = merge_hook_block(
            old_text,
            build_hook_block("pythonw.exe", "relay.py", hook_profile="full"),
        )

        self.assertEqual(1, merged.count(START_MARKER))
        self.assertEqual(1, merged.count(END_MARKER))
        self.assertIn("[hooks.state]", merged)
        self.assertIn('trusted_hash = "sha256:test"', merged)
        self.assertIn("[features]", merged)


if __name__ == "__main__":
    unittest.main()
