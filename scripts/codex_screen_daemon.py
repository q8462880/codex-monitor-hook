#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, os, queue, signal, socket, sys, threading, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from codex_screen_log import log_line
from codex_quota_client import (
    is_quota_update_event,
    start_quota_poller,
    stop_quota_poller,
)
from codex_state_manager import (
    EVENT_STATE_MAP,
    STATE_COMPACTING,
    STATE_EXECUTING,
    STATE_IDLE,
    STATE_SUBAGENT,
    STATE_THINKING,
    STATE_WAIT_PERM,
    CodexStateManager,
)
# ===== 配置区 =====
BASE_DIR = Path.home() / ".codex_screen"
HOST = "127.0.0.1"
PORT = 12688
HID_VENDOR_ID = 0x1CA9
HID_PRODUCT_ID = 0x1305
HID_USAGE_PAGE = 0xFF00
HID_USAGE = 0x01
HID_REPORT_SIZE = 1024
HID_REPORT_ID = 0x00
# 固件的 Output Report 通过 hidapi 需要额外带 1 字节 report_id=0，
# 因此 _render_frame() 返回 1024 字节协议报文，_write_frame() 再在前面补 0。
HID_PROTOCOL_READY = True
SCREEN_HID_PROTOCOL_VERSION = 0x01
SCREEN_HID_CMD_CODEX_MONITOR = 0x24
SCREEN_HID_CODEX_SUBCMD_STATE = 0x01
SCREEN_HID_CODEX_FLAG_STATUS_VALID = 1 << 0
SCREEN_HID_CODEX_FLAG_CURRENT_VALID = 1 << 1
SCREEN_HID_CODEX_FLAG_WEEKLY_VALID = 1 << 2
SCREEN_HID_CODEX_FLAG_CURRENT_RESET_VALID = 1 << 3
SCREEN_HID_CODEX_FLAG_WEEKLY_RESET_VALID = 1 << 4
SCREEN_HID_CODEX_FLAG_DEGRADED = 1 << 5
SCREEN_HID_CODEX_PERCENT_INVALID = 0xFF
SCREEN_HID_CODEX_RESET_INVALID = 0xFFFFFFFF
SCREEN_HID_CODEX_STATUS_IDLE = 0x00
SCREEN_HID_CODEX_STATUS_THINKING = 0x01
SCREEN_HID_CODEX_STATUS_EXECUTING = 0x02
SCREEN_HID_CODEX_STATUS_WAIT_PERM = 0x03
SCREEN_HID_CODEX_STATUS_COMPACTING = 0x04
SCREEN_HID_CODEX_STATUS_SUBAGENT = 0x05
SCREEN_HID_CODEX_STATUS_OFFLINE = 0xE0
SCREEN_HID_CODEX_STATUS_ERROR = 0xE1
IDLE_TIMEOUT_SEC = 600
ACCEPT_TIMEOUT_SEC = 1.0
DEVICE_POLL_TIMEOUT_MS = 50
HID_HEARTBEAT_INTERVAL_SEC = 5.0
QUEUE_POLL_INTERVAL_SEC = 0.02
RECONNECT_BACKOFF_MIN_SEC = 0.5
RECONNECT_BACKOFF_MAX_SEC = 30.0
HID_FAILURE_LOG_INTERVAL_SEC = 60.0
FRAME_SEQ_RESERVATION = 1_000_000
FRAME_SEQ_STATE_FILE = BASE_DIR / "codex_monitor_frame_seq"
DAEMON_LOCK_FILE = BASE_DIR / "codex_screen_daemon.lock"
DEFAULT_QUOTA_TEXT = os.environ.get("CODEX_SCREEN_QUOTA_TEXT", "quota: --")
QUEUE_MAX_ITEMS = 256
STATUS_CODE_MAP = {
    STATE_IDLE: SCREEN_HID_CODEX_STATUS_IDLE,
    STATE_THINKING: SCREEN_HID_CODEX_STATUS_THINKING,
    STATE_EXECUTING: SCREEN_HID_CODEX_STATUS_EXECUTING,
    STATE_WAIT_PERM: SCREEN_HID_CODEX_STATUS_WAIT_PERM,
    STATE_COMPACTING: SCREEN_HID_CODEX_STATUS_COMPACTING,
    STATE_SUBAGENT: SCREEN_HID_CODEX_STATUS_SUBAGENT,
}
ICON_CODE_MAP = {
    STATE_IDLE: 0x00,
    STATE_THINKING: 0x01,
    STATE_EXECUTING: 0x02,
    STATE_WAIT_PERM: 0x03,
    STATE_COMPACTING: 0x04,
    STATE_SUBAGENT: 0x05,
}


class BoundedStateQueue(queue.Queue):
    """有界状态队列，满了就丢最旧事件，避免极端 hook 风暴吃内存。

    屏幕显示只关心最新状态。保留最新事件比让队列无限增长更重要；
    队列正常很快会被设备线程 drain，这个保护只在异常高频事件下触发。
    """

    def put(self, item: Any, block: bool = True, timeout: Optional[float] = None) -> None:
        while True:
            try:
                super().put(item, block=False)
                return
            except queue.Full:
                try:
                    super().get_nowait()
                    super().task_done()
                except queue.Empty:
                    return

def _short(text: Any, limit: int) -> str:
    if text is None: return "-"
    value = str(text).replace("\r", " ").replace("\n", " ").strip()
    if not value: return "-"
    data = value.encode("utf-8")
    return value if len(data) <= limit else data[:limit].decode("utf-8", errors="ignore").rstrip()
def _put_u32_le(frame: bytearray, offset: int, value: int) -> None:
    frame[offset : offset + 4] = int(value).to_bytes(4, "little", signed=False)


def _normalize_percent(value: Any) -> int:
    try:
        percent = int(value)
    except (TypeError, ValueError):
        return SCREEN_HID_CODEX_PERCENT_INVALID
    return max(0, min(100, percent))


def _normalize_reset_sec(value: Any) -> int:
    try:
        reset_value = float(value)
    except (TypeError, ValueError):
        return SCREEN_HID_CODEX_RESET_INVALID

    if reset_value > 10_000_000_000:
        reset_value /= 1000
    if reset_value > 1_000_000_000:
        reset_value -= time.time()
    return max(0, min(int(reset_value), SCREEN_HID_CODEX_RESET_INVALID - 1))


def _lock_file(handle: Any) -> bool:
    """以非阻塞方式锁住文件的第一个字节，跨平台防止状态竞争。"""

    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, OSError):
        return False


def _unlock_file(handle: Any) -> None:
    """释放文件锁；进程异常退出时由操作系统自动释放。"""

    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


@contextmanager
def _daemon_instance_lock(lock_path: Optional[Path] = None):
    """保证同一台电脑上只有一个 daemon 可以持有 HID 和监听端口。"""

    path = lock_path or (BASE_DIR / DAEMON_LOCK_FILE.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        acquired = _lock_file(handle)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock_file(handle)


def _reserve_frame_seq() -> int:
    """为 daemon 进程预留单调递增的序号区间。

    固件会丢弃旧 frame_seq。daemon 重启时如果从 1 重新开始，
    即使 HID 写入成功，固件也会持续忽略这些心跳并显示 offline。
    这里只在进程启动时写一次状态文件，不增加每帧的磁盘写入。
    """

    lock_path = FRAME_SEQ_STATE_FILE.with_name(
        FRAME_SEQ_STATE_FILE.name + ".lock"
    )
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with _daemon_instance_lock(lock_path) as acquired:
        if not acquired:
            return max(1, int(time.time()))

        now = int(time.time())
        previous = 0
        try:
            previous = int(FRAME_SEQ_STATE_FILE.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pass

        seed = max(now, previous)
        next_seed = seed + FRAME_SEQ_RESERVATION
        temp_file = FRAME_SEQ_STATE_FILE.with_suffix(".tmp")
        try:
            temp_file.write_text(str(next_seed), encoding="ascii")
            os.replace(temp_file, FRAME_SEQ_STATE_FILE)
        except OSError:
            try:
                temp_file.unlink()
            except OSError:
                pass

        return max(1, seed)


class CodexScreenDaemon:
    def __init__(self) -> None:
        self.state_manager = CodexStateManager()
        self.state = {
            "status": STATE_IDLE,
            "quota_text": DEFAULT_QUOTA_TEXT,
            "session_id": "-",
            "turn_id": "-",
            "permission_mode": "-",
            "source": "-",
            "tool_name": "-",
            "prompt": "-",
            "last_event": "INIT",
            "updated_at": time.time(),
            "raw_event": {},
            "frame_seq": _reserve_frame_seq(),
            "current_used_percent": SCREEN_HID_CODEX_PERCENT_INVALID,
            "weekly_used_percent": SCREEN_HID_CODEX_PERCENT_INVALID,
            "current_reset_sec": SCREEN_HID_CODEX_RESET_INVALID,
            "weekly_reset_sec": SCREEN_HID_CODEX_RESET_INVALID,
            "session_active": False,
            "turn_active": False,
        }
        self.queue: "queue.Queue[Dict[str, Any]]" = BoundedStateQueue(QUEUE_MAX_ITEMS)
        self.stop = threading.Event()
        self.last_activity = time.monotonic()
        self.dev = None
        self.hid = None
        self.server: Optional[socket.socket] = None
        self.last_frame: Optional[bytes] = None
        self.last_frame_written_at = 0.0
        self.lock = threading.Lock()
        self.last_hid_open_log_at = 0.0
        self.last_hid_failure_log_at = 0.0
        self.hid_failure_count = 0
        self.hid_disabled_logged = False
        self.quota_thread: Optional[threading.Thread] = None

    def run(self) -> int:
        with _daemon_instance_lock() as acquired:
            if not acquired:
                log_line("daemon", "daemon instance lock busy; exit")
                return 0
            try: self.server = self._bind()
            except OSError as exc:
                log_line("daemon", f"port {HOST}:{PORT} busy: {exc}")
                return 0
            self._install_signals()
            threading.Thread(target=self._device_loop, name="codex-screen-device", daemon=True).start()
            threading.Thread(target=self._idle_watchdog, name="codex-screen-watchdog", daemon=True).start()
            self.quota_thread = start_quota_poller(self.queue, self.stop)
            try: self._accept_loop()
            finally:
                self.stop.set()
                stop_quota_poller(self.quota_thread)
                self._close_device()
                self._close_server()
            return 0

    def _bind(self) -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"): server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server.bind((HOST, PORT))
        server.listen(4)
        server.settimeout(ACCEPT_TIMEOUT_SEC)
        return server

    def _install_signals(self) -> None:
        def handler(*_: Any) -> None: self.stop.set()
        for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if signum is None: continue
            try: signal.signal(signum, handler)
            except Exception: pass

    def _accept_loop(self) -> None:
        assert self.server is not None
        while not self.stop.is_set():
            try: conn, _addr = self.server.accept()
            except socket.timeout: continue
            except OSError: break
            with conn:
                conn.settimeout(1.0)
                raw = self._read_conn(conn)
                if not raw: continue
                packet = self._parse_packet(raw)
                latency = _elapsed_ms(packet.get("_relay_sent_at"))
                log_line(
                    "daemon",
                    f"received event={packet.get('hook_event_name') or 'UNKNOWN'}"
                    f" latency={latency}",
                )
                self.queue.put(packet)
                self._touch()
                self._ack(conn, packet)

    def _read_conn(self, conn: socket.socket) -> bytes:
        chunks: List[bytes] = []
        while True:
            try: chunk = conn.recv(4096)
            except socket.timeout: break
            if not chunk: break
            chunks.append(chunk)
            if b"\n" in chunk: break
        raw = b"".join(chunks).strip()
        return raw.splitlines()[-1].strip() if b"\n" in raw else raw

    def _parse_packet(self, raw: bytes) -> Dict[str, Any]:
        try: data = json.loads(raw.decode("utf-8"))
        except Exception: return {"hook_event_name": "UNKNOWN", "_raw": raw.decode("utf-8", "replace")}
        if (
            isinstance(data, dict)
            and data.get("kind") == "codex_hook_event"
            and isinstance(data.get("event"), dict)
        ):
            event = dict(data["event"])
            event["_relay_sent_at"] = data.get("sent_at")
            return event
        return data if isinstance(data, dict) else {"hook_event_name": "UNKNOWN", "_raw": str(data)}

    def _ack(self, conn: socket.socket, packet: Dict[str, Any]) -> None:
        reply = {"ok": True, "state": self.state["status"], "event": packet.get("hook_event_name") or "UNKNOWN"}
        try: conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError: pass

    def _device_loop(self) -> None:
        backoff = RECONNECT_BACKOFF_MIN_SEC
        while not self.stop.is_set():
            if not HID_PROTOCOL_READY:
                if not self.hid_disabled_logged:
                    log_line(
                        "daemon",
                        "HID output disabled: firmware protocol is not ready",
                    )
                    self.hid_disabled_logged = True
                self._drain_queue()
                time.sleep(QUEUE_POLL_INTERVAL_SEC)
                continue

            if self.dev is None:
                try:
                    if not self._open_device():
                        time.sleep(backoff)
                        backoff = min(backoff * 1.5, RECONNECT_BACKOFF_MAX_SEC)
                        continue
                    self._write_frame()
                    if self.dev is None:
                        time.sleep(backoff)
                        backoff = min(backoff * 1.5, RECONNECT_BACKOFF_MAX_SEC)
                        continue
                    backoff = RECONNECT_BACKOFF_MIN_SEC
                except RuntimeError as exc:
                    log_line("daemon", str(exc))
                    self.stop.set()
                    return
            dirty = self._drain_queue()
            if dirty:
                self._write_frame()
            elif self._heartbeat_due():
                self._refresh_heartbeat()
                self._write_frame(force=True)
            if self.dev is None:
                time.sleep(backoff); backoff = min(backoff * 1.5, RECONNECT_BACKOFF_MAX_SEC); continue
            self._poll_input()
            if self.dev is None: time.sleep(backoff); backoff = min(backoff * 1.5, RECONNECT_BACKOFF_MAX_SEC)

    def _drain_queue(self) -> bool:
        """按入队顺序处理事件，返回是否需要刷新屏幕。"""

        dirty = False
        while True:
            try:
                packet = self.queue.get_nowait()
            except queue.Empty:
                break
            self._apply_event(packet)
            self._touch()
            dirty = True
        return dirty

    def _apply_event(self, event: Dict[str, Any]) -> None:
        if is_quota_update_event(event):
            self._apply_quota_fields(event)
            self.state["quota_text"] = _short(event.get("quota_text"), 96)
            self.state["updated_at"] = time.time()
            self._advance_frame_seq()
            log_line("daemon", f"quota updated: {self.state['quota_text']}")
            return

        name = str(event.get("hook_event_name") or event.get("event") or "UNKNOWN")
        session_id = event.get("session_id")
        turn_id = event.get("turn_id")
        if not self.state_manager.apply_event(name, session_id, turn_id):
            log_line(
                "daemon",
                f"ignored stale event={name}"
                f" session={_short(session_id, 24)}"
                f" turn={_short(turn_id, 24)}",
            )
            return

        self.state["status"] = self.state_manager.status
        self.state["last_event"] = self.state_manager.last_event
        self.state["updated_at"] = time.time()
        self.state["raw_event"] = event
        self.state["session_id"] = _short(
            self.state_manager.active_session_id,
            64,
        )
        self.state["turn_id"] = _short(
            self.state_manager.last_turn_id,
            64,
        )
        self.state["session_active"] = self.state_manager.session_active
        self.state["turn_active"] = self.state_manager.turn_active
        self.state["permission_mode"] = _short(event.get("permission_mode") or self.state["permission_mode"], 32)
        self.state["source"] = _short(event.get("source") or self.state["source"], 32)
        self.state["tool_name"] = _short(event.get("tool_name") or self.state["tool_name"], 64)
        if "prompt" in event: self.state["prompt"] = _short(event.get("prompt"), 128)
        elif "last_assistant_message" in event: self.state["prompt"] = _short(event.get("last_assistant_message"), 128)
        quota = event.get("quota_text")
        if quota is None and isinstance(event.get("quota"), dict):
            parts = [f"{k}={event['quota'].get(k)}" for k in ("used", "remaining", "limit", "percent") if event["quota"].get(k) is not None]
            quota = " ".join(parts) if parts else None
        if quota is None and event.get("budget") is not None: quota = f"budget={event.get('budget')}"
        if quota is not None: self.state["quota_text"] = _short(quota, 96)
        self._apply_quota_fields(event)
        self._advance_frame_seq()
        latency = _elapsed_ms(event.get("_relay_sent_at"))
        log_line(
            "daemon",
            f"state={self.state['status']} event={name} latency={latency}",
        )

    def _apply_quota_fields(self, event: Dict[str, Any]) -> None:
        quota = event.get("quota")
        source = quota if isinstance(quota, dict) else event
        for key, state_key in (
            ("current_used_percent", "current_used_percent"),
            ("weekly_used_percent", "weekly_used_percent"),
            ("current_reset_sec", "current_reset_sec"),
            ("weekly_reset_sec", "weekly_reset_sec"),
        ):
            if key not in source:
                continue
            value = source[key]
            self.state[state_key] = (
                _normalize_percent(value)
                if "percent" in key
                else _normalize_reset_sec(value)
            )

    def _advance_frame_seq(self) -> None:
        self.state["frame_seq"] = (self.state["frame_seq"] + 1) & 0xFFFFFFFF
        if self.state["frame_seq"] == 0:
            self.state["frame_seq"] = 1

    def _heartbeat_due(self, now: Optional[float] = None) -> bool:
        """判断是否需要发送在线心跳，避免固件误判 HID 链路断开。"""

        current = time.monotonic() if now is None else now
        return (
            self.last_frame_written_at <= 0.0
            or current - self.last_frame_written_at >= HID_HEARTBEAT_INTERVAL_SEC
        )

    def _refresh_heartbeat(self, now: Optional[float] = None) -> None:
        """刷新帧序号后再发心跳，确保固件不会按旧序号丢弃。"""

        self._advance_frame_seq()
        self.state["updated_at"] = time.time()
        if now is not None:
            self.last_frame_written_at = now

    def _ensure_hid(self):
        if self.hid is not None:
            return self.hid
        try:
            import hid  # type: ignore
        except Exception as exc:
            if getattr(sys, "frozen", False):
                raise RuntimeError("打包程序缺少 HID 运行组件，请重新下载安装包") from exc
            raise RuntimeError("hidapi 未安装，请先执行 python -m pip install hidapi") from exc
        self.hid = hid
        return hid

    def _open_device(self) -> bool:
        hid = self._ensure_hid()
        devices = hid.enumerate(HID_VENDOR_ID, HID_PRODUCT_ID)
        if not devices: return False
        info = self._pick_device(devices)
        if info is None: return False
        dev = hid.device()
        try: dev.open_path(info["path"])
        except Exception:
            try: dev.open(HID_VENDOR_ID, HID_PRODUCT_ID)
            except Exception:
                try: dev.close()
                except Exception: pass
                return False
        try: dev.set_nonblocking(False)
        except Exception: pass
        self.dev = dev
        now = time.monotonic()
        if now - self.last_hid_open_log_at >= HID_FAILURE_LOG_INTERVAL_SEC:
            log_line(
                "daemon",
                f"HID opened vid=0x{HID_VENDOR_ID:04X} pid=0x{HID_PRODUCT_ID:04X}",
            )
            self.last_hid_open_log_at = now
        return True

    def _pick_device(self, devices: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for info in devices:
            if int(info.get("usage_page") or 0) == HID_USAGE_PAGE and int(info.get("usage") or 0) == HID_USAGE:
                return info
        return devices[0] if devices else None

    def _close_device(self) -> None:
        with self.lock:
            dev, self.dev = self.dev, None
            self.last_frame = None
            self.last_frame_written_at = 0.0
        if dev is not None:
            try: dev.close()
            except Exception: pass

    def _poll_input(self) -> None:
        dev = self.dev
        if dev is None: return
        try: data = dev.read(HID_REPORT_SIZE + 1, DEVICE_POLL_TIMEOUT_MS)
        except Exception as exc:
            log_line("daemon", f"HID read failed: {exc}")
            self._close_device()
            return
        if not data: return
        self._touch()
        log_line("daemon", "device frame received") if bytes(data).startswith(b"\x00CDX1") else log_line("daemon", f"device input bytes={len(data)}")

    def _render_frame(self) -> bytes:
        frame = bytearray(HID_REPORT_SIZE)
        status = self.state["status"]
        status_code = STATUS_CODE_MAP.get(status, 0xE1)
        flags = SCREEN_HID_CODEX_FLAG_STATUS_VALID

        if self.state["current_used_percent"] != SCREEN_HID_CODEX_PERCENT_INVALID:
            flags |= SCREEN_HID_CODEX_FLAG_CURRENT_VALID
        if self.state["weekly_used_percent"] != SCREEN_HID_CODEX_PERCENT_INVALID:
            flags |= SCREEN_HID_CODEX_FLAG_WEEKLY_VALID
        if self.state["current_reset_sec"] != SCREEN_HID_CODEX_RESET_INVALID:
            flags |= SCREEN_HID_CODEX_FLAG_CURRENT_RESET_VALID
        if self.state["weekly_reset_sec"] != SCREEN_HID_CODEX_RESET_INVALID:
            flags |= SCREEN_HID_CODEX_FLAG_WEEKLY_RESET_VALID
        if status_code >= 0xE0:
            flags |= SCREEN_HID_CODEX_FLAG_DEGRADED

        frame[0] = SCREEN_HID_CMD_CODEX_MONITOR
        frame[1] = SCREEN_HID_CODEX_SUBCMD_STATE
        frame[2] = SCREEN_HID_PROTOCOL_VERSION
        frame[3] = flags
        _put_u32_le(frame, 4, self.state["frame_seq"])
        frame[8] = status_code
        frame[9] = ICON_CODE_MAP.get(status, 0xFF)
        frame[10] = self.state["current_used_percent"]
        frame[11] = self.state["weekly_used_percent"]
        _put_u32_le(frame, 12, self.state["current_reset_sec"])
        _put_u32_le(frame, 16, self.state["weekly_reset_sec"])
        _put_u32_le(frame, 20, int(self.state["updated_at"]))
        return bytes(frame)

    def _write_frame(self, force: bool = False) -> None:
        dev = self.dev
        if dev is None: return
        payload = self._render_frame()
        frame = bytearray(HID_REPORT_SIZE + 1)
        frame[0] = HID_REPORT_ID
        frame[1:] = payload
        raw = bytes(frame)
        if not force and raw == self.last_frame: return
        try: written = dev.write(list(raw))
        except Exception as exc:
            log_line("daemon", f"HID write failed: {exc}")
            self._close_device()
            return
        if written <= 0:
            self._log_hid_write_failure(f"HID write returned {written}")
            self._close_device()
            return
        self.last_frame = raw
        self.last_frame_written_at = time.monotonic()
        log_line(
            "daemon",
            f"frame written bytes={written}"
            f" seq={self.state['frame_seq']}"
            f" status={self.state['status']}",
        )
        self._touch()

    def _log_hid_write_failure(self, message: str) -> None:
        """首次记录失败，后续一分钟内合并为一条，避免日志持续刷屏。"""

        self.hid_failure_count += 1
        now = time.monotonic()
        if now - self.last_hid_failure_log_at < HID_FAILURE_LOG_INTERVAL_SEC:
            return
        suffix = (
            f" (repeated {self.hid_failure_count - 1} times)"
            if self.hid_failure_count > 1
            else ""
        )
        log_line("daemon", message + suffix)
        self.last_hid_failure_log_at = now
        self.hid_failure_count = 0

    def _idle_watchdog(self) -> None:
        while not self.stop.is_set():
            if time.monotonic() - self.last_activity >= IDLE_TIMEOUT_SEC:
                log_line("daemon", "idle timeout reached")
                self.stop.set()
                return
            time.sleep(2.0)

    def _touch(self) -> None: self.last_activity = time.monotonic()

    def _close_server(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            try: server.close()
            except Exception: pass


def _elapsed_ms(started_at: Any) -> str:
    """计算 relay 到 daemon 当前阶段的耗时，不记录 prompt 内容。"""

    try:
        elapsed = max(0.0, (time.time() - float(started_at)) * 1000)
    except (TypeError, ValueError):
        return "unknown"
    return f"{elapsed:.1f}ms"


def _self_test() -> str:
    daemon = CodexScreenDaemon()
    daemon._apply_event({"hook_event_name": "UserPromptSubmit", "session_id": "session-test", "turn_id": "turn-test", "permission_mode": "acceptEdits", "prompt": "先跑一遍自检。", "quota_text": "quota: 80% remaining"})
    frame = daemon._render_frame()
    return (
        f"codex_frame len={len(frame)} cmd=0x{frame[0]:02X} "
        f"subcmd=0x{frame[1]:02X} status=0x{frame[8]:02X}"
    )
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Codex 屏幕常驻服务")
    parser.add_argument("--daemon", action="store_true", help="由 relay 后台拉起时使用")
    parser.add_argument("--self-test", action="store_true", help="仅执行本地渲染自检")
    args = parser.parse_args(argv)
    if args.self_test:
        print(_self_test())
        return 0
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    return CodexScreenDaemon().run()
if __name__ == "__main__":
    raise SystemExit(main())
