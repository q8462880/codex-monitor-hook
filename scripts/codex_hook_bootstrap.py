#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 Windows Codex Hook 把事件 JSON 放到 Python 脚本前的兼容层。

模块由用户级 ``.pth`` 在 Python 初始化阶段导入。普通 Python 启动没有
Codex Hook JSON 时立即返回；只有命令行同时包含 Hook JSON 和本项目 relay
脚本时，才在 Python 尝试把 JSON 当脚本文件打开前转交给真正的 relay。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple


RELAY_SCRIPT_NAME = "codex_hook_relay.py"
DRY_RUN_ENV = "CODEX_HOOK_BOOTSTRAP_DRY_RUN"


def _hook_invocation(argv: list[str]) -> Optional[Tuple[str, str]]:
    """识别被 Windows runner 改写顺序的 Codex Hook 调用。"""

    event_json = ""
    relay_path = ""
    for value in argv:
        text = str(value or "")
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict) and event.get("hook_event_name"):
            event_json = text
        if Path(text).name.lower() == RELAY_SCRIPT_NAME:
            relay_path = text
    return (event_json, relay_path) if event_json and relay_path else None


def _run_relay(event_json: str, relay_path: str) -> None:
    """在 site 初始化阶段执行 relay，阻止解释器把 JSON 当作脚本打开。"""

    if os.environ.get(DRY_RUN_ENV) == "1":
        print("codex_hook_bootstrap OK", flush=True)
        os._exit(0)

    relay_dir = str(Path(relay_path).resolve().parent)
    if relay_dir not in sys.path:
        sys.path.insert(0, relay_dir)
    import codex_hook_relay

    exit_code = codex_hook_relay.main([event_json, relay_path])
    os._exit(exit_code)


def run() -> None:
    """供 .pth 导入；不匹配时零副作用返回。"""

    invocation = _hook_invocation(sys.argv)
    if invocation is not None:
        _run_relay(*invocation)


run()
