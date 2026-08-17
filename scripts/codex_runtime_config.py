#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relay 和 daemon 共享的运行时端口配置。

Windows 会把某些 TCP 端口划成 excluded range。端口在这个范围里时，
没有进程占用也会绑定失败，所以不能只依赖一个写死端口。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12688
# 最后的 0 表示让操作系统自动分配一个可用临时端口。
DEFAULT_PORT_CANDIDATES = (12688, 27688, 27689, 27690, 28688, 29688, 0)
PORT_ENV = "CODEX_SCREEN_PORT"
PORT_CANDIDATES_ENV = "CODEX_SCREEN_PORT_CANDIDATES"
RUNTIME_CONFIG_NAME = "runtime.json"


def runtime_config_path(base_dir: Path) -> Path:
    return base_dir / RUNTIME_CONFIG_NAME


def get_candidate_ports() -> List[int]:
    """返回 daemon 可尝试监听的端口列表，保留 12688 作为首选兼容旧安装。"""

    ports: List[int] = []
    env_port = _parse_port(os.environ.get(PORT_ENV), allow_zero=True)
    if env_port is not None:
        ports.append(env_port)

    raw_candidates = os.environ.get(PORT_CANDIDATES_ENV)
    if raw_candidates:
        ports.extend(_parse_port_list(raw_candidates, allow_zero=True))
    else:
        ports.extend(DEFAULT_PORT_CANDIDATES)
    return _dedupe_ports(ports)


def read_active_port(base_dir: Path) -> Optional[int]:
    """读取 daemon 最近一次成功监听的端口；文件损坏时直接忽略。"""

    try:
        data = json.loads(runtime_config_path(base_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _parse_port(data.get("port"))


def write_active_port(base_dir: Path, host: str, port: int, pid: int) -> None:
    """daemon 成功监听后写入实际端口，让后续 relay 不再猜端口。"""

    data = {
        "host": host,
        "port": int(port),
        "pid": int(pid),
        "updated_at": time.time(),
    }
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        runtime_config_path(base_dir).write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def relay_candidate_ports(base_dir: Path) -> List[int]:
    """relay 先试 daemon 上次成功端口，再试候选端口。"""

    ports: List[int] = []
    active_port = read_active_port(base_dir)
    if active_port is not None:
        ports.append(active_port)
    ports.extend(get_candidate_ports())
    return _dedupe_ports(ports)


def _parse_port_list(raw: str, allow_zero: bool = False) -> List[int]:
    ports: List[int] = []
    for part in raw.replace(";", ",").split(","):
        port = _parse_port(part.strip(), allow_zero=allow_zero)
        if port is not None:
            ports.append(port)
    return ports


def _parse_port(value: Any, allow_zero: bool = False) -> Optional[int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    minimum = 0 if allow_zero else 1
    return port if minimum <= port <= 65535 else None


def _dedupe_ports(values: Iterable[int]) -> List[int]:
    seen = set()
    ports: List[int] = []
    for value in values:
        port = _parse_port(value)
        if port is None or port in seen:
            continue
        seen.add(port)
        ports.append(port)
    return ports
