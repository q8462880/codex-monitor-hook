#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Hook 转发器。

职责很窄：
1. 只读取 Codex hook 的 stdin 事件
2. 只转发到 127.0.0.1:12688
3. 连接失败时后台脱离拉起 daemon

这个脚本不导入任何 HID / USB 相关库。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

from codex_screen_log import log_line


# ===== 配置区：只保留本地转发相关参数 =====
BASE_DIR = Path.home() / ".codex_screen"
DAEMON_EXE = BASE_DIR / "codex_screen_daemon.exe"
DAEMON_SCRIPT = BASE_DIR / "codex_screen_daemon.py"
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 12688
CONNECT_TIMEOUT_SEC = 0.6
RETRY_AFTER_SPAWN_SEC = 0.8
MAX_RETRY_COUNT = 4
RELAY_LOCK_FILE = BASE_DIR / "codex_hook_relay.lock"
RELAY_LOCK_WAIT_SEC = 4.0
RELAY_LOCK_RETRY_SEC = 0.05


def _short_id(value: Any) -> str:
    """日志里只放短 ID，避免把完整 prompt 或敏感内容写进文件。"""

    text = str(value or "-").replace("\r", " ").replace("\n", " ").strip()
    return text[:24] if text else "-"


def _read_hook_event() -> Dict[str, Any]:
    """读取已经到达的 Codex hook JSON，不能等待 stdin 完全关闭。"""

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
    if not raw.strip():
        return {"hook_event_name": "UNKNOWN", "_raw": ""}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"hook_event_name": "UNKNOWN", "_raw": raw}

    if isinstance(data, dict):
        return data
    return {"hook_event_name": "UNKNOWN", "_raw": raw}


def _spawn_detached_daemon() -> bool:
    """跨平台后台脱离启动 daemon。"""

    args = _daemon_command()
    if not args:
        log_line(
            "relay",
            f"daemon executable missing: {DAEMON_EXE}; script missing: {DAEMON_SCRIPT}",
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
        # CREATE_NO_WINDOW 隐藏 daemon 的控制台；relay 自身则由配置中的
        # pythonw.exe 启动，避免 Codex 桌面端执行 hook 时闪出黑色窗口。
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(args, **kwargs)
    log_line("relay", f"spawned daemon: {args[0]}")
    return True


def _daemon_command() -> list[str]:
    """返回 daemon 启动命令；普通用户安装包优先使用 exe。"""

    # PyInstaller 打包后 relay 自己也是 exe，sys.executable 指向 relay。
    # 因此不能再用 sys.executable 去运行 daemon.py，必须启动同目录的 daemon exe。
    if os.name == "nt" and DAEMON_EXE.exists():
        return [str(DAEMON_EXE), "--daemon"]

    # 这个分支只服务仓库内开发和测试，普通用户安装包不依赖 Python。
    if DAEMON_SCRIPT.exists():
        python_bin = sys.executable or "python"
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


def _send_packet(packet: Dict[str, Any]) -> bool:
    """把事件发送到 daemon。"""

    payload = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload += b"\n"

    try:
        with socket.create_connection((DAEMON_HOST, DAEMON_PORT), timeout=CONNECT_TIMEOUT_SEC) as conn:
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
                f" elapsed={elapsed_ms}",
            )
            return True
    except OSError as exc:
        log_line("relay", f"send failed: {exc}")
        return False


def main() -> int:
    """hook 入口。"""

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    event = _read_hook_event()
    log_line("relay", f"hook event={event.get('hook_event_name') or 'UNKNOWN'} session={_short_id(event.get('session_id'))} turn={_short_id(event.get('turn_id'))}")
    packet = {
        "kind": "codex_hook_event",
        "sent_at": time.time(),
        "event": event,
    }

    with _relay_instance_lock() as acquired:
        if not acquired:
            log_line("relay", "relay instance lock timeout; event dropped")
            return 0

        for attempt in range(MAX_RETRY_COUNT):
            if _send_packet(packet):
                return 0

            if attempt == 0:
                _spawn_detached_daemon()

            time.sleep(RETRY_AFTER_SPAWN_SEC)

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
