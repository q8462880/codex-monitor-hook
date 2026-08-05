---
name: codex-monitor-hook
description: Deploy and maintain a local Codex hook relay plus Python HID daemon for showing Codex session state and quota text on a custom HID screen. Use when installing, updating, or troubleshooting the codex-monitor-hook project, ~/.codex_screen files, localhost:12688 relay, hidapi dependency, or Codex hook configuration.
---

# Codex Monitor Hook

## Project Layout

- `scripts/codex_hook_relay.py`: Codex hook relay. It only reads hook stdin and forwards JSON to `127.0.0.1:12688`; it must not import or touch USB/HID APIs.
- `scripts/codex_screen_daemon.py`: Long-running local daemon. It owns the HID device, listens on TCP, reconnects on USB unplug/replug, serializes messages through a queue, and exits after idle timeout.
- `scripts/codex_quota_client.py`: Optional Codex app-server quota reader. It keeps one hidden stdio app-server connection for the daemon lifetime, reuses it for refreshes, and reconnects after a timeout or process exit; failures fall back silently.
- `scripts/codex_screen_log.py`: Shared file logger. It writes short lifecycle logs to `~/.codex_screen/codex_screen.log` without recording full prompts.
- `scripts/update_codex_config.py`: Installer helper that backs up `config.toml`, installs the hook marker block, and validates TOML syntax.
- `scripts/install.ps1`: Windows-friendly installer that copies runtime Python files, backs up Codex `config.toml`, and installs hook config.
- `references/codex_config_hooks.toml`: Hook snippet that can be appended to Codex `config.toml` after installation.

## Required Architecture

Keep this architecture unchanged unless the user explicitly changes it:

`Codex Hook -> codex_hook_relay.py -> 127.0.0.1:12688 TCP -> codex_screen_daemon.py -> HID device`

Rules:

- The hook relay only performs local socket forwarding and daemon spawn/retry.
- Inline hooks must use `[[hooks.Event]]` plus `[[hooks.Event.hooks]]`; the command handler uses string `command` and `commandWindows` values. Keep hooks synchronous and let the relay return quickly after socket forwarding/spawn.
- The daemon is the only process allowed to open the HID device.
- The daemon exits immediately when the port is already occupied, providing single-instance protection.
- Hardware constants stay in the daemon configuration block near the top of the file.
- `HID_PROTOCOL_READY` is `True`; the daemon writes the firmware `0x24/0x01` binary Codex Monitor state frame and still processes hook events when the HID device is temporarily disconnected.
- Hook status events include `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, `PreToolUse`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`, and `SessionEnd`.
- The daemon uses `scripts/codex_state_manager.py` to track session and turn lifecycles.
  `UserPromptSubmit` starts a turn, tool/permission/compaction/subagent hooks update the
  turn's detailed state, matching `Stop` ends only that turn, and `SessionEnd` ends the
  whole session. Stale events from another session or an already stopped turn are ignored.
- Runtime dependency is standard Python plus `hidapi` only.
- Real quota lookup is optional. It requires a Codex app-server auth mode that can read `account/rateLimits/read`; API-key-only auth may return `chatgpt authentication required`.

## Deployment Workflow

For a Superpowers-style remote install prompt, publish this repository on GitHub and tell Codex:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/<OWNER>/codex-monitor-hook/refs/heads/main/.codex/INSTALL.md
```

1. Install dependency:

   ```powershell
   python -m pip install hidapi
   ```

2. Copy runtime files and install hooks:

   ```powershell
   .\scripts\install.ps1
   ```

   The installer backs up the previous Codex config to `config.toml.codex-monitor-hook.<timestamp>.bak` before writing hook settings.

3. Verify the install:

   ```powershell
   python -m py_compile $HOME\.codex_screen\codex_hook_relay.py $HOME\.codex_screen\codex_screen_daemon.py $HOME\.codex_screen\codex_quota_client.py
   python $HOME\.codex_screen\codex_screen_daemon.py --self-test
   ```

## Quota Display

The daemon tries to refresh Codex quota text every 180 seconds. Set these environment variables only when needed:

- `CODEX_SCREEN_ENABLE_CODEX_QUOTA=0`: disable real Codex quota lookup.
- `CODEX_SCREEN_QUOTA_REFRESH_SEC=60`: change refresh interval.
- `CODEX_SCREEN_CODEX_EXE=C:\path\to\codex.exe`: force a specific Codex executable.
- `CODEX_SCREEN_QUOTA_TEXT="quota: --"`: fallback text when app-server quota lookup is unavailable.

The quota reader starts one hidden `codex.exe app-server --stdio` when the first refresh is needed.
Later refreshes reuse the same process instead of repeatedly creating and closing it. A timeout or
process exit closes the broken connection, and the next refresh reconnects. When the screen daemon
stops, it waits for the quota thread to close the app-server child.

## Installer Options

- `.\scripts\install.ps1 -SkipPipInstall`: skip `python -m pip install hidapi`.
- `.\scripts\install.ps1 -SkipConfigUpdate`: copy runtime files without changing Codex `config.toml`.
- `.\scripts\install.ps1 -HookProfile minimal`: install only `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, `Stop`, and `SessionEnd`; this reduces Windows PowerShell hook launches when performance matters.
- `.\scripts\install.ps1 -CodexHome D:\tmp\codex-home`: write config under a custom Codex home, useful for testing.

On Windows the installer writes a hidden-console launcher plus `pythonw.exe` in `commandWindows`. The launcher hides the
PowerShell console created by Codex before forwarding stdin to the relay.

## Runtime Logs and Hook Smoke Test

Runtime logs are written to:

```powershell
$HOME\.codex_screen\codex_screen.log
```

Use this local smoke test to confirm relay -> daemon works before testing a real Codex hook:

```powershell
Remove-Item $HOME\.codex_screen\codex_screen.log -ErrorAction SilentlyContinue
'{"hook_event_name":"SessionStart","session_id":"manual-test"}' | python $HOME\.codex_screen\codex_hook_relay.py
Get-Content $HOME\.codex_screen\codex_screen.log -Tail 80
```

Runtime log timestamps include milliseconds. Normal `received` and `state` lines include latency from relay start, so a warm daemon should normally be within a few to tens of milliseconds. The first event after idle timeout can take longer while the daemon process starts.

When testing real Codex hooks, keep the log open:

```powershell
Get-Content $HOME\.codex_screen\codex_screen.log -Wait -Tail 80
```

## Firmware Protocol Note

The daemon writes a 1024-byte firmware payload beginning with `0x24, 0x01, 0x01`
for Codex Monitor state. `hidapi.write()` receives one leading `report_id=0`
byte, so the actual host write is 1025 bytes while the USB Output Report remains
1024 bytes. The payload contains status, icon code, current/weekly usage
percentages and reset seconds; the firmware renders the status icon animation.
