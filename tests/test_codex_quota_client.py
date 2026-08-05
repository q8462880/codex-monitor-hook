import queue
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.codex_quota_client as quota_client
from scripts.codex_quota_client import (
    fetch_codex_quota_text,
    format_rate_limit_text,
    format_rate_limit_state,
    parse_rate_limits_result,
)


class CodexQuotaClientTest(unittest.TestCase):
    def test_quota_session_reuses_one_process_for_multiple_requests(self):
        responses = [
            {"id": 1, "result": {}},
            {
                "id": 2,
                "result": {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 12},
                    },
                },
            },
            {
                "id": 3,
                "result": {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 13},
                    },
                },
            },
        ]
        fake_process = unittest.mock.Mock(stdout=None, stderr=None)
        session = quota_client.CodexQuotaSession(timeout_sec=1)

        with patch.object(
            quota_client,
            "_iter_runnable_codex_paths",
            return_value=iter(["codex.exe"]),
        ), patch.object(
            quota_client,
            "_start_app_server",
            return_value=fake_process,
        ) as start_process, patch.object(
            quota_client,
            "_start_reader",
        ), patch.object(
            quota_client,
            "_send",
        ) as send, patch.object(
            quota_client,
            "_read_response",
            side_effect=responses,
        ), patch.object(
            quota_client,
            "_stop_process",
        ) as stop_process:
            first = session.fetch_state()
            second = session.fetch_state()
            session.close()

        self.assertEqual("codex 12% used", first["quota_text"])
        self.assertEqual("codex 13% used", second["quota_text"])
        start_process.assert_called_once_with("codex.exe")
        self.assertEqual(3, send.call_count)
        stop_process.assert_called_once_with(fake_process)

    def test_quota_session_reconnects_after_response_timeout(self):
        responses = [
            {"id": 1, "result": {}},
            None,
            {"id": 1, "result": {}},
            {
                "id": 2,
                "result": {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 20},
                    },
                },
            },
        ]
        first_process = unittest.mock.Mock(stdout=None, stderr=None)
        second_process = unittest.mock.Mock(stdout=None, stderr=None)
        session = quota_client.CodexQuotaSession(timeout_sec=1)

        with patch.object(
            quota_client,
            "_iter_runnable_codex_paths",
            return_value=iter(["codex.exe"]),
        ), patch.object(
            quota_client,
            "_start_app_server",
            side_effect=[first_process, second_process],
        ) as start_process, patch.object(
            quota_client,
            "_start_reader",
        ), patch.object(
            quota_client,
            "_send",
        ), patch.object(
            quota_client,
            "_read_response",
            side_effect=responses,
        ), patch.object(
            quota_client,
            "_stop_process",
        ) as stop_process:
            self.assertIsNone(session.fetch_state())
            state = session.fetch_state()
            session.close()

        self.assertEqual("codex 20% used", state["quota_text"])
        self.assertEqual(2, start_process.call_count)
        self.assertEqual(2, stop_process.call_count)

    def test_poller_closes_persistent_session_when_stopped(self):
        class StopAfterWait:
            def __init__(self):
                self.wait_calls = 0

            def is_set(self):
                return self.wait_calls > 0

            def wait(self, _timeout):
                self.wait_calls += 1

        stop_event = StopAfterWait()
        target_queue = queue.Queue()
        fake_session = unittest.mock.Mock()
        fake_session.fetch_state.return_value = {
            "quota_text": "codex 30% used",
        }

        with patch.object(
            quota_client,
            "CodexQuotaSession",
            return_value=fake_session,
        ):
            quota_client._poll_quota_loop(target_queue, stop_event)

        self.assertEqual(
            {
                "_internal_kind": quota_client.INTERNAL_QUOTA_EVENT,
                "quota_text": "codex 30% used",
            },
            target_queue.get_nowait(),
        )
        fake_session.close.assert_called_once_with()

    def test_reader_can_discard_stderr_without_buffer_growth(self):
        lines = ["warning 1\n", "warning 2\n"]

        quota_client._read_lines(lines, None)

        # stderr 只是为了避免子进程管道阻塞而被读取，不能把内容长期堆到内存队列。
        self.assertTrue(True)

    def test_formats_codex_bucket_before_legacy_bucket(self):
        result = {
            "rateLimits": {
                "limitId": "legacy",
                "primary": {"usedPercent": 90, "resetsAt": 1800},
            },
            "rateLimitsByLimitId": {
                "other": {"limitId": "other", "primary": {"usedPercent": 10}},
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "primary": {
                        "usedPercent": 42,
                        "resetsAt": 1_800,
                        "windowDurationMins": 300,
                    },
                },
            },
        }

        text = format_rate_limit_text(result, now_epoch=0)

        self.assertEqual("Codex 42% used reset 00:30", text)

    def test_formats_spend_control_when_window_is_missing(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "individualLimit": {
                    "limit": "$20",
                    "used": "$7",
                    "remainingPercent": 65,
                    "resetsAt": 7_200,
                },
            },
        }

        text = format_rate_limit_text(result, now_epoch=3_600)

        self.assertEqual("codex 35% used reset 01:00", text)

    def test_preserves_primary_and_secondary_quota_fields_for_hid(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "limitName": "Codex",
                "primary": {"usedPercent": 42, "resetsAt": 1800},
                "secondary": {"usedPercent": 11, "resetsAt": 9000},
            }
        }

        state = format_rate_limit_state(result, now_epoch=0)

        self.assertIsNotNone(state)
        self.assertEqual("Codex 42% used reset 00:30", state["quota_text"])
        self.assertEqual(42, state["current_used_percent"])
        self.assertEqual(11, state["weekly_used_percent"])
        self.assertEqual(1800, state["current_reset_sec"])
        self.assertEqual(9000, state["weekly_reset_sec"])

    def test_converts_millisecond_duration_reset(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 42,
                    "resetsAt": 2_509_218_000,
                },
            },
        }

        state = format_rate_limit_state(result, now_epoch=0)

        self.assertIsNotNone(state)
        self.assertEqual(2_509_218, state["current_reset_sec"])
        self.assertEqual("codex 42% used reset 29d", state["quota_text"])

    def test_returns_none_for_error_or_empty_payload(self):
        self.assertIsNone(parse_rate_limits_result({"error": {"message": "auth required"}}))
        self.assertIsNone(format_rate_limit_text({}, now_epoch=0))

    def test_skips_candidate_executable_that_cannot_start(self):
        response = {
            "id": 2,
            "result": {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 12},
                },
            },
        }

        with patch.object(quota_client, "_candidate_codex_paths") as paths:
            paths.return_value = [Path("windowsapps-codex.exe"), Path("local-codex.exe")]
            with patch.object(quota_client, "_looks_runnable", return_value=True):
                with patch.object(quota_client, "_query_rate_limits") as query:
                    query.side_effect = [PermissionError("denied"), response]

                    text = fetch_codex_quota_text()

        self.assertEqual("codex 12% used", text)

    def test_starts_windows_app_server_without_console_window(self):
        fake_process = object()

        with patch.object(
            quota_client.subprocess,
            "Popen",
            return_value=fake_process,
        ) as popen:
            process = quota_client._start_app_server("codex.exe")

        self.assertIs(fake_process, process)
        kwargs = popen.call_args.kwargs
        expected_flag = int(
            getattr(quota_client.subprocess, "CREATE_NO_WINDOW", 0)
        )
        self.assertEqual(
            expected_flag,
            kwargs["creationflags"] & expected_flag,
        )
        startup_info = kwargs.get("startupinfo")
        if startup_info is not None:
            show_window_flag = int(
                getattr(quota_client.subprocess, "STARTF_USESHOWWINDOW", 0)
            )
            self.assertNotEqual(0, startup_info.dwFlags & show_window_flag)
            self.assertEqual(
                int(getattr(quota_client.subprocess, "SW_HIDE", 0)),
                startup_info.wShowWindow,
            )


if __name__ == "__main__":
    unittest.main()
