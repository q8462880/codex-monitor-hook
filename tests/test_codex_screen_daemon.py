import sys
import tempfile
import unittest
import queue
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_screen_daemon as daemon
from codex_state_manager import CodexStateManager


class CodexScreenDaemonTest(unittest.TestCase):
    def test_daemon_instance_lock_is_released_after_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "daemon.lock"
            with patch.object(daemon, "BASE_DIR", Path(directory)):
                with daemon._daemon_instance_lock(lock_path) as first_acquired:
                    self.assertTrue(first_acquired)

                with daemon._daemon_instance_lock(lock_path) as second_acquired:
                    self.assertTrue(second_acquired)

    def test_frame_seq_reservation_survives_daemon_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            state_file = base_dir / "codex_monitor_frame_seq"
            with patch.object(daemon, "BASE_DIR", base_dir), patch.object(
                daemon, "FRAME_SEQ_STATE_FILE", state_file
            ):
                first_seq = daemon._reserve_frame_seq()
                second_seq = daemon._reserve_frame_seq()

        self.assertGreater(second_seq, first_seq)
        self.assertEqual(
            daemon.FRAME_SEQ_RESERVATION,
            second_seq - first_seq,
        )

    def test_hook_events_map_to_screen_states(self):
        expected = {
            "SessionStart": daemon.STATE_IDLE,
            "UserPromptSubmit": daemon.STATE_THINKING,
            "PermissionRequest": daemon.STATE_WAIT_PERM,
            "PreToolUse": daemon.STATE_EXECUTING,
            "PostToolUse": daemon.STATE_THINKING,
            "PreCompact": daemon.STATE_COMPACTING,
            "PostCompact": daemon.STATE_THINKING,
            "SubagentStart": daemon.STATE_SUBAGENT,
            "SubagentStop": daemon.STATE_THINKING,
            "Stop": daemon.STATE_IDLE,
            "SessionEnd": daemon.STATE_IDLE,
        }

        self.assertEqual(expected, daemon.EVENT_STATE_MAP)

    def test_queue_updates_state_without_hid_protocol(self):
        instance = daemon.CodexScreenDaemon()
        instance.queue.put(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "shell_command",
                "session_id": "session-test",
            }
        )

        self.assertTrue(instance._drain_queue())
        self.assertEqual(daemon.STATE_EXECUTING, instance.state["status"])
        self.assertEqual("shell_command", instance.state["tool_name"])
        self.assertIsNone(instance.dev)

    def test_state_queue_drops_oldest_event_when_full(self):
        state_queue = daemon.BoundedStateQueue(maxsize=1)
        state_queue.put({"hook_event_name": "PreToolUse"})
        state_queue.put({"hook_event_name": "Stop"})

        self.assertEqual(1, state_queue.qsize())
        self.assertEqual(
            {"hook_event_name": "Stop"},
            state_queue.get_nowait(),
        )
        with self.assertRaises(queue.Empty):
            state_queue.get_nowait()

    def test_render_frame_matches_firmware_codex_monitor_protocol(self):
        instance = daemon.CodexScreenDaemon()
        instance._apply_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-test",
                "quota": {
                    "current_used_percent": 42,
                    "weekly_used_percent": 11,
                    "current_reset_sec": 131,
                    "weekly_reset_sec": 9000,
                },
            }
        )

        frame = instance._render_frame()

        self.assertTrue(daemon.HID_PROTOCOL_READY)
        self.assertEqual(daemon.HID_REPORT_SIZE, len(frame))
        self.assertEqual(daemon.SCREEN_HID_CMD_CODEX_MONITOR, frame[0])
        self.assertEqual(daemon.SCREEN_HID_CODEX_SUBCMD_STATE, frame[1])
        self.assertEqual(daemon.SCREEN_HID_PROTOCOL_VERSION, frame[2])
        self.assertEqual(daemon.SCREEN_HID_CODEX_STATUS_EXECUTING, frame[8])
        self.assertEqual(42, frame[10])
        self.assertEqual(11, frame[11])
        self.assertEqual(131, int.from_bytes(frame[12:16], "little"))
        self.assertEqual(9000, int.from_bytes(frame[16:20], "little"))

    def test_hid_heartbeat_is_due_without_new_hook_event(self):
        instance = daemon.CodexScreenDaemon()
        instance.last_frame_written_at = 100.0
        previous_seq = instance.state["frame_seq"]

        self.assertFalse(instance._heartbeat_due(104.9))
        self.assertTrue(instance._heartbeat_due(105.0))

        instance._refresh_heartbeat(105.0)
        self.assertEqual(previous_seq + 1, instance.state["frame_seq"])
        self.assertEqual(105.0, instance.last_frame_written_at)

    def test_turn_lifecycle_returns_idle_only_on_matching_stop(self):
        manager = CodexStateManager()

        self.assertTrue(manager.apply_event("SessionStart", "session-a"))
        self.assertTrue(manager.apply_event("UserPromptSubmit", "session-a", "turn-1"))
        self.assertEqual(daemon.STATE_THINKING, manager.status)
        self.assertTrue(manager.apply_event("PreToolUse", "session-a", "turn-1"))
        self.assertEqual(daemon.STATE_EXECUTING, manager.status)
        self.assertTrue(manager.apply_event("PostToolUse", "session-a", "turn-1"))
        self.assertEqual(daemon.STATE_THINKING, manager.status)
        self.assertTrue(manager.apply_event("Stop", "session-a", "turn-1"))
        self.assertEqual(daemon.STATE_IDLE, manager.status)
        self.assertFalse(manager.turn_active)
        self.assertTrue(manager.session_active)

    def test_old_session_stop_cannot_stop_new_session(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-a")
        manager.apply_event("SessionStart", "session-b")
        manager.apply_event("UserPromptSubmit", "session-b", "turn-b")

        self.assertFalse(manager.apply_event("Stop", "session-a", "turn-a"))
        self.assertEqual(daemon.STATE_THINKING, manager.status)
        self.assertEqual("session-b", manager.active_session_id)
        self.assertTrue(manager.turn_active)

    def test_old_turn_events_are_ignored_after_stop(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-1")
        manager.apply_event("Stop", "session-a", "turn-1")
        self.assertFalse(manager.apply_event("PostToolUse", "session-a", "turn-1"))
        self.assertFalse(manager.apply_event("PostToolUse", "session-a"))
        self.assertEqual(daemon.STATE_IDLE, manager.status)

    def test_stop_without_turn_id_cannot_stop_active_turn(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-1")

        self.assertFalse(manager.apply_event("Stop", "session-a"))
        self.assertEqual(daemon.STATE_THINKING, manager.status)
        self.assertTrue(manager.turn_active)

    def test_subagent_stop_restores_previous_detail_state(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-1")
        manager.apply_event("PreToolUse", "session-a", "turn-1")
        manager.apply_event("SubagentStart", "session-a", "turn-1")
        self.assertEqual(daemon.STATE_SUBAGENT, manager.status)
        manager.apply_event("SubagentStop", "session-a", "turn-1")
        self.assertEqual(daemon.STATE_EXECUTING, manager.status)

    def test_running_event_can_implicitly_start_turn(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        self.assertTrue(manager.apply_event("PreToolUse", "session-a", "turn-1"))
        self.assertTrue(manager.turn_active)
        self.assertEqual(daemon.STATE_EXECUTING, manager.status)

    def test_prompt_can_switch_session_when_session_start_was_missed(self):
        manager = CodexStateManager()

        manager.apply_event("UserPromptSubmit", "session-a", "turn-a")
        self.assertTrue(
            manager.apply_event("UserPromptSubmit", "session-b", "turn-b")
        )
        self.assertEqual("session-b", manager.active_session_id)
        self.assertEqual("turn-b", manager.last_turn_id)
        self.assertEqual(daemon.STATE_THINKING, manager.status)

    def test_session_end_closes_only_the_active_session(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-a")
        self.assertFalse(manager.apply_event("SessionEnd", "session-b"))
        self.assertTrue(manager.session_active)
        self.assertTrue(manager.apply_event("SessionEnd", "session-a"))
        self.assertFalse(manager.session_active)
        self.assertFalse(manager.turn_active)
        self.assertEqual(daemon.STATE_IDLE, manager.status)


if __name__ == "__main__":
    unittest.main()
