#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Hook 转发器。

职责很窄：
1. 只读取 Codex hook 的 stdin 事件
2. 只转发到 daemon 当前监听的本地 TCP 端口
3. 连接失败时用同一个 Python 解释器后台拉起 daemon 脚本

这个脚本不导入任何 HID / USB 相关库。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import argparse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from codex_screen_log import log_line
from codex_quota_client import (
    INTERNAL_DAEMON_SHUTDOWN_EVENT,
    INTERNAL_QUOTA_REFRESH_EVENT,
)
from codex_runtime_config import DEFAULT_HOST, relay_candidate_ports
from codex_relay_state import (
    RelaySessionState,
    load_state,
    save_state,
)


# ===== 配置区：只保留本地转发相关参数 =====
BASE_DIR = Path.home() / ".codex_screen"
DAEMON_SCRIPT = BASE_DIR / "codex_screen_daemon.py"
DAEMON_HOST = DEFAULT_HOST
CONNECT_TIMEOUT_SEC = 0.6
DAEMON_PROBE_TIMEOUT_SEC = 0.2
RETRY_AFTER_SPAWN_SEC = 0.8
MAX_RETRY_COUNT = 4
RELAY_LOCK_FILE = BASE_DIR / "codex_hook_relay.lock"
RELAY_STATE_FILE = BASE_DIR / "codex_relay_state.json"
RELAY_LOCK_WAIT_SEC = 4.0
RELAY_LOCK_RETRY_SEC = 0.05
STATUS_HOOK_FORWARDING_ENABLED = os.environ.get(
    "CODEX_SCREEN_ENABLE_STATUS_HOOKS",
    "0",
).strip().lower() in {"1", "true", "yes", "on"}
QUOTA_HOOK_EVENTS = {"SessionStart", "UserPromptSubmit", "SessionEnd"}


def _short_id(value: Any) -> str:
    """日志里只放短 ID，避免把完整 prompt 或敏感内容写进文件。"""

    text = str(value or "-").replace("\r", " ").replace("\n", " ").strip()
    return text[:24] if text else "-"


def _read_hook_event(extra_args: Optional[List[str]] = None) -> Dict[str, Any]:
    """读取 Codex hook JSON，兼容 stdin 与 Desktop 前置的位置参数。"""

    # Codex 会先写入 JSON，再等待 Hook 进程退出。
    # read() 会等待 EOF，readline() 又依赖输入中一定有换行。
    # BufferedReader.read1() 只读取当前已经到达的数据，适合 Windows
    # 管道，也能避免 pythonw 与 Codex Hook 互相等待导致超时。
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    if hasattr(stream, "read1"):
        raw_data = stream.read1(64 * 1024)
    else:
        raw_data = stream.readline()
    raw = (
        raw_data.decode("utf-8", errors="replace")
        if isinstance(raw_data, bytes)
        else raw_data
    )
    event = _parse_event_json(raw)
    if event is not None:
        return event

    # Python bootstrap 会把被 Windows runner 前置的事件 JSON 作为 relay
    # 参数传入；非 Windows 或未改写的 runner 仍可从 stdin 读取。
    for value in extra_args or []:
        event = _parse_event_json(value)
        if event is not None:
            return event
    return {"hook_event_name": "UNKNOWN", "_raw": raw}


def _parse_event_json(value: Any) -> Optional[Dict[str, Any]]:
    """把单个输入值解析为 hook 事件字典，非 JSON 输入返回 None。"""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _spawn_detached_daemon() -> bool:
    """跨平台后台脱离启动 daemon。"""

    args = _daemon_command()
    if not args:
        log_line(
            "relay",
            f"daemon script missing: {DAEMON_SCRIPT}",
        )
        return False

    kwargs: Dict[str, Any] = {
        "cwd": str(BASE_DIR),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }

    if os.name == "nt":
        flags = 0
        # 让后台 daemon 继承 pythonw 的无控制台行为；即使用户手动用
        # python.exe 启动 relay，也尽量不创建新的黑色窗口。
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(args, **kwargs)
    log_line("relay", f"spawned daemon: {args[0]}")
    return True


def _daemon_command() -> list[str]:
    """使用当前 relay 所在的 Python 解释器启动 daemon 脚本。"""

    if DAEMON_SCRIPT.exists():
        python_bin = sys.executable or "python3"
        if os.name == "nt":
            # 即使 relay 被 python.exe 手动启动，也优先切到同目录的
            # pythonw.exe，避免后台 daemon 创建控制台窗口。
            sibling_pythonw = Path(python_bin).with_name("pythonw.exe")
            if sibling_pythonw.exists():
                python_bin = str(sibling_pythonw)
        return [python_bin, str(DAEMON_SCRIPT), "--daemon"]

    return []


def _try_lock(lock_handle: Any) -> bool:
    """尝试获取跨进程锁，兼容 Windows Hook 和 Unix 开发环境。"""

    deadline = time.monotonic() + RELAY_LOCK_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            if os.name == "nt":
                import msvcrt

                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (ImportError, OSError):
            time.sleep(RELAY_LOCK_RETRY_SEC)
    return False


def _unlock(lock_handle: Any) -> None:
    """释放 relay 锁；进程异常退出时由操作系统兜底释放。"""

    try:
        if os.name == "nt":
            import msvcrt

            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


@contextmanager
def _relay_instance_lock() -> Iterator[bool]:
    """串行化所有 Hook relay，避免新旧 relay 同时触发 daemon 启动。"""

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with RELAY_LOCK_FILE.open("a+b") as lock_handle:
        if lock_handle.seek(0, os.SEEK_END) == 0:
            lock_handle.write(b"0")
            lock_handle.flush()
        acquired = _try_lock(lock_handle)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock(lock_handle)


def _send_packet(packet: Dict[str, Any], port: int) -> bool:
    """把事件发送到指定端口的 daemon。"""

    payload = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload += b"\n"

    try:
        with socket.create_connection((DAEMON_HOST, port), timeout=CONNECT_TIMEOUT_SEC) as conn:
            conn.settimeout(CONNECT_TIMEOUT_SEC)
            conn.sendall(payload)
            try:
                conn.recv(256)
            except OSError:
                pass
            event = packet.get("event") or {}
            elapsed_ms = _elapsed_ms(packet.get("sent_at"))
            log_line(
                "relay",
                f"sent event={event.get('hook_event_name') or 'UNKNOWN'}"
                f" port={port} elapsed={elapsed_ms}",
            )
            return True
    except OSError as exc:
        log_line("relay", f"send failed port={port}: {exc}")
        return False


def _preferred_relay_port() -> Optional[int]:
    """返回 runtime.json 指向的端口，缺失时才使用首选候选端口。"""

    ports = relay_candidate_ports(BASE_DIR)
    return ports[0] if ports else None


def _send_with_retries(packet: Dict[str, Any]) -> bool:
    """优先投递活动端口，冷启动时立即拉起 daemon 后再重试。"""

    port = _preferred_relay_port()
    if port is not None and _send_packet(packet, port):
        return True

    # runtime.json 是 daemon 成功监听后写入的唯一真实端口来源。daemon 未运行
    # 时逐个连接历史候选端口会耗尽 Codex 5 秒 Hook 超时，因此首个失败后立即
    # 拉起 daemon，再等它写入新的 runtime.json。
    _spawn_detached_daemon()
    for _ in range(MAX_RETRY_COUNT):
        time.sleep(RETRY_AFTER_SPAWN_SEC)
        port = _preferred_relay_port()
        if port is not None and _send_packet(packet, port):
            return True
    return False


def _make_packet(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "codex_hook_event",
        "sent_at": time.time(),
        "event": event,
    }


def _daemon_is_reachable() -> bool:
    """探测已有 daemon，避免每个 SessionStart 都重复创建进程。"""

    port = _preferred_relay_port()
    if port is None:
        return False
    try:
        with socket.create_connection(
            (DAEMON_HOST, port),
            timeout=DAEMON_PROBE_TIMEOUT_SEC,
        ) as conn:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        return True
    except OSError:
        return False


def _ensure_daemon_started(event_name: str) -> None:
    """SessionStart 即使不转发状态，也必须先启动 HID daemon。"""

    if event_name != "SessionStart" or _daemon_is_reachable():
        return
    if not _spawn_detached_daemon():
        log_line("relay", "daemon start requested by SessionStart failed")


def _handle_hook_event(event: Dict[str, Any]) -> bool:
    """旧版状态 Hook 转发逻辑，保留供未来恢复完整状态显示。"""

    with _relay_instance_lock() as acquired:
        if not acquired:
            log_line("relay", "relay instance lock timeout; event dropped")
            return False

        state = load_state(RELAY_STATE_FILE)
        result = state.apply_event(event)
        save_state(RELAY_STATE_FILE, state)

        if not result.accepted:
            log_line(
                "relay",
                f"cached event ignored={event.get('hook_event_name') or 'UNKNOWN'}"
                f" session={_short_id(result.session_id)} reason={result.reason}",
            )
            return False

        if event.get("hook_event_name") == "UserPromptSubmit":
            log_line(
                "relay",
                f"active session auto-selected={_short_id(result.session_id)}",
            )

        event_name = str(event.get("hook_event_name") or "UNKNOWN")
        if not state.should_forward(result.session_id):
            _ensure_daemon_started(event_name)
            log_line(
                "relay",
                f"cached event={event_name}"
                f" session={_short_id(result.session_id)}"
                f" active={_short_id(state.active_session_id)}"
                f" status={result.status}"
                " forwarded=no reason=inactive_session",
            )
            return False

        return _send_with_retries(_make_packet(event))


def _handle_quota_hook_event(event: Dict[str, Any]) -> bool:
    """只把额度刷新请求发送给 daemon，不读取或转发 session 状态。"""

    event_name = str(event.get("hook_event_name") or "UNKNOWN")
    if event_name not in QUOTA_HOOK_EVENTS:
        log_line("relay", f"quota hook ignored event={event_name}")
        return False

    internal_kind = (
        INTERNAL_DAEMON_SHUTDOWN_EVENT
        if event_name == "SessionEnd"
        else INTERNAL_QUOTA_REFRESH_EVENT
    )
    refresh_event: Dict[str, Any] = {
        "_internal_kind": internal_kind,
        "hook_event_name": event_name,
    }
    for key in ("quota", "quota_text", "budget"):
        if key in event:
            refresh_event[key] = event[key]
    return _send_with_retries(_make_packet(refresh_event))


def _dispatch_hook_event(event: Dict[str, Any]) -> bool:
    """选择当前功能模式；完整状态分支保留但默认不启用。"""

    if STATUS_HOOK_FORWARDING_ENABLED:
        return _handle_hook_event(event)
    # 当前只需要额度，避免把状态 Hook 信号写入 relay 缓存和 HID。
    return _handle_quota_hook_event(event)


def _handle_active_session_command(
    set_active_session: Optional[str],
    clear_active_session: bool,
    show_active_session: bool,
) -> int:
    """处理桌面集成或测试工具显式传入的 active session。"""

    with _relay_instance_lock() as acquired:
        if not acquired:
            log_line("relay", "relay instance lock timeout; control dropped")
            return 0

        state = load_state(RELAY_STATE_FILE)
        if clear_active_session:
            state.clear_active_session()
            save_state(RELAY_STATE_FILE, state)
            log_line("relay", "active session cleared; hook events stay cached")
            return 0

        if set_active_session is not None:
            cache = state.set_active_session(set_active_session)
            save_state(RELAY_STATE_FILE, state)
            if cache is None:
                log_line(
                    "relay",
                    f"active session={_short_id(set_active_session)} state=UNKNOWN replay=none",
                )
                return 0

            replay_events = state.replay_events(set_active_session)
            if not replay_events:
                log_line(
                    "relay",
                    f"active session={_short_id(set_active_session)}"
                    f" state={cache.status} replay=none",
                )
                return 0

            for event in replay_events:
                if not _send_with_retries(_make_packet(event)):
                    return 0
            log_line(
                "relay",
                f"active session={_short_id(set_active_session)}"
                f" state={cache.status} replayed={len(replay_events)}",
            )
            return 0

        if show_active_session:
            print(
                json.dumps(
                    {
                        "active_session_id": state.active_session_id,
                        "status": (
                            state.status_for(state.active_session_id)
                            if state.active_session_id
                            else "UNKNOWN"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

    return 0


def _parse_args(argv: Optional[List[str]]) -> tuple[argparse.Namespace, List[str]]:
    parser = argparse.ArgumentParser(description="Codex Hook 本地 relay")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set-active-session", metavar="SESSION_ID")
    group.add_argument("--clear-active-session", action="store_true")
    group.add_argument("--show-active-session", action="store_true")
    args, extras = parser.parse_known_args(argv)
    return args, extras


def main(argv: Optional[List[str]] = None) -> int:
    """Hook 入口；UserPromptSubmit 会把最近用户操作的 session 设为 active。"""

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    # Windows runner 可能把事件 JSON 放到 argv[0]，因此 hook 模式检查
    # 完整 argv；relay 脚本路径会被 JSON 解析器自动忽略。
    input_args = list(sys.argv) if argv is None else argv
    args, extras = _parse_args(input_args)
    if (
        args.set_active_session is not None
        or args.clear_active_session
        or args.show_active_session
    ):
        return _handle_active_session_command(
            args.set_active_session,
            args.clear_active_session,
            args.show_active_session,
        )

    event = _read_hook_event(extras)
    log_line(
        "relay",
        f"hook event={event.get('hook_event_name') or 'UNKNOWN'}"
        f" session={_short_id(event.get('session_id'))}"
        f" turn={_short_id(event.get('turn_id'))}",
    )
    _dispatch_hook_event(event)
    return 0


def _elapsed_ms(started_at: Any) -> str:
    """返回 relay 开始处理到当前时刻的耗时。"""

    try:
        elapsed = max(0.0, (time.time() - float(started_at)) * 1000)
    except (TypeError, ValueError):
        return "unknown"
    return f"{elapsed:.1f}ms"


if __name__ == "__main__":
    raise SystemExit(main())
