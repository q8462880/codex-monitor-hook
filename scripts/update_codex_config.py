#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装时更新 Codex config.toml。

这个脚本只做三件事：
1. 写入前备份现有 config.toml
2. 用标记块安装/替换本项目 hooks
3. 写完后用 tomllib 验证 TOML 语法
"""

from __future__ import annotations

import datetime
import re
import shlex
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    # Python 3.9/3.10 使用安装器按需安装的 tomli，API 与 tomllib 兼容。
    import tomli as tomllib

from codex_hook_diagnostics import sync_codex_hook_trust


START_MARKER = "# BEGIN codex-monitor-hook"
END_MARKER = "# END codex-monitor-hook"
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)
QUOTA_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "SessionEnd",
)
MINIMAL_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "Stop",
    "SessionEnd",
)
HOOK_PROFILES = {
    "quota": QUOTA_HOOK_EVENTS,
    "full": HOOK_EVENTS,
    "minimal": MINIMAL_HOOK_EVENTS,
}
def toml_string(value: str) -> str:
    """生成 TOML 字符串；Windows 路径优先用单引号避免反斜杠转义。"""

    if "'" not in value:
        return "'" + value + "'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def powershell_command(pythonw_path: str, relay_path: str) -> str:
    """生成 commandWindows 的安全 PowerShell 调用表达式。"""

    # Desktop 当前通过 pwsh -Command 执行 commandWindows。PowerShell 中单独
    # 写出带引号的 exe 路径只是字符串，而前置 & 才会真正启动 pythonw.exe。
    # 单引号加倍可以保留路径中的单引号，不把路径内容解释为 PowerShell 语法。
    pythonw_literal = pythonw_path.replace("'", "''")
    relay_literal = relay_path.replace("'", "''")
    return f"& '{pythonw_literal}' '{relay_literal}'"


def _platform_name(platform_name: str | None) -> str:
    if platform_name:
        return platform_name.lower()
    return "windows" if sys.platform == "win32" else "posix"


def build_hook_block(
    pythonw_path: str,
    relay_path: str,
    hook_profile: str = "quota",
    platform_name: str | None = None,
) -> str:
    """生成 Codex 当前版本能识别的嵌套 hook 配置。"""

    if not pythonw_path or not relay_path:
        raise ValueError("pythonw path and relay path are required")
    if hook_profile not in HOOK_PROFILES:
        raise ValueError(f"unknown hook profile: {hook_profile}")
    target_platform = _platform_name(platform_name)
    if target_platform not in {"windows", "posix"}:
        raise ValueError(f"unknown platform: {platform_name}")
    if target_platform == "windows":
        # Windows runner 会把 Hook JSON 放在脚本路径前。当前解释器用户目录的
        # 条件化 Python bootstrap 会在尝试打开 JSON 前转交 relay。
        relay_command = toml_string(f'"{pythonw_path}" "{relay_path}"')
    else:
        # macOS/Linux 没有 pythonw.exe，Codex 直接执行 shell command；shlex
        # 能正确处理用户目录中空格和单引号，避免路径被拆成多个参数。
        relay_command = toml_string(shlex.join([pythonw_path, relay_path]))
    lines = [START_MARKER]
    for event_name in HOOK_PROFILES[hook_profile]:
        lines.extend(
            [
                "",
                f"[[hooks.{event_name}]]",
                "",
                f"[[hooks.{event_name}.hooks]]",
                'type = "command"',
                f"command = {relay_command}",
                f"timeout = {3 if event_name == 'SessionEnd' else 5}",
            ]
        )
        if target_platform == "windows":
            command_windows = toml_string(
                powershell_command(pythonw_path, relay_path)
            )
            lines.insert(-1, f"commandWindows = {command_windows}")
    lines.append(END_MARKER)
    return "\n".join(lines)


def backup_config(config_path: Path) -> str:
    """备份已有 config；没有旧文件时返回空字符串。"""

    if not config_path.exists():
        return ""

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = config_path.with_name(f"{config_path.name}.codex-monitor-hook.{stamp}.bak")
    shutil.copy2(config_path, backup_path)
    return str(backup_path)


def merge_hook_block(old_text: str, block: str) -> str:
    """替换 Hook 定义，同时保留 Codex 维护的信任状态和用户配置。"""

    pattern = rf"(?s){re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}"
    match = re.search(pattern, old_text)
    if match:
        old_block = match.group(0)
        suffix = _preserved_suffix(old_block)
        replacement = block
        if suffix:
            replacement += "\n\n" + suffix
        return old_text[: match.start()] + replacement + old_text[match.end() :]

    prefix = old_text.rstrip()
    return f"{prefix}\n\n{block}" if prefix else block


def _preserved_suffix(old_block: str) -> str:
    """提取旧标记块中 Codex/用户维护的 TOML 表区。"""

    markers = ("\n[hooks.state]", "\n[plugins.", "\n[features]")
    positions = [old_block.find(marker) for marker in markers]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return ""

    start = min(positions)
    suffix = old_block[start:].strip()
    return suffix.removesuffix(END_MARKER).rstrip()


def update_config(
    config_path: Path,
    pythonw_path: str,
    relay_path: str,
    hook_profile: str = "quota",
    platform_name: str | None = None,
) -> str:
    """更新 config 并返回备份路径。"""

    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_config(config_path)
    old_text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
    new_text = merge_hook_block(
        old_text,
        build_hook_block(
            pythonw_path,
            relay_path,
            hook_profile,
            platform_name=platform_name,
        ),
    )
    config_path.write_text(new_text.rstrip() + "\n", encoding="utf-8")
    tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    sync_codex_hook_trust(config_path, str(Path.cwd()))
    tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    return backup_path


def main(argv: list[str]) -> int:
    if len(argv) not in (4, 5, 6):
        print(
            "usage: update_codex_config.py <config.toml> <python> <relay.py> [quota|full|minimal] [windows|posix]",
            file=sys.stderr,
        )
        return 2

    hook_profile = argv[4] if len(argv) == 5 else "quota"
    platform_name = argv[5] if len(argv) == 6 else None
    backup_path = update_config(
        Path(argv[1]),
        argv[2], argv[3], hook_profile,
        platform_name=platform_name,
    )
    print("BACKUP_PATH=" + backup_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
