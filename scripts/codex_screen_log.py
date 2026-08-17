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
MAX_LOG_BACKUPS = 2


def _rotate_if_large() -> None:
    """日志超过上限时轮转，并删除更旧的备份。"""

    try:
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size <= MAX_LOG_BYTES:
            return

        # 先从最旧的备份开始移动，避免覆盖仍有价值的最近日志。
        for index in range(MAX_LOG_BACKUPS, 0, -1):
            source = (
                LOG_PATH
                if index == 1
                else LOG_PATH.with_name(f"{LOG_PATH.name}.{index - 1}")
            )
            target = LOG_PATH.with_name(f"{LOG_PATH.name}.{index}")
            if not source.exists():
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            source.replace(target)

        for stale_path in LOG_PATH.parent.glob(f"{LOG_PATH.name}.*"):
            suffix = stale_path.name.rsplit(".", 1)[-1]
            if not suffix.isdigit() or int(suffix) <= MAX_LOG_BACKUPS:
                continue
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass

        # 清理旧版本留下的单文件备份，避免长期残留。
        legacy_path = LOG_PATH.with_suffix(".log.old")
        try:
            legacy_path.unlink()
        except FileNotFoundError:
            pass
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
