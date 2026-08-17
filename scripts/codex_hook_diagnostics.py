# -*- coding: utf-8 -*-
"""通过 Codex app-server 检查 Hook 是否真正可执行。"""

from __future__ import annotations

import queue
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from codex_quota_client import (
    _initialize_request,
    _iter_runnable_codex_paths,
    _read_response,
    _send,
    _start_app_server,
    _start_reader,
    _stop_process,
)


HOOKS_LIST_METHOD = "hooks/list"
HOOK_COMMAND_MARKER = "codex_hook_relay"
EXECUTABLE_TRUST_STATES = {"managed", "trusted"}


def summarize_hooks_response(
    message: Dict[str, Any],
    command_marker: str = HOOK_COMMAND_MARKER,
) -> Dict[str, Any]:
    """汇总本项目 Hook 的加载、启用和信任状态。"""

    result = message.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    entries = data if isinstance(data, list) else []
    hooks: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks.extend(_matching_hooks(entry.get("hooks"), command_marker))
        warnings.extend(_string_items(entry.get("warnings")))
        errors.extend(_error_items(entry.get("errors")))

    return {
        "loaded": len(hooks),
        "executable": sum(_hook_is_executable(hook) for hook in hooks),
        "disabled": sum(not bool(hook.get("enabled")) for hook in hooks),
        "untrusted": sum(hook.get("trustStatus") == "untrusted" for hook in hooks),
        "modified": sum(hook.get("trustStatus") == "modified" for hook in hooks),
        "warnings": warnings,
        "errors": errors,
    }


def diagnose_codex_hooks(cwd: str, timeout_sec: float = 8.0) -> Dict[str, Any]:
    """使用首个可运行 Codex 查询 hooks/list，失败时返回诊断原因。"""

    failures: List[str] = []
    for exe_path in _iter_runnable_codex_paths():
        try:
            summary = _query_hooks(exe_path, cwd, timeout_sec)
            summary["codex_exe"] = exe_path
            return summary
        except Exception as exc:
            failures.append(f"{Path(exe_path).name}: {exc}")
    return {
        "loaded": 0,
        "executable": 0,
        "disabled": 0,
        "untrusted": 0,
        "modified": 0,
        "warnings": [],
        "errors": failures or ["没有找到可运行的 Codex CLI"],
        "codex_exe": "-",
    }


def sync_codex_hook_trust(config_path: Path, cwd: str, timeout_sec: float = 8.0) -> int:
    """把刚由安装器写入的本项目 Hook 哈希同步为可信状态。"""

    for exe_path in _iter_runnable_codex_paths():
        try:
            response = _query_hooks_response(exe_path, cwd, timeout_sec)
            updates = _trusted_hash_updates(response, config_path)
            if updates:
                _write_trusted_hashes(config_path, updates)
            return len(updates)
        except Exception:
            continue
    return 0


def _query_hooks(exe_path: str, cwd: str, timeout_sec: float) -> Dict[str, Any]:
    return summarize_hooks_response(_query_hooks_response(exe_path, cwd, timeout_sec))


def _query_hooks_response(
    exe_path: str, cwd: str, timeout_sec: float
) -> Dict[str, Any]:
    process = _start_app_server(exe_path)
    output: "queue.Queue[str]" = queue.Queue()
    _start_reader(process.stdout, output)
    _start_reader(process.stderr, None)
    try:
        _send(process, _initialize_request())
        _raise_rpc_error(_required_response(output, 1, timeout_sec))
        _send(
            process,
            {"id": 2, "method": HOOKS_LIST_METHOD, "params": {"cwds": [cwd]}},
        )
        response = _required_response(output, 2, timeout_sec)
        _raise_rpc_error(response)
        return response
    finally:
        _stop_process(process)


def _required_response(
    output: "queue.Queue[str]", request_id: int, timeout_sec: float
) -> Dict[str, Any]:
    response = _read_response(output, request_id, time.time() + timeout_sec)
    if response is None:
        raise TimeoutError(f"app-server request {request_id} timed out")
    return response


def _raise_rpc_error(message: Dict[str, Any]) -> None:
    error = message.get("error")
    if isinstance(error, dict):
        raise RuntimeError(str(error.get("message") or error))


def _matching_hooks(value: Any, marker: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        hook
        for hook in value
        if isinstance(hook, dict) and marker in str(hook.get("command") or "")
    ]


def _hook_is_executable(hook: Dict[str, Any]) -> bool:
    return bool(hook.get("enabled")) and hook.get("trustStatus") in EXECUTABLE_TRUST_STATES


def _string_items(value: Any) -> List[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _error_items(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    items = []
    for error in value:
        if isinstance(error, dict):
            items.append(f"{error.get('path', '-')}: {error.get('message', error)}")
        else:
            items.append(str(error))
    return items


def _trusted_hash_updates(message: Dict[str, Any], config_path: Path) -> Dict[str, str]:
    """仅选择当前 config 标记块中的 relay 命令，避免信任无关 Hook。"""

    result = message.get("result")
    groups = result.get("data") if isinstance(result, dict) else []
    expected_path = str(config_path)
    updates: Dict[str, str] = {}
    for group in groups if isinstance(groups, list) else []:
        for hook in group.get("hooks", []) if isinstance(group, dict) else []:
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command") or "")
            if (
                hook.get("sourcePath") == expected_path
                and HOOK_COMMAND_MARKER in command
                and isinstance(hook.get("key"), str)
                and isinstance(hook.get("currentHash"), str)
            ):
                updates[hook["key"]] = hook["currentHash"]
    return updates


def _write_trusted_hashes(config_path: Path, updates: Dict[str, str]) -> None:
    """更新信任哈希；首次写入时创建 hooks.state 父表。"""

    text = config_path.read_text(encoding="utf-8-sig")
    state_header = "[hooks.state]\n"
    if state_header not in text:
        # hooks.state 必须是根级 TOML 表。追加到文件末尾不会改变现有项目、插件
        # 或 Hook 数组表的作用域；缺少它时原先的 replace() 会静默丢弃信任哈希。
        text = text.rstrip() + "\n\n" + state_header
    for key, digest in updates.items():
        section = f"[hooks.state.'{key}']"
        pattern = rf"({re.escape(section)}\s*\ntrusted_hash\s*=\s*\")[^\"]*(\")"
        if re.search(pattern, text):
            text = re.sub(pattern, rf"\g<1>{digest}\2", text, count=1)
            continue
        insertion = f"\n{section}\ntrusted_hash = \"{digest}\"\n"
        text = text.replace("[hooks.state]\n", "[hooks.state]\n" + insertion, 1)
    config_path.write_text(text, encoding="utf-8")


def format_hook_diagnostic(summary: Dict[str, Any], expected_count: int = 0) -> str:
    """生成适合安装器直接展示的简短中文诊断。"""

    lines = [
        f"[hook-check] codex={summary.get('codex_exe', '-')}",
        f"[hook-check] loaded={summary['loaded']} executable={summary['executable']} "
        f"untrusted={summary['untrusted']} modified={summary['modified']} "
        f"disabled={summary['disabled']}",
    ]
    for warning in summary["warnings"]:
        lines.append(f"[hook-check] warning: {warning}")
    for error in summary["errors"]:
        lines.append(f"[hook-check] error: {error}")
    if expected_count and summary["loaded"] < expected_count:
        lines.append(
            f"[hook-check] error: expected {expected_count} hooks, "
            f"but Codex loaded {summary['loaded']}"
        )
    return "\n".join(lines)


def hook_diagnostic_exit_code(summary: Dict[str, Any], expected_count: int = 0) -> int:
    if summary["errors"] or summary["loaded"] == 0:
        return 4
    if expected_count and summary["loaded"] < expected_count:
        return 3
    return 0 if summary["executable"] == summary["loaded"] else 2
