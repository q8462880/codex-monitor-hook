#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 额度查询工具。

这个模块只和 Codex app-server 的 JSON-RPC stdio 协议通信，不接触 HID。
如果查询失败，调用方会继续显示环境变量或 hook 事件里的占位额度。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


APP_SERVER_TIMEOUT_SEC = 8.0
CLIENT_NAME = "codex-monitor-hook"
CLIENT_VERSION = "0.1.0"
CODEX_EXE_ENV = "CODEX_SCREEN_CODEX_EXE"
RATE_LIMIT_METHOD = "account/rateLimits/read"
INTERNAL_QUOTA_EVENT = "__codex_quota_update__"
QUOTA_REFRESH_SEC = float(os.environ.get("CODEX_SCREEN_QUOTA_REFRESH_SEC", "180"))
MAX_REASONABLE_RESET_SEC = 366 * 24 * 60 * 60
QUOTA_THREAD_STOP_TIMEOUT_SEC = APP_SERVER_TIMEOUT_SEC + 2.0


def format_rate_limit_text(result: Dict[str, Any], now_epoch: Optional[float] = None) -> Optional[str]:
    """把 app-server 返回的 rate limit 结构压成屏幕上能显示的一行。"""

    snapshot = _pick_snapshot(result)
    if snapshot is None:
        return None

    label = _snapshot_label(snapshot)
    window = snapshot.get("primary") or snapshot.get("secondary")
    if isinstance(window, dict) and isinstance(window.get("usedPercent"), int):
        return _format_window(label, window, now_epoch)

    spend = snapshot.get("individualLimit")
    if isinstance(spend, dict) and isinstance(spend.get("remainingPercent"), int):
        used_percent = max(0, min(100, 100 - int(spend["remainingPercent"])))
        return _join_quota_parts(label, used_percent, spend.get("resetsAt"), now_epoch)

    credits = snapshot.get("credits")
    if isinstance(credits, dict) and credits.get("unlimited"):
        return f"{label} unlimited"
    return None


def parse_rate_limits_result(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析 JSON-RPC 响应；错误响应返回 None，让 daemon 自动降级。"""

    if "error" in message:
        return None
    result = message.get("result", message)
    return result if isinstance(result, dict) and "rateLimits" in result else None


def fetch_codex_quota_text(timeout_sec: float = APP_SERVER_TIMEOUT_SEC) -> Optional[str]:
    """查询 Codex app-server，成功时返回一行额度文本，失败时返回 None。"""

    state = fetch_codex_quota_state(timeout_sec)
    return state.get("quota_text") if state is not None else None


def fetch_codex_quota_state(timeout_sec: float = APP_SERVER_TIMEOUT_SEC) -> Optional[Dict[str, Any]]:
    """查询额度并保留固件需要的百分比和复位秒数。"""

    for exe_path in _iter_runnable_codex_paths():
        try:
            response = _query_rate_limits(exe_path, timeout_sec)
        except Exception:
            # WindowsApps 里的 codex.exe 可能可见但不可启动，继续试本地副本。
            continue
        result = parse_rate_limits_result(response or {})
        if result is not None:
            return format_rate_limit_state(result)
    return None


def start_quota_poller(
    target_queue: Any,
    stop_event: Any,
) -> Optional[threading.Thread]:
    """后台定时查额度；整个 daemon 生命周期复用一个 app-server。"""

    if not _quota_query_enabled():
        return None
    thread = threading.Thread(
        target=_poll_quota_loop,
        args=(target_queue, stop_event),
        name="codex-screen-quota",
        daemon=True,
    )
    thread.start()
    return thread


def stop_quota_poller(thread: Optional[threading.Thread]) -> None:
    """等待额度线程关闭 app-server，避免 daemon 退出时留下子进程。"""

    if thread is not None and thread.is_alive():
        thread.join(timeout=QUOTA_THREAD_STOP_TIMEOUT_SEC)


def is_quota_update_event(event: Dict[str, Any]) -> bool:
    return event.get("_internal_kind") == INTERNAL_QUOTA_EVENT


def _quota_query_enabled() -> bool:
    value = os.environ.get("CODEX_SCREEN_ENABLE_CODEX_QUOTA", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _poll_quota_loop(target_queue: Any, stop_event: Any) -> None:
    session = CodexQuotaSession()
    try:
        while not stop_event.is_set():
            quota_state = session.fetch_state()
            if quota_state is not None:
                target_queue.put(
                    {"_internal_kind": INTERNAL_QUOTA_EVENT, **quota_state}
                )
            stop_event.wait(QUOTA_REFRESH_SEC)
    finally:
        # daemon 退出时主动关闭 app-server，避免留下孤儿进程。
        session.close()


def format_rate_limit_state(
    result: Dict[str, Any],
    now_epoch: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """把额度结果转换为文本和 0-100 / 秒数格式。"""

    snapshot = _pick_snapshot(result)
    if snapshot is None:
        return None

    state: Dict[str, Any] = {
        "quota_text": format_rate_limit_text(result, now_epoch),
        "current_used_percent": 0xFF,
        "weekly_used_percent": 0xFF,
        "current_reset_sec": 0xFFFFFFFF,
        "weekly_reset_sec": 0xFFFFFFFF,
    }
    primary = snapshot.get("primary")
    secondary = snapshot.get("secondary")
    _apply_window_state(state, "current", primary, now_epoch)
    _apply_window_state(state, "weekly", secondary, now_epoch)

    spend = snapshot.get("individualLimit")
    if (
        state["current_used_percent"] == 0xFF
        and isinstance(spend, dict)
        and isinstance(spend.get("remainingPercent"), int)
    ):
        state["current_used_percent"] = max(
            0, min(100, 100 - int(spend["remainingPercent"]))
        )
        state["current_reset_sec"] = _remaining_reset_seconds(
            spend.get("resetsAt"), now_epoch
        )

    if state["quota_text"] is None:
        credits = snapshot.get("credits")
        if isinstance(credits, dict) and credits.get("unlimited"):
            state["quota_text"] = f"{_snapshot_label(snapshot)} unlimited"
        else:
            return None
    return state


def _pick_snapshot(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    buckets = result.get("rateLimitsByLimitId")
    if isinstance(buckets, dict):
        picked = _pick_named_bucket(buckets)
        if picked is not None:
            return picked

    snapshot = result.get("rateLimits")
    return snapshot if isinstance(snapshot, dict) else None


def _pick_named_bucket(buckets: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # 官方 multi-bucket 结构通常用 limit_id 区分，codex 桶最适合屏幕显示。
    for key in ("codex", "default"):
        value = buckets.get(key)
        if isinstance(value, dict):
            return value

    for value in buckets.values():
        if isinstance(value, dict) and value.get("limitId") == "codex":
            return value
    for value in buckets.values():
        if isinstance(value, dict):
            return value
    return None


def _snapshot_label(snapshot: Dict[str, Any]) -> str:
    return str(snapshot.get("limitName") or snapshot.get("limitId") or "Codex")


def _format_window(label: str, window: Dict[str, Any], now_epoch: Optional[float]) -> str:
    used_percent = max(0, min(100, int(window["usedPercent"])))
    return _join_quota_parts(label, used_percent, window.get("resetsAt"), now_epoch)


def _apply_window_state(
    state: Dict[str, Any],
    prefix: str,
    window: Any,
    now_epoch: Optional[float],
) -> None:
    if not isinstance(window, dict) or not isinstance(window.get("usedPercent"), int):
        return

    state[f"{prefix}_used_percent"] = max(
        0, min(100, int(window["usedPercent"]))
    )
    state[f"{prefix}_reset_sec"] = _remaining_reset_seconds(
        window.get("resetsAt"), now_epoch
    )


def _remaining_reset_seconds(
    resets_at: Any,
    now_epoch: Optional[float],
) -> int:
    if not isinstance(resets_at, (int, float)):
        return 0xFFFFFFFF

    now = time.time() if now_epoch is None else float(now_epoch)
    reset_epoch = float(resets_at)
    if reset_epoch > 10_000_000_000:
        reset_epoch /= 1000
    elif (
        reset_epoch > MAX_REASONABLE_RESET_SEC
        and reset_epoch / 1000 <= MAX_REASONABLE_RESET_SEC
        and reset_epoch > now + MAX_REASONABLE_RESET_SEC
    ):
        # 部分桌面版响应把窗口剩余时间以毫秒放在 resetsAt 中。
        # 只有当它明显不可能是当前 Unix 秒时间戳时才按持续时间兼容，
        # 避免误伤正常的绝对秒时间戳。
        return int(reset_epoch / 1000)
    return max(0, min(int(reset_epoch - now), 0xFFFFFFFE))


def _join_quota_parts(
    label: str,
    used_percent: int,
    resets_at: Any,
    now_epoch: Optional[float],
) -> str:
    text = f"{label} {used_percent}% used"
    reset_text = _format_reset_delta(resets_at, now_epoch)
    return f"{text} reset {reset_text}" if reset_text else text


def _format_reset_delta(resets_at: Any, now_epoch: Optional[float]) -> Optional[str]:
    remaining = _remaining_reset_seconds(resets_at, now_epoch)
    if remaining == 0xFFFFFFFF:
        return None

    hours, remainder = divmod(remaining, 3600)
    minutes = remainder // 60
    if hours >= 24:
        return f"{hours // 24}d"
    return f"{hours:02d}:{minutes:02d}"


def _iter_runnable_codex_paths() -> Iterable[str]:
    seen = set()
    for path in _candidate_codex_paths():
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if _looks_runnable(path):
            yield str(path)


def _candidate_codex_paths() -> Iterable[Path]:
    env_path = os.environ.get(CODEX_EXE_ENV)
    if env_path:
        yield Path(env_path)

    which_path = shutil.which("codex")
    if which_path:
        yield Path(which_path)

    codex_home = Path.home() / ".codex"
    yield codex_home / ".sandbox-bin" / "codex.exe"
    yield from codex_home.glob("zh-cn-patched/*/app/resources/codex.exe")


def _looks_runnable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


class CodexQuotaSession:
    """复用一个 app-server 进程的额度查询会话。

    额度刷新间隔通常是几分钟。如果每次刷新都重新启动 codex.exe，
    Windows 可能反复创建 conhost，造成终端闪现；长期会话只在首次
    查询时创建一次，协议超时或进程退出后才重连。
    """

    def __init__(self, timeout_sec: float = APP_SERVER_TIMEOUT_SEC) -> None:
        self.timeout_sec = timeout_sec
        self.process: Optional[subprocess.Popen[str]] = None
        self.output: Optional["queue.Queue[str]"] = None
        self.exe_path: Optional[str] = None
        self.next_request_id = 2

    def fetch_state(self) -> Optional[Dict[str, Any]]:
        """通过复用的 app-server 查询额度；失败后释放连接等待下次重连。"""

        if self.process is None and not self._connect():
            return None

        try:
            response = self._request_rate_limits()
            result = parse_rate_limits_result(response or {})
            return (
                format_rate_limit_state(result)
                if result is not None
                else None
            )
        except Exception:
            self.close()
            return None

    def close(self) -> None:
        """关闭当前连接；下一次查询会重新建立连接。"""

        process = self.process
        self.process = None
        self.output = None
        if process is not None:
            _stop_process(process)

    def _connect(self) -> bool:
        candidates = (
            [self.exe_path]
            if self.exe_path
            else list(_iter_runnable_codex_paths())
        )
        for exe_path in candidates:
            if not exe_path:
                continue
            try:
                process = _start_app_server(exe_path)
                output: "queue.Queue[str]" = queue.Queue()
                _start_reader(process.stdout, output)
                _start_reader(process.stderr, None)
                self.process = process
                self.output = output
                _send(process, _initialize_request())
                if self._read_response(1) is None:
                    self.close()
                    continue
                self.exe_path = exe_path
                self.next_request_id = 2
                return True
            except Exception:
                self.close()
        self.exe_path = None
        return False

    def _request_rate_limits(self) -> Optional[Dict[str, Any]]:
        request_id = self.next_request_id
        self.next_request_id += 1
        assert self.process is not None
        _send(
            self.process,
            {"id": request_id, "method": RATE_LIMIT_METHOD, "params": None},
        )
        return self._read_response(request_id)

    def _read_response(self, request_id: int) -> Optional[Dict[str, Any]]:
        if self.output is None:
            raise RuntimeError("app-server output unavailable")
        response = _read_response(
            self.output,
            request_id,
            time.time() + self.timeout_sec,
        )
        if response is None:
            raise TimeoutError(f"app-server response timeout: {request_id}")
        return response


def _query_rate_limits(exe_path: str, timeout_sec: float) -> Optional[Dict[str, Any]]:
    proc = _start_app_server(exe_path)
    output: "queue.Queue[str]" = queue.Queue()
    _start_reader(proc.stdout, output)
    _start_reader(proc.stderr, None)
    try:
        _send(proc, _initialize_request())
        _read_response(output, 1, time.time() + timeout_sec)
        _send(proc, {"id": 2, "method": RATE_LIMIT_METHOD, "params": None})
        return _read_response(output, 2, time.time() + timeout_sec)
    finally:
        _stop_process(proc)


def _start_app_server(exe_path: str) -> subprocess.Popen[str]:
    # daemon 会复用这个 Codex app-server，直到 daemon 停止或连接失败。
    # Windows 下 codex.exe 属于控制台程序；如果父进程是 pythonw.exe，
    # 不设置 CREATE_NO_WINDOW 时，系统可能为它创建一个可见的 conhost 窗口。
    process_options = _hidden_process_options()
    return subprocess.Popen(
        [exe_path, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **process_options,
    )


def _hidden_process_options() -> Dict[str, Any]:
    """返回跨平台的隐藏子进程参数。"""

    if os.name != "nt":
        return {}

    options: Dict[str, Any] = {
        "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    }
    startup_info_type = getattr(subprocess, "STARTUPINFO", None)
    if startup_info_type is None:
        return options

    startup_info = startup_info_type()
    startup_info.dwFlags |= int(
        getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    )
    startup_info.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
    options["startupinfo"] = startup_info
    return options


def _initialize_request() -> Dict[str, Any]:
    return {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "capabilities": {"experimentalApi": True},
        },
    }


def _send(proc: subprocess.Popen[str], message: Dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("app-server stdin unavailable")
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _start_reader(
    stream: Any,
    output: Optional["queue.Queue[str]"],
) -> None:
    if stream is None:
        return
    thread = threading.Thread(target=_read_lines, args=(stream, output), daemon=True)
    thread.start()


def _read_lines(stream: Any, output: Optional["queue.Queue[str]"]) -> None:
    for line in stream:
        if output is not None:
            output.put(line.rstrip("\n"))


def _read_response(
    output: "queue.Queue[str]",
    request_id: int,
    deadline: float,
) -> Optional[Dict[str, Any]]:
    while time.time() < deadline:
        try:
            line = output.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == request_id:
            return message
    return None


def _stop_process(proc: subprocess.Popen[str]) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        proc.kill()
