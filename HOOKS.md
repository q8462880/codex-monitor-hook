# OpenAI Codex CLI Hook 事件清单

> 当前默认安装为额度模式，只注册 `SessionStart` 和 `UserPromptSubmit` 来启动 daemon
> 并请求额度刷新。下文的状态映射属于保留的 legacy `full` profile，不会默认发送到 HID。
## 事件对照表
| Hook 事件 | 触发时机（用户视角） | 常用用途 |
| ---- | ---- | ---- |
| SessionStart | 新开对话 / 加载历史对话 | 会话初始化、记录对话启动 |
| UserPromptSubmit | 你敲回车发送提问，AI 还没开始思考 | 修改 / 拦截你的问题、日志记录输入 |
| PermissionRequest | AI 准备执行代码 / 读写文件，弹出权限确认前 | 自动允许 / 阻止高危操作 |
| PreToolUse | AI 准备运行命令、读写文件（操作还没执行） | 拦截危险命令、修改待执行指令 |
| PostToolUse | AI 执行完命令，拿到运行结果后 | 获取命令输出、修改返回给 AI 的数据 |
| PreCompact | 对话太长，Codex 准备自动精简历史上下文 | 阻止会话被压缩、备份原始对话 |
| PostCompact | 对话历史精简完成 | 监控上下文压缩、保存精简前后内容 |
| SubagentStart | AI 开启子任务、拆分工作并行处理 | 监听 AI 启动并行任务 |
| SubagentStop | AI 的子任务执行完毕 | 回收子任务信息、资源清理 |
| Stop | 一轮问答结束（AI 给出最终回复） | 保存本轮对话、统计 token |
| SessionEnd | 彻底关闭当前对话窗口 | 保存完整会话、清理资源 |

## 事件生命周期流程

SessionStart 【开启对话】
├─ 发送提问 → UserPromptSubmit
│ ├─ AI 要执行工具 → PreToolUse
│ │ ├─ 需要权限弹窗 → PermissionRequest
│ │ ├─ 工具运行结束 → PostToolUse
│ ├─ AI 开启并行子任务 → SubagentStart → SubagentStop
│ └─ AI 回答完成 → Stop
├─ 对话过长触发精简 → PreCompact → PostCompact
└─ 关闭对话窗口 → SessionEnd【对话结束】

## 屏幕状态生命周期

- `SessionStart`：创建或恢复 session，屏幕回到 `IDLE`，不代表一轮问答开始。
- `UserPromptSubmit`：创建当前 session 的新 turn，进入 `THINKING`。
- `PreToolUse` / `PermissionRequest` / `PreCompact` / `SubagentStart`：只更新当前 turn 的详细运行状态。
- `PostToolUse` / `PostCompact` / `SubagentStop`：回到当前 turn 的后续运行状态，通常是 `THINKING`；子代理结束时恢复进入子代理前的状态。
- `Stop`：只结束匹配 `session_id + turn_id` 的一轮问答，屏幕回到 `IDLE`，session 本身仍然存在。
- `SessionEnd`：结束整个 session。旧 session 或旧 turn 的迟到事件不会覆盖当前屏幕状态。

## 并发对话显示规则

- relay 为每个 `session_id` 保存独立缓存；`turn_id` 和 `tool_use_id` 只在所属
  session 内校验，不能跨 session 判断归属。
- 普通 `SessionStart`、后台工具事件和最新到达事件都不会自动成为桌面当前对话。
- 当前阶段把成功的 `UserPromptSubmit` 视为“用户最后一次明确操作”，自动把它的
  `session_id` 填入 `active_session_id`：

  ```text
  UserPromptSubmit(session_id=A)
    -> active_session_id=A
    -> A 后续状态允许发送到 HID
  ```

- 如果需要桌面桥接或手工切换，仍可以使用独立输入接口：

  ```powershell
  & python $HOME\.codex_screen\codex_hook_relay.py --set-active-session <session_id>
  & python $HOME\.codex_screen\codex_hook_relay.py --show-active-session
  & python $HOME\.codex_screen\codex_hook_relay.py --clear-active-session
  ```

- 切换时 relay 会用目标 session 的缓存重放既有 Hook 事件；没有缓存时不伪造
  后台状态，并记录 `UNKNOWN`。
- Codex Hook 没有可靠的桌面“当前选中对话”事件，因此这个方案只能把
  `UserPromptSubmit` 当作用户动作信号。用户只切换对话但不提交提示词时，
  relay 无法知道当前选中项，只能继续显示上一次已选 session 的状态。
- `Stop` Hook 可以监听用户停止，但当前 daemon 契约把它显示为 `IDLE`；`READY`
  仍由无新 Hook 的超时机制产生。要实现“Stop 后立即 READY”需要额外修改 daemon
  的状态事件处理，不在本次 relay-only 改动范围内。

## Hook 到屏幕状态映射

Codex Monitor 当前由 `scripts/codex_state_manager.py` 按
`session_id + turn_id` 过滤事件，再把接受的事件发送给 daemon 和固件。
状态不是由 hook 脚本直接绘制，hook 只负责转发事件。

| Hook 事件 | 屏幕状态 | 触发含义 |
| ---- | ---- | ---- |
| `SessionStart` | `IDLE` | 会话创建或恢复，等待用户输入 |
| `UserPromptSubmit` | `THINKING` | 用户提交新一轮提示词，当前 turn 开始 |
| `PermissionRequest` | `WAIT_PERM` | Codex 等待用户确认权限 |
| `PreToolUse` | `EXECUTING` | 即将执行工具、命令或文件操作 |
| `PostToolUse` | `THINKING` | 工具返回后继续分析当前结果 |
| `PreCompact` | `COMPACTING` | 即将压缩过长的上下文 |
| `PostCompact` | `THINKING` | 上下文压缩完成，继续当前 turn |
| `SubagentStart` | `SUBAGENT` | 当前 turn 启动子任务 |
| `SubagentStop` | 恢复子任务前状态 | 子任务结束，恢复进入子任务前的状态 |
| `Stop` | `IDLE` | 当前 `session_id + turn_id` 的一轮问答完成 |
| `SessionEnd` | `IDLE` | 整个 session 关闭 |
| 2 分钟无新 Hook | `READY` | 缺失后续事件时回到初始页面 |

## 数据保留策略

- `~/.codex_screen/codex_screen.log` 超过 1 MiB 后轮转为 `.1`、`.2`，
  更旧日志自动删除。
- `codex_relay_state.json` 最多保留 32 个 session；每个 session 最多保留
  64 个去重 key 和 32 个已结束 turn，避免并发对话长期增长。
- 缓存只保存状态和短 ID，不保存完整 prompt；事件去重使用哈希，不把事件正文写入
  持久化文件。

### 当前状态集合

协议状态码定义在固件的
`packages/third-party/cherryusb/screen_hid_protocol.h`：

| 状态 | 状态码 | 屏幕文字 | 是否动态 |
| ---- | ---- | ---- | ---- |
| `IDLE` | `0x00` | `Idle` | 否 |
| `THINKING` | `0x01` | `Thinking` | 是 |
| `EXECUTING` | `0x02` | `Executing` | 是 |
| `WAIT_PERM` | `0x03` | `Waiting` | 是 |
| `COMPACTING` | `0x04` | `Compacting` | 是 |
| `SUBAGENT` | `0x05` | `Subagent` | 是 |
| `READY` | `0x06` | `Ready` | 否 |
| `OFFLINE` | `0xE0` | `Offline` | 否 |
| `ERROR` | `0xE1` | `Check Codex` | 否 |

说明：

- `THINKING` 由 `UserPromptSubmit`、`PostToolUse`、`PostCompact` 等继续运行事件触发，
  不是固定的假状态。
- `EXECUTING` 主要对应 `PreToolUse`，表示即将执行工具，不表示工具已经完成。
- `WAIT_PERM` 对应 `PermissionRequest`，权限确认完成后通常会回到后续运行状态。
- `SUBAGENT` 结束时恢复进入子任务前的状态，不一定固定回到 `THINKING`。
- `Stop` 必须匹配当前 turn；缺少 `turn_id` 或来自旧 session/旧 turn 的结束事件会被忽略，
  防止旧事件把正在运行的对话错误切回 `IDLE`。
- `OFFLINE` 和 `ERROR` 不是 Codex hook 事件状态，而是固件根据 HID 超时或异常链路显示。
- 当前屏幕使用真实状态名，不再使用 `Divining` 这类占位文字。

## 当前状态图实现

当前固件没有从主题包加载 Codex 状态图片，状态图标由
`ui/screens/codex-monitor/widgets/codex_monitor_status_widget.c` 使用 LVGL
绘制圆形、矩形和透明度变化生成。`icon_code` 字段目前保留在协议结构中，但当前绘制逻辑尚未按
`icon_code` 选择外部图片。

当前动画参数定义在
`ui/screens/codex-monitor/defs/codex_monitor_defs.h`：

- 动态状态图标：每个状态 3 帧，帧间隔 `180ms`。
- 动态状态文字末尾的点：3 帧循环，帧间隔 `350ms`。
- 动态状态包括 `THINKING`、`EXECUTING`、`WAIT_PERM`、`COMPACTING`、`SUBAGENT`。
- `IDLE`、`OFFLINE`、`ERROR` 当前为静态图标。
- 三个点当前由文字控件动态追加，不需要图片文件。

## 后续替换状态图片的资源要求

如果后续把当前矢量绘制替换成图片资源，建议按下面规格制作，避免圆屏边缘裁切和透明背景问题：

- 源文件格式：`PNG`、`RGBA`，保留透明通道；不要使用 `JPG`，否则会产生黑色或实色背景。
- 建议单帧尺寸：`180 x 132 px`，对应当前状态图标控件区域。
- 图片内容应在画布中心绘制，四周保留透明边距；不要把整张 `360 x 360` 背景放进状态图文件。
- 文件名建议：`codex_monitor_<status>_<frame>.png`，帧号从 `00` 开始，例如
  `codex_monitor_thinking_00.png`。
- 图片必须再转换为项目实际使用的 LVGL/主题资源格式后才能进入固件；当前 widget
  不支持直接读取 PNG 文件，需要同步增加资源加载和按帧切换代码。

### 建议资源数量

保持当前 3 帧动画策略时，需要：

- `THINKING`：3 张
- `EXECUTING`：3 张
- `WAIT_PERM`：3 张
- `COMPACTING`：3 张
- `SUBAGENT`：3 张
- `IDLE`：1 张
- `OFFLINE`：1 张
- `ERROR`：1 张
- 状态图合计：`18 张`

如果保留当前文字末尾的三个点，不需要额外图片；如果希望点也改成图片动画，再增加一组
共 `3 张` 的通用点动画，最终为 `21 张`。

建议目录和资源名：

```text
codex-monitor/
  codex_monitor_idle.png
  codex_monitor_offline.png
  codex_monitor_error.png
  codex_monitor_thinking_00.png
  codex_monitor_thinking_01.png
  codex_monitor_thinking_02.png
  codex_monitor_executing_00.png
  codex_monitor_executing_01.png
  codex_monitor_executing_02.png
  codex_monitor_wait_perm_00.png
  codex_monitor_wait_perm_01.png
  codex_monitor_wait_perm_02.png
  codex_monitor_compacting_00.png
  codex_monitor_compacting_01.png
  codex_monitor_compacting_02.png
  codex_monitor_subagent_00.png
  codex_monitor_subagent_01.png
  codex_monitor_subagent_02.png
```

后续实现图片化时，必须同时修改固件的资源注册、图片对象创建、状态到资源前缀的映射和
动画帧定时器；仅把 PNG 放进主题包不会自动替换当前 C 绘制图标。
