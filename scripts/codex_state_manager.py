# -*- coding: utf-8 -*-
"""Codex session/turn 生命周期状态机。

Hook 事件不是一个简单的“事件名 -> UI 状态”映射：
同一个 session 会包含多轮 turn，多个 session 也可能交错抵达 daemon。
本模块先判断事件属于哪个 session/turn，再决定是否切换屏幕状态，
避免旧 session 的 Stop 把当前对话错误地切回 Idle。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional


STATE_IDLE = "IDLE"
STATE_READY = "READY"
STATE_THINKING = "THINKING"
STATE_WAIT_PERM = "WAIT_PERM"
STATE_EXECUTING = "EXECUTING"
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


@dataclass
class SessionRuntime:
    """一个 Codex session 的生命周期快照。"""

    session_id: str
    active: bool = True
    turn_active: bool = False
    active_turn_id: str = "-"
    status: str = STATE_IDLE
    resume_status: str = STATE_THINKING
    last_event_at: float = 0.0


class CodexStateManager:
    """按 session/turn 过滤并推进 Codex 状态。"""

    MAX_SESSIONS = 32

    def __init__(self) -> None:
        self.sessions: Dict[str, SessionRuntime] = {}
        self.active_session_id = "-"
        self.last_turn_id = "-"
        self.status = STATE_IDLE
        self.session_active = False
        self.turn_active = False
        self.last_event = "INIT"

    def apply_event(
        self,
        event_name: str,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """校验归属并应用事件，返回事件是否被当前显示 session 接受。"""

        now = time.time() if now is None else now
        sid = self._normalize_id(session_id)
        tid = self._normalize_id(turn_id)

        if event_name == SESSION_START_EVENT:
            return self._start_session(sid, now)

        if event_name == SESSION_END_EVENT:
            return self._end_session(sid, now)

        if event_name == TURN_START_EVENT:
            runtime = self._resolve_runtime_for_turn_start(sid, now)
        else:
            runtime = self._resolve_runtime(
                sid,
                now,
                allow_session_takeover=event_name in RUNNING_EVENTS,
            )
        if runtime is None:
            return False

        if event_name == TURN_START_EVENT:
            self._start_turn(runtime, tid, now)
        elif not self._matches_turn(runtime, event_name, tid):
            return False
        elif event_name == TURN_END_EVENT:
            self._stop_turn(runtime, tid, now)
        elif event_name in RUNNING_EVENTS:
            self._apply_running_event(runtime, event_name, now)
        else:
            runtime.last_event_at = now

        self._publish(runtime, event_name, tid)
        return True

    def expire_stale_status(
        self,
        timeout_sec: float,
        now: Optional[float] = None,
    ) -> bool:
        """长时间没有任何 Hook 时回到 READY，避免屏幕停在旧状态。"""

        if timeout_sec <= 0:
            return False
        now = time.time() if now is None else now
        runtime = self.sessions.get(self.active_session_id)
        if runtime is None or runtime.status == STATE_READY:
            return False
        if now - runtime.last_event_at < timeout_sec:
            return False

        runtime.turn_active = False
        runtime.status = STATE_READY
        self._publish(runtime, "HookTimeout", runtime.active_turn_id)
        return True

    def expire_stale_thinking_turn(
        self,
        timeout_sec: float,
        now: Optional[float] = None,
    ) -> bool:
        """兼容旧调用名，实际现在检查整个 session 的最后 Hook。"""

        return self.expire_stale_status(timeout_sec, now)

    def active_status_age(self, now: Optional[float] = None) -> Optional[float]:
        """返回当前 session 最后一次 Hook 的年龄。"""

        runtime = self.sessions.get(self.active_session_id)
        if runtime is None:
            return None
        current = time.time() if now is None else now
        return max(0.0, current - runtime.last_event_at)

    def active_turn_age(self, now: Optional[float] = None) -> Optional[float]:
        """兼容旧调用名，返回当前 session 最后 Hook 的年龄。"""

        return self.active_status_age(now)

    def _start_session(self, session_id: str, now: float) -> bool:
        if session_id == "-":
            return False

        runtime = self.sessions.get(session_id)
        if runtime is None:
            runtime = SessionRuntime(session_id=session_id)
            self.sessions[session_id] = runtime
        runtime.active = True
        runtime.turn_active = False
        runtime.active_turn_id = "-"
        runtime.status = STATE_IDLE
        runtime.resume_status = STATE_THINKING
        runtime.last_event_at = now
        self.active_session_id = session_id
        self._publish(runtime, SESSION_START_EVENT, "-")
        self._trim_sessions()
        return True

    def _end_session(self, session_id: str, now: float) -> bool:
        if session_id == "-":
            session_id = self.active_session_id
        runtime = self.sessions.get(session_id)
        if runtime is None or session_id != self.active_session_id:
            return False

        runtime.active = False
        runtime.turn_active = False
        runtime.status = STATE_IDLE
        runtime.last_event_at = now
        self.status = STATE_IDLE
        self.session_active = False
        self.turn_active = False
        self.last_event = SESSION_END_EVENT
        self.last_turn_id = runtime.active_turn_id
        return True

    def _resolve_runtime(
        self,
        session_id: str,
        now: float,
        allow_session_takeover: bool = False,
    ) -> Optional[SessionRuntime]:
        if session_id == "-":
            session_id = self.active_session_id
        if session_id == "-":
            return None

        current_runtime = self.sessions.get(self.active_session_id)
        if self.active_session_id not in ("-", session_id):
            if not allow_session_takeover:
                return None
            if not self._can_takeover_session(current_runtime):
                return None

        runtime = self.sessions.get(session_id)
        if runtime is None:
            runtime = SessionRuntime(session_id=session_id)
            self.sessions[session_id] = runtime
        if not runtime.active:
            return None
        self.active_session_id = session_id
        runtime.last_event_at = now
        return runtime

    def _resolve_runtime_for_turn_start(
        self,
        session_id: str,
        now: float,
    ) -> Optional[SessionRuntime]:
        """允许 Prompt 事件在 SessionStart 丢失时恢复显示 session。"""

        if session_id == "-":
            session_id = self.active_session_id
        if session_id == "-":
            return None

        runtime = self.sessions.get(session_id)
        if runtime is None:
            runtime = SessionRuntime(session_id=session_id)
            self.sessions[session_id] = runtime
        runtime.active = True
        runtime.last_event_at = now
        self.active_session_id = session_id
        return runtime

    @staticmethod
    def _can_takeover_session(runtime: Optional[SessionRuntime]) -> bool:
        if runtime is None:
            return True
        if runtime.turn_active:
            return False
        return runtime.status in {STATE_IDLE, STATE_READY}

    @staticmethod
    def _matches_turn(
        runtime: SessionRuntime,
        event_name: str,
        turn_id: str,
    ) -> bool:
        if event_name == TURN_END_EVENT and turn_id == "-":
            # 有些 Codex 客户端的 Stop hook 可能不带 turn_id。
            # 这里已经先按 active_session_id 过滤过 session，所以只允许它结束
            # 当前活跃 session 里的当前 turn；旧 session 仍然不能影响新对话。
            return runtime.turn_active
        if not runtime.turn_active:
            # Stop 后同一个 turn 的迟到 PostToolUse 必须丢弃；
            # 没有活动 turn 时，新的 turn_id 可以隐式开启一轮运行。
            if turn_id != "-" and turn_id != runtime.active_turn_id:
                return True
            return event_name in IMPLICIT_TURN_START_EVENTS
        return turn_id in ("-", runtime.active_turn_id)

    def _start_turn(
        self,
        runtime: SessionRuntime,
        turn_id: str,
        now: float,
    ) -> None:
        runtime.turn_active = True
        runtime.active_turn_id = turn_id
        runtime.status = STATE_THINKING
        runtime.resume_status = STATE_THINKING
        runtime.last_event_at = now
        self.last_turn_id = turn_id

    def _stop_turn(
        self,
        runtime: SessionRuntime,
        turn_id: str,
        now: float,
    ) -> None:
        if not runtime.turn_active:
            return
        runtime.turn_active = False
        runtime.status = STATE_IDLE
        runtime.last_event_at = now
        self.last_turn_id = turn_id if turn_id != "-" else runtime.active_turn_id

    def _apply_running_event(
        self,
        runtime: SessionRuntime,
        event_name: str,
        now: float,
    ) -> None:
        if not runtime.turn_active:
            runtime.turn_active = True
            runtime.status = STATE_THINKING
            runtime.resume_status = STATE_THINKING

        if event_name == "SubagentStart":
            runtime.resume_status = runtime.status
            runtime.status = STATE_SUBAGENT
        elif event_name == "SubagentStop":
            runtime.status = runtime.resume_status or STATE_THINKING
        else:
            runtime.status = EVENT_STATE_MAP[event_name]
        runtime.last_event_at = now

    def _publish(
        self,
        runtime: SessionRuntime,
        event_name: str,
        turn_id: str,
    ) -> None:
        self.active_session_id = runtime.session_id
        self.status = runtime.status
        self.session_active = runtime.active
        self.turn_active = runtime.turn_active
        self.last_event = event_name
        self.last_turn_id = turn_id if turn_id != "-" else runtime.active_turn_id

    @staticmethod
    def _normalize_id(value: Optional[str]) -> str:
        if value is None:
            return "-"
        text = str(value).strip()
        return text or "-"

    def _trim_sessions(self) -> None:
        if len(self.sessions) <= self.MAX_SESSIONS:
            return
        inactive = [
            item for item in self.sessions.values() if not item.active
        ]
        inactive.sort(key=lambda item: item.last_event_at)
        for item in inactive[: len(self.sessions) - self.MAX_SESSIONS]:
            self.sessions.pop(item.session_id, None)
