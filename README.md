# codex-monitor-hook

Python hook installer for showing Codex quota text on a custom HID screen. Legacy hook-based
status forwarding remains in the source for optional restoration.

Use this Codex prompt to install:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/q8462880/codex-monitor-hook/refs/heads/master/.codex/INSTALL.md
```

User requirements:

- Windows with a standard Python installation, or macOS with `python3`
- Codex with hook support
- Network access to GitHub
- The target HID screen device

The Windows installer uses the user's existing `pythonw.exe`; the macOS installer uses
`python3`. Both install the single `hidapi`
package if it is missing, and runs the relay/daemon as Python scripts. A conditional
Python bootstrap handles Windows Codex runners that place Hook JSON before the relay
script; it does not affect normal Python commands. Codex Desktop evaluates its Windows
Hook command with PowerShell, so the generated command uses only its `&` call operator
to invoke `pythonw.exe`; the project does not install a PowerShell launcher. It does not
install a separate Python runtime or require Git.

The default quota-only installation checks Codex `hooks/list` and expects `loaded=3 executable=3`
after the user reviews and trusts the hooks in Settings > Hooks. It installs `SessionStart`,
`UserPromptSubmit`, and `SessionEnd`; tool, subagent, permission, and stop status hooks are not
registered.

Quota behavior:

- API key login is supported for Codex usage, but usually cannot expose ChatGPT account rate limits. The daemon still probes the quota interface once and then hides unavailable quota fields while retaining the fallback text.
- 额度查询会通过隐藏的、复用的 app-server 请求 `account/rateLimits/read`。如果存在隔离的 ChatGPT `auth.json`，它只会传给该私有 app-server；否则继续使用主 `CODEX_HOME`。无论本地登录模式为何都会实际探测一次；账户无权限时，设备隐藏额度区域并保持在线状态，原因只写入本地日志。
- Quota errors are recorded in `~/.codex_screen/codex_screen.log` without logging credentials.

On macOS, when the desktop client uses an API key or a third-party endpoint, place a valid
ChatGPT `auth.json` in `~/.codex_screen/quota-codex-home/`. The quota reader prefers that
isolated home, so the desktop client cannot replace its credentials. Account-switching tools
that replace the main `~/.codex/auth.json` with a ChatGPT login are automatically synchronized
on the next quota refresh; API-key mode does not overwrite the saved ChatGPT credential.
Windows preserves its original behavior and reads the main `CODEX_HOME` by default. Set
`CODEX_SCREEN_QUOTA_CODEX_HOME` on either platform only when an explicit isolated location is
required.
