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
after the user reviews and trusts the hooks in Settings > Hooks. It installs only `SessionStart`
and `UserPromptSubmit`; tool, subagent, permission, stop, and session-end status hooks are not
registered.

Quota behavior:

- API key login is supported for Codex usage, but it does not expose ChatGPT account rate limits. The daemon skips the quota app-server in this mode and keeps the configured fallback text.
- 额度查询会使用与 Codex 相同的 `CODEX_HOME` 通过隐藏的、复用的 app-server 请求 `account/rateLimits/read`。无论本地登录模式为何都会实际探测一次；账户无权限时，设备隐藏额度区域并保持在线状态，原因只写入本地日志。
- Quota errors are recorded in `~/.codex_screen/codex_screen.log` without logging credentials.
