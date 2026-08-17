import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_hook_relay as relay
from codex_quota_client import (
    INTERNAL_DAEMON_SHUTDOWN_EVENT,
    INTERNAL_QUOTA_REFRESH_EVENT,
)
from codex_relay_state import (
    RelaySessionState,
    load_state,
    save_state,
)
from codex_runtime_config import write_active_port


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

    def test_daemon_command_uses_pythonw_for_script_daemon_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            daemon_script = base_dir / "codex_screen_daemon.py"
            daemon_script.write_text("", encoding="utf-8")
            python_exe = base_dir / "python.exe"
            pythonw_exe = base_dir / "pythonw.exe"
            python_exe.write_text("", encoding="utf-8")
            pythonw_exe.write_text("", encoding="utf-8")

            with patch.object(relay.os, "name", "nt"), patch.object(
                relay, "DAEMON_SCRIPT", daemon_script
            ), patch.object(relay.sys, "executable", str(python_exe)):
                self.assertEqual(
                    [str(pythonw_exe), str(daemon_script), "--daemon"],
                    relay._daemon_command(),
                )

    def test_daemon_command_keeps_source_fallback_for_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            daemon_script = base_dir / "codex_screen_daemon.py"
            daemon_script.write_text("", encoding="utf-8")

            with patch.object(relay, "DAEMON_SCRIPT", daemon_script), patch.object(
                relay.sys, "executable", "python3"
            ):
                command = relay._daemon_command()

            self.assertEqual(str(daemon_script), command[1])
            self.assertEqual("--daemon", command[2])

    def test_relay_candidate_ports_prefers_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            write_active_port(base_dir, "127.0.0.1", 27688, 1234)

            with patch.object(relay, "BASE_DIR", base_dir):
                ports = relay.relay_candidate_ports(relay.BASE_DIR)

        self.assertEqual(27688, ports[0])
        self.assertIn(12688, ports)

    def test_cold_start_spawns_daemon_before_trying_fallback_ports(self):
        packet = {"event": {"hook_event_name": "UserPromptSubmit"}}
        calls = []

        def fake_send(_packet, port):
            calls.append(("send", port))
            return False

        def fake_spawn():
            calls.append(("spawn", None))
            return False

        with patch.object(
            relay, "relay_candidate_ports", return_value=[12688, 27688]
        ), patch.object(
            relay, "_send_packet", side_effect=fake_send
        ), patch.object(
            relay, "_spawn_detached_daemon", side_effect=fake_spawn
        ), patch.object(relay, "MAX_RETRY_COUNT", 1):
            self.assertFalse(relay._send_with_retries(packet))

        self.assertEqual([("send", 12688), ("spawn", None)], calls[:2])
        self.assertNotIn(("send", 27688), calls)

    def test_session_start_starts_daemon_without_selecting_inactive_session(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            state_file = base_dir / "relay-state.json"
            lock_file = base_dir / "relay.lock"

            with patch.object(relay, "BASE_DIR", base_dir), patch.object(
                relay, "RELAY_STATE_FILE", state_file
            ), patch.object(relay, "RELAY_LOCK_FILE", lock_file), patch.object(
                relay, "_daemon_is_reachable", return_value=False
            ), patch.object(
                relay, "_spawn_detached_daemon", return_value=True
            ) as spawn_daemon, patch.object(
                relay, "_send_with_retries", return_value=True
            ) as send_event:
                forwarded = relay._handle_hook_event(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "session-a",
                    }
                )

        self.assertFalse(forwarded)
        spawn_daemon.assert_called_once_with()
        send_event.assert_not_called()

    def test_quota_mode_forwards_refresh_without_status_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            lock_file = base_dir / "relay.lock"
            sent_packets = []

            with patch.object(relay, "BASE_DIR", base_dir), patch.object(
                relay, "RELAY_LOCK_FILE", lock_file
            ), patch.object(
                relay,
                "_send_with_retries",
                side_effect=lambda packet: sent_packets.append(packet) or True,
            ):
                forwarded = relay._dispatch_hook_event(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session-a",
                    }
                )

        self.assertTrue(forwarded)
        self.assertEqual(1, len(sent_packets))
        self.assertEqual(
            INTERNAL_QUOTA_REFRESH_EVENT,
            sent_packets[0]["event"]["_internal_kind"],
        )
        self.assertEqual(
            "UserPromptSubmit",
            sent_packets[0]["event"]["hook_event_name"],
        )

    def test_quota_mode_sends_shutdown_on_session_end(self):
        sent_packets = []

        with patch.object(
            relay,
            "_send_with_retries",
            side_effect=lambda packet: sent_packets.append(packet) or True,
        ):
            forwarded = relay._dispatch_hook_event(
                {"hook_event_name": "SessionEnd", "session_id": "session-a"}
            )

        self.assertTrue(forwarded)
        self.assertEqual(
            INTERNAL_DAEMON_SHUTDOWN_EVENT,
            sent_packets[0]["event"]["_internal_kind"],
        )

    def test_session_start_does_not_spawn_when_daemon_is_reachable(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            state_file = base_dir / "relay-state.json"
            lock_file = base_dir / "relay.lock"

            with patch.object(relay, "BASE_DIR", base_dir), patch.object(
                relay, "RELAY_STATE_FILE", state_file
            ), patch.object(relay, "RELAY_LOCK_FILE", lock_file), patch.object(
                relay, "_daemon_is_reachable", return_value=True
            ), patch.object(relay, "_spawn_detached_daemon") as spawn_daemon:
                forwarded = relay._handle_hook_event(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "session-a",
                    }
                )

        self.assertFalse(forwarded)
        spawn_daemon.assert_not_called()

    def test_main_ignores_unknown_args_and_still_processes_hook(self):
        events = []

        with patch.object(relay, "_read_hook_event", return_value={"hook_event_name": "SessionStart", "session_id": "session-a"}), patch.object(relay, "_dispatch_hook_event", side_effect=lambda event: events.append(event) or True), patch.object(relay, "log_line"):
            exit_code = relay.main(["--unknown", "value"])

        self.assertEqual(0, exit_code)
        self.assertEqual(["SessionStart"], [event["hook_event_name"] for event in events])

    def test_reads_hook_json_from_app_server_positional_argument(self):
        event_json = '{"hook_event_name":"UserPromptSubmit","session_id":"session-a"}'

        with patch.object(relay.sys, "stdin", io.StringIO("")):
            event = relay._read_hook_event([event_json])

        self.assertEqual("UserPromptSubmit", event["hook_event_name"])
        self.assertEqual("session-a", event["session_id"])

    def test_main_accepts_event_json_in_argv_zero(self):
        events = []
        event_json = '{"hook_event_name":"SessionStart","session_id":"session-a"}'

        with patch.object(relay.sys, "argv", [event_json]), patch.object(
            relay, "_dispatch_hook_event", side_effect=lambda event: events.append(event)
        ), patch.object(relay, "log_line"):
            exit_code = relay.main()

        self.assertEqual(0, exit_code)
        self.assertEqual("SessionStart", events[0]["hook_event_name"])

    def test_user_prompt_selects_session_but_background_events_do_not(self):
        state = RelaySessionState()

        state.apply_event(
            {"hook_event_name": "SessionStart", "session_id": "session-a"}
        )
        self.assertIsNone(state.active_session_id)
        state.apply_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-a",
                "turn_id": "turn-a",
            }
        )
        self.assertEqual("session-a", state.active_session_id)
        state.apply_event(
            {"hook_event_name": "SessionStart", "session_id": "session-b"}
        )
        self.assertEqual("session-a", state.active_session_id)
        state.apply_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-b",
                "turn_id": "turn-b",
            }
        )
        self.assertEqual("session-a", state.active_session_id)
        state.apply_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-b",
                "turn_id": "turn-b",
            }
        )

        self.assertEqual("session-b", state.active_session_id)
        self.assertFalse(state.should_forward("session-a"))
        self.assertTrue(state.should_forward("session-b"))

    def test_active_session_switch_replays_only_target_cached_state(self):
        state = RelaySessionState()
        state.apply_event(
            {"hook_event_name": "SessionStart", "session_id": "session-a"}
        )
        state.apply_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-a",
                "turn_id": "turn-a",
            }
        )
        state.apply_event(
            {"hook_event_name": "SessionStart", "session_id": "session-b"}
        )
        state.apply_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-b",
                "turn_id": "turn-b",
            }
        )
        state.apply_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-b",
                "turn_id": "turn-b",
                "tool_use_id": "shared-tool-id",
            }
        )

        state.set_active_session("session-b")
        self.assertTrue(state.should_forward("session-b"))
        self.assertFalse(state.should_forward("session-a"))
        self.assertEqual(
            ["SessionStart", "UserPromptSubmit", "PreToolUse"],
            [event["hook_event_name"] for event in state.replay_events("session-b")],
        )

        state.set_active_session("session-a")
        self.assertEqual(
            ["SessionStart", "UserPromptSubmit"],
            [event["hook_event_name"] for event in state.replay_events("session-a")],
        )

    def test_duplicate_and_old_turn_events_are_ignored_per_session(self):
        state = RelaySessionState()
        start = {"hook_event_name": "SessionStart", "session_id": "session-a"}
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-a",
            "turn_id": "turn-a",
        }
        stop = {
            "hook_event_name": "Stop",
            "session_id": "session-a",
            "turn_id": "turn-a",
        }

        self.assertTrue(state.apply_event(start).accepted)
        self.assertTrue(state.apply_event(prompt).accepted)
        self.assertFalse(state.apply_event(prompt).accepted)
        self.assertTrue(state.apply_event(stop).accepted)
        self.assertFalse(
            state.apply_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "session-a",
                    "turn_id": "turn-a",
                }
            ).accepted
        )

    def test_new_prompt_replaces_an_unclosed_previous_turn(self):
        state = RelaySessionState()
        state.apply_event({"hook_event_name": "SessionStart", "session_id": "session-a"})
        self.assertTrue(
            state.apply_event(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-a",
                    "turn_id": "turn-a",
                }
            ).accepted
        )

        result = state.apply_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-a",
                "turn_id": "turn-b",
            }
        )

        self.assertTrue(result.accepted)
        self.assertEqual("turn-b", state.sessions["session-a"].turn_id)
        self.assertFalse(
            state.apply_event(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "session-a",
                    "turn_id": "turn-a",
                }
            ).accepted
        )

    def test_same_tool_use_id_is_scoped_to_each_session(self):
        state = RelaySessionState()

        for session_id, turn_id in (("session-a", "turn-a"), ("session-b", "turn-b")):
            state.apply_event(
                {"hook_event_name": "SessionStart", "session_id": session_id}
            )
            state.apply_event(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": turn_id,
                }
            )
            result = state.apply_event(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "tool_use_id": "same-tool-id",
                }
            )
            self.assertTrue(result.accepted)

    def test_unknown_session_has_no_replay_packets(self):
        state = RelaySessionState()

        state.set_active_session("unknown-session")

        self.assertEqual([], state.replay_events("unknown-session"))
        self.assertEqual("unknown-session", state.active_session_id)

    def test_relay_auto_selects_on_user_prompt_and_filters_background_events(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            state_file = base_dir / "relay-state.json"
            lock_file = base_dir / "relay.lock"
            sent_events = []
            log_messages = []

            def fake_send(packet):
                sent_events.append(packet["event"]["hook_event_name"])
                return True

            patches = [
                patch.object(relay, "BASE_DIR", base_dir),
                patch.object(relay, "RELAY_STATE_FILE", state_file),
                patch.object(relay, "RELAY_LOCK_FILE", lock_file),
                patch.object(relay, "_send_with_retries", side_effect=fake_send),
                patch.object(relay, "_daemon_is_reachable", return_value=False),
                patch.object(relay, "_spawn_detached_daemon", return_value=True),
                patch.object(
                    relay,
                    "log_line",
                    side_effect=lambda scope, message: log_messages.append(
                        (scope, message)
                    ),
                ),
            ]
            for item in patches:
                item.start()
            try:
                self.assertFalse(
                    relay._handle_hook_event(
                        {
                            "hook_event_name": "SessionStart",
                            "session_id": "session-a",
                        }
                    )
                )
                relay._handle_hook_event(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session-a",
                        "turn_id": "turn-a",
                    }
                )
                self.assertEqual(["UserPromptSubmit"], sent_events)
                relay._handle_hook_event(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "session-b",
                    }
                )
                self.assertEqual(["UserPromptSubmit"], sent_events)

                relay._handle_hook_event(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session-b",
                        "turn_id": "turn-b",
                    }
                )
                self.assertEqual(
                    ["UserPromptSubmit", "UserPromptSubmit"],
                    sent_events,
                )

                relay._handle_hook_event(
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "session-a",
                        "turn_id": "turn-a",
                    }
                )
                self.assertEqual(
                    [
                        "UserPromptSubmit",
                        "UserPromptSubmit",
                    ],
                    sent_events,
                )
                self.assertTrue(
                    any(
                        "cached event=PreToolUse" in message
                        and "forwarded=no" in message
                        and "session=session-a" in message
                        for _, message in log_messages
                    )
                )

                relay._handle_hook_event(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "session-b",
                        "turn_id": "turn-b",
                    }
                )
                self.assertEqual(
                    [
                        "UserPromptSubmit",
                        "UserPromptSubmit",
                        "Stop",
                    ],
                    sent_events,
                )
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_relay_state_is_bounded_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "relay-state.json"
            state = RelaySessionState()
            for index in range(40):
                state.apply_event(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": f"session-{index}",
                    }
                )

            save_state(state_path, state)
            restored = load_state(state_path)

            self.assertLessEqual(
                len(restored.sessions),
                RelaySessionState.MAX_SESSIONS,
            )
            self.assertLess(state_path.stat().st_size, 256 * 1024)


if __name__ == "__main__":
    unittest.main()
