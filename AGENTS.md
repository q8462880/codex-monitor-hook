# codex-monitor-hook 项目规则

## 沟通与注释

- 默认使用简体中文沟通。
- 新增或修改代码时，注释优先写简体中文。
- 注释要写给不熟悉这段代码的人看，避免只写缩写、黑话或只重复代码字面意思。
- 对下面几类代码必须补清楚注释：
  - HID / USB 设备参数，例如 VID、PID、Usage Page、Report Size。
  - Codex hook 事件到屏幕状态的映射。
  - 跨平台后台启动、端口单实例锁、USB 重连、消息队列顺序发送。
  - 当前只是占位协议、未来要等固件 UI/协议补齐的地方。
- 注释重点解释“为什么这样做”和“改错会影响什么”，不要写成流水账。

## 架构边界

- 固定链路：Codex Hook -> 平台 Python (`pythonw.exe` 或 `python3`) -> `127.0.0.1` 动态 TCP 端口 -> 平台 Python daemon -> HID 设备。
- `12688` 只是首选端口。daemon 绑定失败时自动尝试备用端口，最后允许操作系统分配临时端口；
  实际端口写入 `~/.codex_screen/runtime.json`，relay 必须读取该文件后再转发。
- Windows 用户使用已有标准 Python，macOS 用户使用已有 `python3`；对应安装脚本自动安装缺失的 `hidapi` 包。
- `scripts/codex_hook_relay.py` 只能做本地 Socket 转发和 daemon 拉起，禁止导入或操作 HID / USB。
- `scripts/codex_screen_daemon.py` 是唯一允许长期独占 HID 设备的进程。
- daemon 启动时如果所有候选端口都不可用必须退出；正常情况下端口由 daemon 单实例独占。
- 不需要系统开机自启，依靠 Codex `SessionStart` hook 自动拉起 daemon。
- 当前 Codex hooks 使用嵌套结构：`[[hooks.Event]]` 下再写 `[[hooks.Event.hooks]]`，命令处理器需要 `type = "command"` 和字符串形式的 `command` / `commandWindows`；relay 必须快速返回。
- 当前状态由 `scripts/codex_state_manager.py` 按 session/turn 生命周期管理：
  `SessionStart` 开启会话并回到 `IDLE`，`UserPromptSubmit` 开启一轮 turn 并进入 `THINKING`，
  `PreToolUse`、`PermissionRequest`、`PreCompact`、`SubagentStart` 等事件只在匹配的
  session/turn 内切换详细运行态；`Stop` 只结束对应 turn，`SessionEnd` 才结束整个 session。
  旧 session 或旧 turn 的迟到事件会被忽略，避免把当前对话错误切回 `IDLE`。
  最后一个 Hook 超过 2 分钟没有后续事件时回到专用 `READY` 状态。

## 依赖与配置

- Python 源码只使用标准库和 `hidapi`；Hook 配置不得通过 PowerShell 或 `.ps1` 启动器执行，macOS 使用 POSIX `command`。
- 所有硬件参数集中放在 daemon 文件顶部配置区，方便以后改 VID / PID / Usage / Report Size。
- 真实 Codex 额度查询只允许放在 daemon 侧或 daemon 调用的本地模块里，hook relay 仍然禁止做额度查询。
- 额度查询失败必须降级到 `CODEX_SCREEN_QUOTA_TEXT` 或 hook 事件里的额度文本，不能影响 HID 状态显示。
- 安装脚本写入 `config.toml` 前必须先备份，备份文件名要包含 `codex-monitor-hook` 和时间戳。
- 安装脚本写入 hooks 时必须使用明确标记块，重复运行只能替换旧标记块，不能无限追加重复配置。
- `references/codex_config_hooks.toml` 保留为人工参考片段；实际安装以 `scripts/install.ps1` 动态生成的路径为准。
- 安装脚本只负责复制运行文件和做本地验证，不做危险删除、不改 Git 历史、不写密钥。
- 运行日志统一写入 `~/.codex_screen/codex_screen.log`，日志只记录事件名、短 ID、连接状态、HID 状态，不记录完整 prompt、密钥或敏感正文。

## 代码可读性

- 优先保持代码直白，函数名和变量名要能说明用途。
- 为了满足文件行数限制，不允许把复杂逻辑硬压成难读的一行。
- 如果某段逻辑需要长注释才能解释清楚，优先拆成命名明确的小函数。
- 保持函数短小；修改 daemon 时尤其注意不要让单个函数承担连接、解析、状态更新和 HID 写入等多种职责。

## 验证要求

- 修改 Python 运行文件后，至少运行：

  ```powershell
  python -m py_compile scripts\codex_hook_relay.py scripts\codex_screen_daemon.py scripts\codex_quota_client.py scripts\update_codex_config.py
  python scripts\codex_screen_daemon.py --self-test
  ```

- 修改额度查询逻辑后，运行：

  ```powershell
  python -m unittest tests.test_codex_quota_client
  ```

- 修改 skill 元数据或目录结构后，运行：

  ```powershell
  python C:\Users\42194\.codex\skills\.system\skill-creator\scripts\quick_validate.py D:\code\codex-monitor-hook
  ```

- 修改 hook TOML 后，用 Python `tomllib` 做解析验证。
- 修改 PowerShell 脚本后，用 PowerShell Parser 做语法检查。
