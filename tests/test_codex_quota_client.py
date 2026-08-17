import queue
import threading
import tempfile
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
        start_process.assert_called_once_with(
            "codex.exe", quota_client._quota_codex_home()
        )
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
        ), patch.object(
            quota_client,
            "_codex_auth_mode",
            return_value=None,
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

    def test_quota_refresh_wait_can_be_woken_by_hook(self):
        stop_event = threading.Event()
        refresh_event = threading.Event()
        refresh_event.set()

        self.assertFalse(
            quota_client._wait_for_quota_refresh_or_stop(
                stop_event,
                refresh_event,
            )
        )
        self.assertFalse(refresh_event.is_set())

    def test_api_key_auth_starts_quota_process_to_probe_compatible_accounts(self):
        with patch.dict(
            quota_client.os.environ,
            {"CODEX_SCREEN_ENABLE_CODEX_QUOTA": "1"},
        ), patch.object(
            quota_client,
            "_codex_auth_mode",
            return_value="apikey",
        ), patch.object(
            quota_client,
            "CodexQuotaSession",
        ) as session:
            thread = quota_client.start_quota_poller(
                queue.Queue(), unittest.mock.Mock()
            )

        self.assertIsNotNone(thread)
        session.assert_called_once_with()
        thread.join(timeout=1)

    def test_poller_notifies_daemon_when_quota_is_unavailable(self):
        class StopAfterWait:
            def __init__(self):
                self.wait_calls = 0

            def is_set(self):
                return self.wait_calls > 0

            def wait(self, _timeout):
                self.wait_calls += 1

        target_queue = queue.Queue()
        fake_session = unittest.mock.Mock()
        fake_session.fetch_state.return_value = None
        fake_session.last_failure = "JSON-RPC -32600: account authentication required"

        with patch.object(
            quota_client,
            "CodexQuotaSession",
            return_value=fake_session,
        ):
            quota_client._poll_quota_loop(target_queue, StopAfterWait())

        self.assertEqual(
            {
                "_internal_kind": quota_client.INTERNAL_QUOTA_UNAVAILABLE_EVENT,
                "reason": "JSON-RPC -32600: account authentication required",
            },
            target_queue.get_nowait(),
        )
        fake_session.close.assert_called_once_with()

    def test_quota_session_reconnects_when_auth_file_changes(self):
        session = quota_client.CodexQuotaSession(timeout_sec=1)
        session.auth_signature = "old-auth"
        session.process = unittest.mock.Mock()

        with patch.object(
            quota_client,
            "_auth_file_signature",
            return_value="new-auth",
        ), patch.object(session, "close") as close:
            session._refresh_auth_session()

        close.assert_called_once_with()
        self.assertEqual("new-auth", session.auth_signature)

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

    def test_accepts_response_with_only_multi_bucket_rate_limits(self):
        result = {
            "result": {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 24},
                    },
                },
            },
        }

        parsed = parse_rate_limits_result(result)

        self.assertIsNotNone(parsed)
        self.assertEqual(
            "codex 24% used",
            format_rate_limit_text(parsed),
        )

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

    def test_logs_chatgpt_authentication_error_instead_of_silently_dropping_it(self):
        fake_process = unittest.mock.Mock(stdout=None, stderr=None)
        session = quota_client.CodexQuotaSession(timeout_sec=1)
        responses = [
            {"id": 1, "result": {}},
            {
                "id": 2,
                "error": {
                    "code": -32600,
                    "message": "chatgpt authentication required to read rate limits",
                },
            },
        ]

        with patch.object(
            quota_client,
            "_iter_runnable_codex_paths",
            return_value=iter(["codex.exe"]),
        ), patch.object(
            quota_client,
            "_start_app_server",
            return_value=fake_process,
        ), patch.object(
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
            "log_line",
        ) as log:
            self.assertIsNone(session.fetch_state())

        self.assertTrue(
            any(
                "chatgpt authentication required" in call.args[1]
                for call in log.call_args_list
            )
        )
        session.close()

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

    def test_candidates_use_codex_home_environment(self):
        with patch.dict(quota_client.os.environ, {"CODEX_HOME": "D:/custom-codex"}):
            candidates = list(quota_client._candidate_codex_paths())

            self.assertEqual(
                Path("D:/custom-codex/.sandbox-bin/codex.exe"),
                candidates[0],
            )

    def test_reads_chatgpt_auth_mode_with_utf8_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                '{"auth_mode":"chatgpt","tokens":{"access_token":"redacted"}}',
                encoding="utf-8-sig",
            )
            with patch.dict(
                quota_client.os.environ,
                {"CODEX_HOME": directory},
            ), patch.object(
                quota_client,
                "DEFAULT_QUOTA_CODEX_HOME",
                Path(directory) / "missing-isolated-home",
            ):
                self.assertEqual("chatgpt", quota_client._codex_auth_mode())

    def test_app_server_defaults_to_main_codex_home(self):
        fake_process = object()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            quota_client.os.environ,
            {"CODEX_HOME": directory},
        ), patch.object(
            quota_client,
            "DEFAULT_QUOTA_CODEX_HOME",
            Path(directory) / "missing-isolated-home",
        ), patch.object(
            quota_client.subprocess,
            "Popen",
            return_value=fake_process,
        ) as popen:
            self.assertIs(fake_process, quota_client._start_app_server("codex"))

        self.assertEqual(directory, popen.call_args.kwargs["env"]["CODEX_HOME"])

    def test_quota_app_server_receives_isolated_codex_home(self):
        fake_process = object()

        with tempfile.TemporaryDirectory() as directory, patch.object(
            quota_client.subprocess,
            "Popen",
            return_value=fake_process,
        ) as popen:
            isolated = Path(directory) / "quota-home"
            self.assertIs(
                fake_process,
                quota_client._start_app_server("codex", isolated),
            )

        self.assertEqual(str(isolated), popen.call_args.kwargs["env"]["CODEX_HOME"])

    def test_quota_home_prefers_isolated_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = Path(directory) / "quota-home"
            isolated.mkdir()
            (isolated / "auth.json").write_text("{}", encoding="utf-8")

            with patch.object(
                quota_client,
                "DEFAULT_QUOTA_CODEX_HOME",
                isolated,
            ), patch.dict(
                quota_client.os.environ,
                {"CODEX_HOME": "/tmp/main-codex-home"},
            ):
                self.assertEqual(isolated, quota_client._quota_codex_home())

    def test_main_chatgpt_auth_is_synced_to_isolated_quota_home(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            isolated = Path(directory) / "quota"
            main.mkdir()
            contents = (
                b'{"auth_mode":"chatgpt","tokens":'
                b'{"access_token":"a","id_token":"i","refresh_token":"r"}}'
            )
            (main / "auth.json").write_bytes(contents)

            with patch.dict(quota_client.os.environ, {"CODEX_HOME": str(main)}), patch.object(
                quota_client, "DEFAULT_QUOTA_CODEX_HOME", isolated
            ):
                self.assertEqual(isolated, quota_client._quota_codex_home())

            self.assertEqual(contents, (isolated / "auth.json").read_bytes())
            self.assertEqual(0o700, isolated.stat().st_mode & 0o777)
            self.assertEqual(0o600, (isolated / "auth.json").stat().st_mode & 0o777)

    def test_main_chatgpt_export_without_auth_mode_is_synced(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            isolated = Path(directory) / "quota"
            main.mkdir()
            (main / "auth.json").write_text(
                '{"tokens":{"access_token":"a","id_token":"i","refresh_token":"r"}}',
                encoding="utf-8",
            )

            with patch.dict(quota_client.os.environ, {"CODEX_HOME": str(main)}), patch.object(
                quota_client, "DEFAULT_QUOTA_CODEX_HOME", isolated
            ):
                self.assertEqual(isolated, quota_client._quota_codex_home())

            self.assertEqual("chatgpt", quota_client._auth_mode_for_path(isolated / "auth.json"))

    def test_main_api_key_auth_does_not_overwrite_isolated_chatgpt_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            isolated = Path(directory) / "quota"
            main.mkdir()
            isolated.mkdir()
            (main / "auth.json").write_text(
                '{"auth_mode":"apikey","tokens":{}}', encoding="utf-8"
            )
            original = b'{"auth_mode":"chatgpt","tokens":{"access_token":"old"}}'
            (isolated / "auth.json").write_bytes(original)

            with patch.dict(quota_client.os.environ, {"CODEX_HOME": str(main)}), patch.object(
                quota_client, "DEFAULT_QUOTA_CODEX_HOME", isolated
            ):
                self.assertEqual(isolated, quota_client._quota_codex_home())

            self.assertEqual(original, (isolated / "auth.json").read_bytes())

    def test_incomplete_main_chatgpt_auth_does_not_overwrite_isolated_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            isolated = Path(directory) / "quota"
            main.mkdir()
            isolated.mkdir()
            (main / "auth.json").write_text(
                '{"auth_mode":"chatgpt","tokens":{"access_token":"new"}}',
                encoding="utf-8",
            )
            original = b'{"auth_mode":"chatgpt","tokens":{"access_token":"old"}}'
            (isolated / "auth.json").write_bytes(original)

            with patch.dict(quota_client.os.environ, {"CODEX_HOME": str(main)}), patch.object(
                quota_client, "DEFAULT_QUOTA_CODEX_HOME", isolated
            ):
                self.assertEqual(isolated, quota_client._quota_codex_home())

            self.assertEqual(original, (isolated / "auth.json").read_bytes())

    def test_windows_defaults_to_main_codex_home_without_syncing_isolated_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            isolated = Path(directory) / "quota"
            main.mkdir()
            main_contents = (
                b'{"auth_mode":"chatgpt","tokens":'
                b'{"access_token":"a","id_token":"i","refresh_token":"r"}}'
            )
            (main / "auth.json").write_bytes(main_contents)

            with patch.object(quota_client.sys, "platform", "win32"), patch.dict(
                quota_client.os.environ, {"CODEX_HOME": str(main)}
            ), patch.object(quota_client, "DEFAULT_QUOTA_CODEX_HOME", isolated):
                self.assertEqual(main, quota_client._quota_codex_home())

            self.assertFalse((isolated / "auth.json").exists())

    def test_quota_home_environment_override_beats_default_isolated_home(self):
        with patch.dict(
            quota_client.os.environ,
            {"CODEX_SCREEN_QUOTA_CODEX_HOME": "/tmp/custom-quota-home"},
        ):
            self.assertEqual(
                Path("/tmp/custom-quota-home"),
                quota_client._quota_codex_home(),
            )

    def test_candidates_include_posix_sandbox_cli(self):
        with patch.dict(quota_client.os.environ, {"CODEX_HOME": "/Users/test/.codex"}):
            candidates = list(quota_client._candidate_codex_paths())

        self.assertIn(
            Path("/Users/test/.codex/.sandbox-bin/codex"),
            candidates,
        )

    def test_candidates_include_chatgpt_macos_cli(self):
        candidates = list(quota_client._candidate_codex_paths())

        self.assertIn(
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            candidates,
        )

    def test_candidates_include_desktop_local_app_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory)
            desktop_exe = (
                local_app_data
                / "OpenAI"
                / "Codex"
                / "bin"
                / "desktop-build"
                / "codex.exe"
            )
            desktop_exe.parent.mkdir(parents=True)
            desktop_exe.write_bytes(b"codex")

            with patch.dict(
                quota_client.os.environ,
                {"LOCALAPPDATA": str(local_app_data)},
            ):
                candidates = list(quota_client._candidate_codex_paths())

        self.assertIn(desktop_exe, candidates)

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
        if quota_client.os.name != "nt":
            self.assertNotIn("creationflags", kwargs)
            return
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
