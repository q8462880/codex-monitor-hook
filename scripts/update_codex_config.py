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
import shutil
import sys
import tomllib
from pathlib import Path


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
MINIMAL_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "Stop",
    "SessionEnd",
)
HOOK_PROFILES = {
    "full": HOOK_EVENTS,
    "minimal": MINIMAL_HOOK_EVENTS,
}


def toml_string(value: str) -> str:
    """生成 TOML 字符串；Windows 路径优先用单引号避免反斜杠转义。"""

    if "'" not in value:
        return "'" + value + "'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def build_hook_block(
    relay_path: str,
    windows_python: str = "pythonw",
    hook_profile: str = "full",
) -> str:
    """生成 Codex 当前版本能识别的嵌套 hook 配置。"""

    if not relay_path:
        raise ValueError("relay path is empty")
    if hook_profile not in HOOK_PROFILES:
        raise ValueError(f"unknown hook profile: {hook_profile}")

    # Codex 在 Windows 上会通过 pwsh -Command 执行这段字符串。
    # 先调用隐藏窗口启动器，再由启动器调用 pythonw，可以避免外层
    # PowerShell 控制台闪现。调用绝对路径时必须使用 &。
    launcher_path = str(Path(relay_path).with_name("codex_hook_windows_launcher.ps1"))
    command_windows = toml_string(
        f'& "{launcher_path}" -Pythonw "{windows_python}" -Relay "{relay_path}"'
    )
    lines = [START_MARKER]
    for event_name in HOOK_PROFILES[hook_profile]:
        lines.extend(
            [
                "",
                f"[[hooks.{event_name}]]",
                "",
                f"[[hooks.{event_name}.hooks]]",
                'type = "command"',
                'command = "python3 ~/.codex_screen/codex_hook_relay.py"',
                f"commandWindows = {command_windows}",
                "timeout = 5",
            ]
        )
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
    relay_path: str,
    windows_python: str = "pythonw",
    hook_profile: str = "full",
) -> str:
    """更新 config 并返回备份路径。"""

    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_config(config_path)
    old_text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
    new_text = merge_hook_block(
        old_text,
        build_hook_block(relay_path, windows_python, hook_profile),
    )
    config_path.write_text(new_text.rstrip() + "\n", encoding="utf-8")
    tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    return backup_path


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4, 5):
        print(
            "usage: update_codex_config.py <config.toml> <relay.py> [pythonw] [full|minimal]",
            file=sys.stderr,
        )
        return 2

    windows_python = argv[3] if len(argv) == 4 else "pythonw"
    if len(argv) == 5:
        windows_python = argv[3]
    hook_profile = argv[4] if len(argv) == 5 else "full"
    backup_path = update_config(Path(argv[1]), argv[2], windows_python, hook_profile)
    print("BACKUP_PATH=" + backup_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
