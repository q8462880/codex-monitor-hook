#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 Codex Hook 的 Python 启动兼容层安装到当前解释器用户目录。"""

from __future__ import annotations

import site
import sys
from pathlib import Path


PTH_FILE_NAME = "codex_monitor_hook_bootstrap.pth"


def install_bootstrap(runtime_dir: Path) -> Path:
    """让当前 Python 在启动时可找到项目的条件化 bootstrap 模块。"""

    user_site = Path(site.getusersitepackages())
    user_site.mkdir(parents=True, exist_ok=True)
    pth_path = user_site / PTH_FILE_NAME
    pth_path.write_text(
        f"{runtime_dir}\nimport codex_hook_bootstrap\n",
        encoding="utf-8",
    )
    return pth_path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: install_python_hook_bootstrap.py <runtime-dir>", file=sys.stderr)
        return 2

    pth_path = install_bootstrap(Path(argv[1]).resolve())
    print(f"PYTHON_HOOK_BOOTSTRAP={pth_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
