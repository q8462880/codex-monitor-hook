import sys
import tempfile
import unittest
import queue
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_screen_daemon as daemon
import codex_macos_usb
from codex_quota_client import INTERNAL_QUOTA_EVENT
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

    def test_bind_falls_back_when_preferred_port_is_unavailable(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fallback_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind((daemon.HOST, 0))
            blocker.listen(1)
            blocked_port = blocker.getsockname()[1]

            fallback_probe.bind((daemon.HOST, 0))
            fallback_port = fallback_probe.getsockname()[1]
        finally:
            fallback_probe.close()

        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            instance = daemon.CodexScreenDaemon(frame_seq=1)
            with patch.object(daemon, "BASE_DIR", base_dir), patch.object(
                daemon, "get_candidate_ports", return_value=[blocked_port, fallback_port]
            ):
                server = instance._bind()
        try:
            self.assertEqual(fallback_port, instance.listen_port)
        finally:
            server.close()
            blocker.close()

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
        instance = daemon.CodexScreenDaemon(frame_seq=1)
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

    def test_quota_refresh_event_does_not_change_status(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)

        with patch.object(instance.quota_refresh_event, "set") as request_refresh:
            instance._apply_event(
                {
                    "_internal_kind": daemon.INTERNAL_QUOTA_REFRESH_EVENT,
                    "hook_event_name": "UserPromptSubmit",
                }
            )

        request_refresh.assert_called_once_with()
        self.assertEqual(daemon.STATE_IDLE, instance.state["status"])

    def test_shutdown_event_stops_daemon_after_session_end(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)

        instance._apply_event(
            {"_internal_kind": daemon.INTERNAL_DAEMON_SHUTDOWN_EVENT}
        )

        self.assertTrue(instance.stop.is_set())

    def test_quota_unavailable_hides_stale_quota_fields_on_hid(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.state["quota_text"] = "Codex 42% used"
        instance.state["current_used_percent"] = 42
        instance.state["weekly_used_percent"] = 11
        instance.state["current_reset_sec"] = 1800
        instance.state["weekly_reset_sec"] = 9000

        instance._apply_event(
            {
                "_internal_kind": daemon.INTERNAL_QUOTA_UNAVAILABLE_EVENT,
                "reason": "account authentication required",
            }
        )

        frame = instance._render_frame()

        self.assertEqual("", instance.state["quota_text"])
        self.assertEqual(daemon.SCREEN_HID_CODEX_PERCENT_INVALID, frame[10])
        self.assertEqual(daemon.SCREEN_HID_CODEX_PERCENT_INVALID, frame[11])
        self.assertEqual(0, frame[3] & daemon.SCREEN_HID_CODEX_FLAG_CURRENT_VALID)
        self.assertEqual(0, frame[3] & daemon.SCREEN_HID_CODEX_FLAG_WEEKLY_VALID)

    def test_missing_weekly_quota_stays_invalid_in_hid_frame(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        with patch.object(daemon, "log_line"):
            instance._apply_event(
                {
                    "_internal_kind": INTERNAL_QUOTA_EVENT,
                    "quota_text": "codex 0% used",
                    "current_used_percent": 0,
                    "current_reset_sec": 3600,
                    "weekly_used_percent": 0xFF,
                    "weekly_reset_sec": 0xFFFFFFFF,
                }
            )

        fields = instance._frame_business_fields(heartbeat=False)

        self.assertEqual(0, fields["current_percent"])
        self.assertEqual(3600, fields["current_reset"])
        self.assertEqual(daemon.SCREEN_HID_CODEX_PERCENT_INVALID, fields["weekly_percent"])
        self.assertEqual(daemon.SCREEN_HID_CODEX_RESET_INVALID, fields["weekly_reset"])
        self.assertEqual(
            daemon.SCREEN_HID_CODEX_FLAG_STATUS_VALID
            | daemon.SCREEN_HID_CODEX_FLAG_CURRENT_VALID
            | daemon.SCREEN_HID_CODEX_FLAG_CURRENT_RESET_VALID,
            fields["flags"],
        )

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
        instance = daemon.CodexScreenDaemon(frame_seq=1)
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

    def test_codex_daemon_uses_output_only_report_id_seven_channel(self):
        self.assertEqual(0x303A, daemon.HID_VENDOR_ID)
        self.assertEqual(0x8360, daemon.HID_PRODUCT_ID)
        self.assertEqual(0xFF01, daemon.HID_USAGE_PAGE)
        self.assertEqual(0x01, daemon.HID_USAGE)
        self.assertEqual(0x07, daemon.HID_REPORT_ID)
        self.assertFalse(daemon.HID_INPUT_ENABLED)

        class WritableDevice:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(bytes(data))
                return len(data)

        instance = daemon.CodexScreenDaemon(frame_seq=1)
        device = WritableDevice()
        instance.dev = device
        with patch.object(daemon, "log_line"):
            instance._write_frame(force=True)

        self.assertEqual(1, len(device.writes))
        self.assertEqual(daemon.HID_TRANSFER_SIZE, len(device.writes[0]))
        self.assertEqual(daemon.HID_REPORT_ID, device.writes[0][0])
        self.assertEqual(
            instance._render_frame()[: daemon.HID_OUTPUT_PAYLOAD_SIZE],
            device.writes[0][1:],
        )

    def test_codex_daemon_selects_vendor_monitor_collection(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        devices = [
            {"usage_page": 0x0001, "usage": 0x0006, "interface_number": 0},
            {"usage_page": 0xFF01, "usage": 0x0001, "interface_number": 2},
        ]

        selected = instance._pick_device(devices)

        self.assertIs(devices[1], selected)

    def test_codex_daemon_rejects_macos_rpc_collection_as_monitor(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        devices = [
            {"usage_page": 0x0001, "usage": 0x0006, "interface_number": 0},
            {"usage_page": 0xFF00, "usage": 0x0061, "interface_number": 1},
        ]

        selected = instance._pick_device(devices)

        self.assertIsNone(selected)

    def test_macos_uses_raw_interface_two_when_hidapi_hides_monitor(self):
        devices = [
            {"usage_page": 0x0001, "usage": 0x0006, "interface_number": 0},
            {"usage_page": 0xFF00, "usage": 0x0061, "interface_number": 1},
        ]

        class MacHid:
            @staticmethod
            def enumerate(_vendor_id, _product_id):
                return devices

        raw_device = MagicMock()
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.hid = MacHid()
        with patch.object(daemon.sys, "platform", "darwin"), patch.object(
            codex_macos_usb, "MacOSRawHIDDevice", return_value=raw_device
        ) as raw_factory, patch.object(daemon, "log_line"):
            self.assertTrue(instance._open_device())

        raw_factory.assert_called_once_with(
            daemon.HID_VENDOR_ID,
            daemon.HID_PRODUCT_ID,
            daemon.HID_INTERFACE_NUMBER,
            daemon.HID_TRANSFER_SIZE,
        )
        raw_device.open.assert_called_once_with()
        self.assertIs(raw_device, instance.dev)

    def test_windows_keeps_hidapi_interface_two_transport(self):
        monitor = {
            "path": b"windows-mi-02",
            "usage_page": 0xFF01,
            "usage": 0x0001,
            "interface_number": 2,
        }

        class WindowsDevice:
            def __init__(self):
                self.opened_path = None

            def open_path(self, path):
                self.opened_path = path

            def set_nonblocking(self, _enabled):
                pass

        device = WindowsDevice()

        class WindowsHid:
            @staticmethod
            def enumerate(_vendor_id, _product_id):
                return [
                    {"usage_page": 0xFF00, "usage": 0x0061, "interface_number": 1},
                    monitor,
                ]

            @staticmethod
            def device():
                return device

        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.hid = WindowsHid()
        with patch.object(daemon.sys, "platform", "win32"), patch.object(
            daemon, "log_line"
        ):
            self.assertTrue(instance._open_device())

        self.assertEqual(monitor["path"], device.opened_path)
        self.assertIs(device, instance.dev)

    def test_windows_monitor_frame_keeps_quota_and_reset_fields(self):
        class WritableDevice:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(bytes(data))
                return len(data)

        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.dev = WritableDevice()
        with patch.object(daemon.sys, "platform", "win32"), patch.object(
            daemon, "log_line"
        ):
            instance._apply_event(
                {
                    "_internal_kind": INTERNAL_QUOTA_EVENT,
                    "quota_text": "codex current=42 weekly=11",
                    "current_used_percent": 42,
                    "weekly_used_percent": 11,
                    "current_reset_sec": 3600,
                    "weekly_reset_sec": 86400,
                }
            )
            instance._write_frame(force=True)

        raw = instance.dev.writes[0]
        self.assertEqual(1024, len(raw))
        self.assertEqual(0x07, raw[0])
        self.assertEqual(0x24, raw[1])
        self.assertEqual(42, raw[11])
        self.assertEqual(11, raw[12])
        self.assertEqual(3600, int.from_bytes(raw[13:17], "little"))
        self.assertEqual(86400, int.from_bytes(raw[17:21], "little"))

    def test_codex_daemon_does_not_fallback_to_unknown_hid_collection(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        devices = [{"usage_page": 0x0001, "usage": 0x0006}]

        self.assertIsNone(instance._pick_device(devices))

    def test_hid_open_logs_when_device_is_not_enumerated(self):
        class EmptyHid:
            @staticmethod
            def enumerate(_vendor_id, _product_id):
                return []

        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.hid = EmptyHid()
        with patch.object(daemon.sys, "platform", "win32"), patch.object(
            daemon, "log_line"
        ) as log:
            self.assertFalse(instance._open_device())

        self.assertIn("HID monitor collection not found", log.call_args.args[1])

    def test_hid_heartbeat_is_due_without_new_hook_event(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.last_frame_written_at = 100.0
        previous_seq = instance.state["frame_seq"]

        self.assertFalse(instance._heartbeat_due(104.9))
        self.assertTrue(instance._heartbeat_due(105.0))

        instance._refresh_heartbeat(105.0)
        self.assertEqual(previous_seq + 1, instance.state["frame_seq"])
        self.assertEqual(105.0, instance.last_frame_written_at)

    def test_heartbeat_frame_does_not_repeat_business_state(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.state["status"] = daemon.STATE_THINKING
        instance.state["current_used_percent"] = 42
        instance.state["weekly_used_percent"] = 11
        instance.state["current_reset_sec"] = 131
        instance.state["weekly_reset_sec"] = 9000

        frame = instance._render_frame(heartbeat=True)

        self.assertEqual(0, frame[3])
        self.assertEqual(0xFF, frame[8])
        self.assertEqual(0xFF, frame[9])
        self.assertEqual(0xFF, frame[10])
        self.assertEqual(0xFF, frame[11])
        self.assertEqual(0xFFFFFFFF, int.from_bytes(frame[12:16], "little"))
        self.assertEqual(0xFFFFFFFF, int.from_bytes(frame[16:20], "little"))

    def test_heartbeat_write_does_not_extend_daemon_idle_lifetime(self):
        class WritableDevice:
            def write(self, data):
                return len(data)

        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.dev = WritableDevice()
        instance.last_activity = 100.0

        with patch.object(daemon.time, "monotonic", return_value=200.0):
            with patch.object(daemon, "log_line"):
                instance._write_frame(force=True, heartbeat=True)

        self.assertEqual(100.0, instance.last_activity)

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

    def test_stop_without_turn_id_can_stop_active_session_turn(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-1")

        self.assertTrue(manager.apply_event("Stop", "session-a"))
        self.assertEqual(daemon.STATE_IDLE, manager.status)
        self.assertFalse(manager.turn_active)

    def test_stale_hook_status_returns_to_ready_after_2_minutes(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a", now=100)
        manager.apply_event("UserPromptSubmit", "session-a", "turn-1", now=100)

        self.assertFalse(manager.expire_stale_status(120, now=219))
        self.assertTrue(manager.expire_stale_status(120, now=220))
        self.assertEqual(daemon.STATE_READY, manager.status)
        self.assertEqual("HookTimeout", manager.last_event)
        self.assertFalse(manager.turn_active)

    def test_stale_timeout_returns_all_running_states_to_ready(self):
        for event_name in (
            "PreToolUse",
            "PermissionRequest",
        ):
            manager = CodexStateManager()
            manager.apply_event("SessionStart", "session-a", now=100)
            manager.apply_event("UserPromptSubmit", "session-a", "turn-1", now=100)
            manager.apply_event(event_name, "session-a", "turn-1", now=100)

            self.assertTrue(manager.expire_stale_status(120, now=220))
            self.assertEqual(daemon.STATE_READY, manager.status)
            self.assertFalse(manager.turn_active)

    def test_completed_turn_returns_to_ready_after_2_minutes(self):
        manager = CodexStateManager()
        manager.apply_event("SessionStart", "session-a", now=100)
        manager.apply_event("UserPromptSubmit", "session-a", "turn-1", now=101)
        manager.apply_event("Stop", "session-a", "turn-1", now=102)

        self.assertTrue(manager.expire_stale_status(120, now=222))
        self.assertEqual(daemon.STATE_READY, manager.status)

    def test_daemon_applies_internal_turn_timeout_event(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.state_manager.apply_event("SessionStart", "session-a", now=100)
        instance.state_manager.apply_event(
            "UserPromptSubmit", "session-a", "turn-1", now=100
        )

        with patch.object(daemon, "HOOK_STALE_TIMEOUT_SEC", 120):
            with patch.object(daemon.time, "time", return_value=500):
                instance._apply_event(
                    {"_internal_kind": daemon.INTERNAL_TURN_TIMEOUT}
                )

        self.assertEqual(daemon.STATE_READY, instance.state["status"])
        self.assertEqual("HookTimeout", instance.state["last_event"])
        self.assertFalse(instance.state["turn_active"])

    def test_daemon_does_not_queue_timeout_again_when_already_ready(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.state_manager.apply_event("SessionStart", "session-a", now=100)
        instance.state_manager.apply_event(
            "UserPromptSubmit", "session-a", "turn-1", now=100
        )
        instance.state_manager.expire_stale_status(120, now=220)

        with patch.object(daemon, "HOOK_STALE_TIMEOUT_SEC", 120):
            instance._queue_stale_turn_timeout()

        self.assertTrue(instance.queue.empty())

    def test_ready_state_uses_dedicated_firmware_status(self):
        instance = daemon.CodexScreenDaemon(frame_seq=1)
        instance.state["status"] = daemon.STATE_READY

        frame = instance._render_frame()

        self.assertEqual(daemon.SCREEN_HID_CODEX_STATUS_READY, frame[8])

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

    def test_running_event_can_take_over_when_current_session_is_idle(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        self.assertTrue(
            manager.apply_event("PostToolUse", "session-b", "turn-b")
        )

        self.assertEqual("session-b", manager.active_session_id)
        self.assertEqual("turn-b", manager.last_turn_id)
        self.assertEqual(daemon.STATE_THINKING, manager.status)

    def test_running_event_cannot_take_over_active_turn(self):
        manager = CodexStateManager()

        manager.apply_event("SessionStart", "session-a")
        manager.apply_event("UserPromptSubmit", "session-a", "turn-a")

        self.assertFalse(
            manager.apply_event("PostToolUse", "session-b", "turn-b")
        )
        self.assertEqual("session-a", manager.active_session_id)
        self.assertEqual("turn-a", manager.last_turn_id)
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
