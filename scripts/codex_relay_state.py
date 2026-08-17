# -*- coding: utf-8 -*-
"""Relay 侧的多会话状态缓存。

每个 Codex Hook 都可能是一个新的短生命周期进程，所以状态不能只放在
Python 全局变量里。本模块把会话缓存序列化到本地 JSON；relay 自己负责
加锁，保证多个并发 Hook 不会同时覆盖文件。

这里不使用 PID、时间戳或后台工具事件来选择当前会话。
当前先把用户明确提交提示词的 UserPromptSubmit 作为“最后一次用户动作”，
用它更新 active_session_id；独立控制接口仍可用于桌面桥接和测试。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


STATE_UNKNOWN = "UNKNOWN"
STATE_IDLE = "IDLE"
STATE_READY = "READY"
STATE_THINKING = "THINKING"
STATE_EXECUTING = "EXECUTING"
STATE_WAIT_PERM = "WAIT_PERM"
STATE_COMPACTING = "COMPACTING"
STATE_SUBAGENT = "SUBAGENT"

SESSION_START_EVENT = "SessionStart"
SESSION_END_EVENT = "SessionEnd"
TURN_START_EVENT = "UserPromptSubmit"
TURN_END_EVENT = "Stop"

RUNNING_EVENTS = {
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
}

IMPLICIT_TURN_START_EVENTS = {
    "PermissionRequest",
    "PreToolUse",
    "PreCompact",
    "SubagentStart",
}

EVENT_STATE_MAP = {
    SESSION_START_EVENT: STATE_IDLE,
    TURN_START_EVENT: STATE_THINKING,
    "PermissionRequest": STATE_WAIT_PERM,
    "PreToolUse": STATE_EXECUTING,
    "PostToolUse": STATE_THINKING,
    "PreCompact": STATE_COMPACTING,
    "PostCompact": STATE_THINKING,
    "SubagentStart": STATE_SUBAGENT,
    "SubagentStop": STATE_THINKING,
    TURN_END_EVENT: STATE_IDLE,
    SESSION_END_EVENT: STATE_IDLE,
}

REPLAY_EVENT_FOR_STATE = {
    STATE_EXECUTING: "PreToolUse",
    STATE_WAIT_PERM: "PermissionRequest",
    STATE_COMPACTING: "PreCompact",
    STATE_SUBAGENT: "SubagentStart",
}
MAX_STATE_BYTES = 256 * 1024


@dataclass
class RelayEventResult:
    """一次 Hook 更新的结果，供 relay 决定是否发送到 daemon。"""

    accepted: bool
    session_id: str
    status: str
    reason: str


@dataclass
class SessionCache:
    """单个 session 的最近可恢复状态。

    turn_id 和 tool_use_id 只存在这个对象里，绝不会跨 session 比较。
    last_event_name 仅用于切换时重建 daemon 能理解的事件序列，不参与
    active session 选择。
    """

    session_id: str
    status: str = STATE_UNKNOWN
    turn_active: bool = False
    turn_id: str = "-"
    tool_use_id: str = "-"
    resume_status: str = STATE_THINKING
    ended: bool = False
    last_event_name: str = "UNKNOWN"
    permission_mode: str = ""
    source: str = ""
    tool_name: str = ""
    seen_event_keys: List[str] = field(default_factory=list)
    closed_turn_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "turn_active": self.turn_active,
            "turn_id": self.turn_id,
            "tool_use_id": self.tool_use_id,
            "resume_status": self.resume_status,
            "ended": self.ended,
            "last_event_name": self.last_event_name,
            "permission_mode": self.permission_mode,
            "source": self.source,
            "tool_name": self.tool_name,
            "seen_event_keys": self.seen_event_keys[-64:],
            "closed_turn_ids": self.closed_turn_ids[-32:],
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "SessionCache":
        session_id = _normalize_id(value.get("session_id"))
        return cls(
            session_id=session_id,
            status=str(value.get("status") or STATE_UNKNOWN),
            turn_active=bool(value.get("turn_active")),
            turn_id=_normalize_id(value.get("turn_id")),
            tool_use_id=_normalize_id(value.get("tool_use_id")),
            resume_status=str(value.get("resume_status") or STATE_THINKING),
            ended=bool(value.get("ended")),
            last_event_name=str(value.get("last_event_name") or "UNKNOWN"),
            permission_mode=str(value.get("permission_mode") or ""),
            source=str(value.get("source") or ""),
            tool_name=str(value.get("tool_name") or ""),
            seen_event_keys=_string_list(value.get("seen_event_keys"))[-64:],
            closed_turn_ids=_string_list(value.get("closed_turn_ids"))[-32:],
        )


class RelaySessionState:
    """保存所有 session，并单独记录由外部输入指定的 active session。"""

    MAX_SESSIONS = 32
    MAX_SEEN_EVENT_KEYS = 64
    MAX_CLOSED_TURNS = 32

    def __init__(
        self,
        active_session_id: Optional[str] = None,
        sessions: Optional[Dict[str, SessionCache]] = None,
    ) -> None:
        self.active_session_id = _optional_id(active_session_id)
        self.sessions = sessions or {}

    def apply_event(self, event: Dict[str, Any]) -> RelayEventResult:
        """更新 session 缓存，并按用户提交提示词选择当前 session。"""

        session_id = _normalize_id(event.get("session_id"))
        event_name = str(event.get("hook_event_name") or "UNKNOWN")
        if session_id == "-":
            return RelayEventResult(False, session_id, STATE_UNKNOWN, "missing_session_id")

        cache = self._get_or_create(session_id)
        event_key = _event_key(event)
        if event_key in cache.seen_event_keys:
            return RelayEventResult(False, session_id, cache.status, "duplicate")
        cache.seen_event_keys.append(event_key)
        cache.seen_event_keys = cache.seen_event_keys[-self.MAX_SEEN_EVENT_KEYS :]

        if event_name == SESSION_START_EVENT:
            if cache.turn_active:
                return RelayEventResult(False, session_id, cache.status, "stale_session_start")
            self._reset_session(cache)
            return self._accepted(cache, event_name)

        if event_name == SESSION_END_EVENT:
            # 没有 turn_id 的 SessionEnd 不能结束一个仍在运行的 turn；
            # 这样可以避免旧会话的迟到结束事件覆盖当前缓存。
            if cache.turn_active:
                return RelayEventResult(False, session_id, cache.status, "stale_session_end")
            cache.ended = True
            cache.status = STATE_IDLE
            cache.last_event_name = event_name
            return self._accepted(cache, event_name)

        if event_name == TURN_START_EVENT:
            result = self._apply_turn_start(cache, event)
            if result.accepted:
                # UserPromptSubmit 是目前 Hook 能可靠代表“用户刚操作了这个
                # 对话”的事件。只在这里切换，避免后台工具事件抢占 HID。
                self.active_session_id = cache.session_id
                result.reason = "accepted_and_auto_selected"
            return result

        if event_name == TURN_END_EVENT:
            return self._apply_turn_end(cache, event)

        if event_name in RUNNING_EVENTS:
            return self._apply_running_event(cache, event)

        cache.last_event_name = event_name
        return self._accepted(cache, event_name)

    def set_active_session(self, session_id: str) -> Optional[SessionCache]:
        """只接受外部显式输入，不从普通 Hook 推断桌面选中项。"""

        normalized = _normalize_id(session_id)
        if normalized == "-":
            self.active_session_id = None
            return None
        self.active_session_id = normalized
        return self.sessions.get(normalized)

    def clear_active_session(self) -> None:
        self.active_session_id = None

    def should_forward(self, session_id: Optional[str]) -> bool:
        """只有明确选中的 session 才允许把原始 Hook 发给 daemon。"""

        return (
            self.active_session_id is not None
            and _normalize_id(session_id) == self.active_session_id
        )

    def replay_events(self, session_id: str) -> List[Dict[str, Any]]:
        """生成切换到目标 session 时 daemon 能理解的恢复事件序列。

        daemon 当前只接受既有 Hook 事件，因此这里用 SessionStart +
        UserPromptSubmit + 详细运行事件重建已缓存状态，不新增协议。
        READY/UNKNOWN 没有对应的既有 Hook 事件，返回空序列，由调用方记录
        “无法立即恢复”的事实，避免伪造一个后台会话状态。
        """

        normalized = _normalize_id(session_id)
        cache = self.sessions.get(normalized)
        if cache is None or cache.status in {STATE_UNKNOWN, STATE_READY}:
            return []

        events: List[Dict[str, Any]] = [
            self._replay_event(cache, SESSION_START_EVENT)
        ]
        if not cache.turn_active:
            return events

        events.append(self._replay_event(cache, TURN_START_EVENT))
        detail_event = REPLAY_EVENT_FOR_STATE.get(cache.status)
        if detail_event:
            events.append(self._replay_event(cache, detail_event))
        return events

    def status_for(self, session_id: str) -> str:
        cache = self.sessions.get(_normalize_id(session_id))
        return cache.status if cache else STATE_UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "active_session_id": self.active_session_id,
            "sessions": {
                session_id: cache.to_dict()
                for session_id, cache in self.sessions.items()
            },
        }

    def compact(self) -> None:
        """主动压缩缓存，保证状态文件不会随 Hook 数量无限增长。"""

        for cache in self.sessions.values():
            cache.seen_event_keys = cache.seen_event_keys[
                -self.MAX_SEEN_EVENT_KEYS :
            ]
            cache.closed_turn_ids = cache.closed_turn_ids[
                -self.MAX_CLOSED_TURNS :
            ]
        self._trim_sessions()

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "RelaySessionState":
        raw_sessions = value.get("sessions")
        sessions: Dict[str, SessionCache] = {}
        if isinstance(raw_sessions, dict):
            for session_id, raw_cache in raw_sessions.items():
                if isinstance(raw_cache, dict):
                    cache = SessionCache.from_dict(raw_cache)
                    if cache.session_id != "-":
                        sessions[str(session_id)] = cache
        return cls(value.get("active_session_id"), sessions)

    def _get_or_create(self, session_id: str) -> SessionCache:
        cache = self.sessions.get(session_id)
        if cache is None:
            cache = SessionCache(session_id=session_id)
            self.sessions[session_id] = cache
            self._trim_sessions()
        return cache

    def _apply_turn_start(
        self,
        cache: SessionCache,
        event: Dict[str, Any],
    ) -> RelayEventResult:
        turn_id = _normalize_id(event.get("turn_id"))
        if turn_id != "-" and turn_id in cache.closed_turn_ids:
            return RelayEventResult(False, cache.session_id, cache.status, "stale_turn")
        if cache.turn_active and turn_id not in ("-", cache.turn_id):
            # UserPromptSubmit 是用户明确开始新一轮的唯一可靠边界。部分
            # Desktop 场景会延迟或遗漏上一轮 Stop；拒绝新 turn 会让同一会话
            # 永久停在旧状态。先封存旧 turn，迟到的工具/Stop 事件便会失效。
            self._remember_closed_turn(cache)

        cache.ended = False
        cache.turn_active = True
        cache.turn_id = turn_id
        cache.tool_use_id = "-"
        cache.status = STATE_THINKING
        cache.resume_status = STATE_THINKING
        self._copy_event_fields(cache, event)
        return self._accepted(cache, TURN_START_EVENT)

    def _apply_turn_end(
        self,
        cache: SessionCache,
        event: Dict[str, Any],
    ) -> RelayEventResult:
        turn_id = _normalize_id(event.get("turn_id"))
        if not cache.turn_active:
            return RelayEventResult(False, cache.session_id, cache.status, "no_active_turn")
        if turn_id not in ("-", cache.turn_id):
            return RelayEventResult(False, cache.session_id, cache.status, "stale_turn")

        self._remember_closed_turn(cache)
        cache.turn_active = False
        cache.status = STATE_IDLE
        cache.tool_use_id = "-"
        cache.last_event_name = TURN_END_EVENT
        self._copy_event_fields(cache, event)
        return self._accepted(cache, TURN_END_EVENT)

    def _apply_running_event(
        self,
        cache: SessionCache,
        event: Dict[str, Any],
    ) -> RelayEventResult:
        turn_id = _normalize_id(event.get("turn_id"))
        if turn_id != "-" and turn_id in cache.closed_turn_ids:
            return RelayEventResult(False, cache.session_id, cache.status, "stale_turn")
        if cache.turn_active and turn_id not in ("-", cache.turn_id):
            return RelayEventResult(False, cache.session_id, cache.status, "foreign_turn")
        if not cache.turn_active:
            if event["hook_event_name"] not in IMPLICIT_TURN_START_EVENTS:
                return RelayEventResult(False, cache.session_id, cache.status, "no_active_turn")
            cache.turn_active = True
            cache.turn_id = turn_id
            cache.resume_status = STATE_THINKING

        event_name = str(event["hook_event_name"])
        if event_name == "SubagentStart":
            cache.resume_status = cache.status
            cache.status = STATE_SUBAGENT
        elif event_name == "SubagentStop":
            cache.status = cache.resume_status or STATE_THINKING
        else:
            cache.status = EVENT_STATE_MAP[event_name]
        cache.ended = False
        self._copy_event_fields(cache, event)
        return self._accepted(cache, event_name)

    def _accepted(self, cache: SessionCache, event_name: str) -> RelayEventResult:
        cache.last_event_name = event_name
        return RelayEventResult(True, cache.session_id, cache.status, "accepted")

    @staticmethod
    def _remember_closed_turn(cache: SessionCache) -> None:
        """记录已结束 turn，让其迟到事件不能覆盖最新用户操作。"""

        if cache.turn_id == "-":
            return
        cache.closed_turn_ids.append(cache.turn_id)
        cache.closed_turn_ids = cache.closed_turn_ids[-RelaySessionState.MAX_CLOSED_TURNS :]

    @staticmethod
    def _reset_session(cache: SessionCache) -> None:
        cache.status = STATE_IDLE
        cache.turn_active = False
        cache.turn_id = "-"
        cache.tool_use_id = "-"
        cache.resume_status = STATE_THINKING
        cache.ended = False
        cache.last_event_name = SESSION_START_EVENT

    @staticmethod
    def _copy_event_fields(cache: SessionCache, event: Dict[str, Any]) -> None:
        cache.permission_mode = str(event.get("permission_mode") or "")
        cache.source = str(event.get("source") or "")
        cache.tool_name = str(event.get("tool_name") or "")
        cache.tool_use_id = _normalize_id(event.get("tool_use_id"))

    @staticmethod
    def _replay_event(cache: SessionCache, event_name: str) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "hook_event_name": event_name,
            "session_id": cache.session_id,
            "_relay_replay": True,
            "_relay_state_hint": cache.status,
        }
        if event_name != SESSION_START_EVENT and cache.turn_id != "-":
            event["turn_id"] = cache.turn_id
        if cache.tool_use_id != "-":
            event["tool_use_id"] = cache.tool_use_id
        if cache.permission_mode:
            event["permission_mode"] = cache.permission_mode
        if cache.source:
            event["source"] = cache.source
        if cache.tool_name:
            event["tool_name"] = cache.tool_name
        return event

    def _trim_sessions(self) -> None:
        while len(self.sessions) > self.MAX_SESSIONS:
            removable = next(
                (
                    session_id
                    for session_id, cache in self.sessions.items()
                    if session_id != self.active_session_id and not cache.turn_active
                ),
                None,
            )
            if removable is None:
                removable = next(iter(self.sessions))
            self.sessions.pop(removable, None)


def load_state(path: Path) -> RelaySessionState:
    temporary: Optional[Path] = None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return RelaySessionState()
    return RelaySessionState.from_dict(value) if isinstance(value, dict) else RelaySessionState()


def save_state(path: Path, state: RelaySessionState) -> None:
    """尽力保存缓存；磁盘问题不能让 Codex Hook 失败。"""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(
            state.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
            state.compact()
            payload = json.dumps(
                state.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _event_key(event: Dict[str, Any]) -> str:
    """用事件内容去重，不把 prompt 原文写入磁盘。"""

    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _optional_id(value: Any) -> Optional[str]:
    normalized = _normalize_id(value)
    return None if normalized == "-" else normalized


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    return [str(item) for item in value]
