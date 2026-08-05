#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex-monitor-hook 的简单文件日志。

这个模块只用标准库，relay 和 daemon 都可以导入。日志文件放在
~/.codex_screen/codex_screen.log，方便用户直接用 PowerShell 查看。
"""

from __future__ import annotations

import os
import time
from pathlib import Path


BASE_DIR = Path.home() / ".codex_screen"
LOG_PATH = BASE_DIR / "codex_screen.log"
MAX_LOG_BYTES = 1024 * 1024


def _rotate_if_large() -> None:
    """日志超过 1MB 时改名为 .old，避免长期运行把文件撑得太大。"""

    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            old_path = LOG_PATH.with_suffix(".log.old")
            try:
                old_path.unlink()
            except FileNotFoundError:
                pass
            LOG_PATH.replace(old_path)
    except OSError:
        pass


def log_line(component: str, message: str) -> None:
    """追加一行日志；失败时静默，不能影响 hook 或 daemon 主流程。"""

    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_large()
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        stamp += f".{int(now * 1000) % 1000:03d}"
        line = f"{stamp} [{component}] pid={os.getpid()} {message}\n"
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(line)
    except OSError:
        pass
