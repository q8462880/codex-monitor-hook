---
name: codex-monitor-hook
description: Deploy and maintain a local Codex hook relay plus Python HID daemon for showing Codex session state and quota text on a custom HID screen. Use when installing, updating, or troubleshooting the codex-monitor-hook project, ~/.codex_screen files, localhost TCP relay, Python runtime, or Codex hook configuration.
---

# Codex Monitor Hook

## Project Layout

- `scripts/codex_hook_relay.py`: Codex hook relay. It only reads hook stdin and forwards JSON to the daemon's negotiated localhost TCP port; it must not import or touch USB/HID APIs.
- `scripts/codex_screen_daemon.py`: Long-running local daemon script. It owns the HID device, listens on TCP, reconnects on USB unplug/replug, serializes messages through a queue, and exits after idle timeout.
- `scripts/codex_quota_client.py`: Optional Codex app-server quota reader. It keeps one hidden stdio app-server connection for the daemon lifetime, reuses it for refreshes, and reconnects after a timeout or process exit; failures fall back silently.
- `scripts/codex_screen_log.py`: Shared file logger. It writes short lifecycle logs to `~/.codex_screen/codex_screen.log` without recording full prompts.
- `scripts/update_codex_config.py`: Installer helper that backs up `config.toml`, installs the hook marker block, and validates TOML syntax.
- `scripts/install.ps1`: Windows installer that copies Python scripts, checks `pythonw.exe` and `hidapi`, backs up Codex `config.toml`, and installs Windows Hook config.
- `scripts/install.sh`: macOS/POSIX installer that uses `python3`, installs `hidapi`, backs up Codex `config.toml`, and installs a POSIX Hook command.
- `requirements.txt`: Python runtime dependency list; currently only `hidapi`.
- `references/codex_config_hooks.toml`: Hook snippet that can be appended to Codex `config.toml` after installation.

## Required Architecture

Keep this architecture unchanged unless the user explicitly changes it:

`Codex Hook -> pythonw.exe codex_hook_relay.py -> localhost negotiated TCP port -> pythonw.exe codex_screen_daemon.py -> HID device`

Rules:

- The hook relay only performs local socket forwarding and daemon spawn/retry.
- Inline hooks must use `[[hooks.Event]]` plus `[[hooks.Event.hooks]]`; the command handler uses string `command` and `commandWindows` values. Keep hooks synchronous and let the relay return quickly after socket forwarding/spawn.
- The daemon is the only process allowed to open the HID device.
- `12688` is the preferred port. If Windows reserves it or another program uses it, the daemon tries fallback ports and finally asks the OS for a temporary port.
- The daemon writes the selected port to `~/.codex_screen/runtime.json`; relay reads that file before connecting.
- The daemon exits when its instance lock is unavailable or all port candidates fail, providing single-instance protection.
- Hardware constants stay in the daemon configuration block near the top of the file.
- `HID_PROTOCOL_READY` is `True`; the daemon writes the firmware `0x24/0x01` binary Codex Monitor state frame and still processes hook events when the HID device is temporarily disconnected.
- HID state frames set `flags.bit0` (`STATUS_VALID`). Five-second link heartbeats clear this bit and use invalid status/quota fields, so firmware must refresh only its offline timer without treating `THINKING` or another business state as a repeated event.
- The default quota profile registers `SessionStart`, `UserPromptSubmit`, and `SessionEnd`.
  The full list of legacy status events remains available through `-HookProfile full`.
- In quota mode, relay sends an internal throttled quota-refresh request and does not cache
  or forward session/turn status signals. The old status path is retained but disabled by default.
  Set `CODEX_SCREEN_ENABLE_STATUS_HOOKS=1` before installing `-HookProfile full` to restore it.
- Relay keeps one bounded cache per `session_id`. `SessionStart`, PID, timestamps,
  and background tool events never select a session. Until a desktop bridge exists,
  a successful `UserPromptSubmit` is treated as the user's latest action and sets
  that event's `session_id` as `active_session_id`; this makes normal single-window
  use work without guessing from background activity.
- Use `--set-active-session <session_id>` for a future desktop bridge or test tool,
  `--show-active-session` to inspect the current selection, and
  `--clear-active-session` to stop forwarding until another user prompt or explicit
  selection arrives.
- The legacy `Stop` and `SessionEnd` behavior remains in the full status profile only.
- The daemon uses `scripts/codex_state_manager.py` to track session and turn lifecycles.
  `UserPromptSubmit` starts a turn, tool/permission/compaction/subagent hooks update the
  turn's detailed state, matching `Stop` ends only that turn, and `SessionEnd` ends the
  whole session. Stale events from another session or an already stopped turn are ignored.
- Windows users need an existing standard Python installation; macOS users need `python3`. The Windows installer uses its
  `pythonw.exe` and automatically installs the single `hidapi` package if it is missing.
- The Hook command never calls PowerShell or a `.ps1` launcher. `pythonw.exe` is used so
  the Hook process and the background daemon do not create a console window.
- Real quota lookup is optional. The daemon always probes `account/rateLimits/read` regardless of local auth mode, because compatible or managed sign-in modes can differ. If the service rejects the request, it hides the device quota fields and records the short reason in the local log.
- Official ChatGPT login is queried through one reused hidden app-server connection. On macOS,
  when `~/.codex_screen/quota-codex-home/auth.json` exists, the quota reader uses that isolated
  `CODEX_HOME` so a desktop API-key login cannot overwrite it; a valid ChatGPT login written to
  the main auth file is synchronized on refresh. Windows preserves its normal main `CODEX_HOME`
  behavior unless `CODEX_SCREEN_QUOTA_CODEX_HOME` is explicitly set. API-key-only auth may return
  `chatgpt authentication required` and will then leave quota values hidden.
- A change to `auth.json` closes the reused app-server session before the next query, so account switchers can refresh the quota without a machine restart. `SessionEnd` also closes the daemon cleanly when Codex exits normally.

## Deployment Workflow

For a Superpowers-style remote install prompt, publish this repository on GitHub and tell Codex:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/q8462880/codex-monitor-hook/refs/heads/master/.codex/INSTALL.md
```

1. Copy Python runtime files and install hooks:

   ```powershell
   .\scripts\install.ps1
   ```

   The installer backs up the previous Codex config to `config.toml.codex-monitor-hook.<timestamp>.bak` before writing hook settings.

2. Verify the install:

   ```powershell
   & python $HOME\.codex_screen\codex_screen_daemon.py --self-test
   ```

## Quota Display

The daemon tries to refresh Codex quota text every 180 seconds. Set these environment variables only when needed:

- `CODEX_SCREEN_ENABLE_CODEX_QUOTA=0`: disable real Codex quota lookup.
- `CODEX_SCREEN_QUOTA_REFRESH_SEC=60`: change refresh interval.
- `CODEX_SCREEN_CODEX_EXE=C:\path\to\codex.exe`: force a specific Codex executable.
- `CODEX_SCREEN_QUOTA_TEXT="quota: --"`: fallback text when app-server quota lookup is unavailable.
- `CODEX_SCREEN_QUOTA_CODEX_HOME=/path/to/home`: override the isolated quota auth directory.

The quota reader starts one hidden `codex.exe app-server --stdio` when the first refresh is needed.
Later refreshes reuse the same process instead of repeatedly creating and closing it. A timeout or
process exit closes the broken connection, and the next refresh reconnects. When the screen daemon
stops, it waits for the quota thread to close the app-server child.

Quota failures are recorded in `~/.codex_screen/codex_screen.log` with a `[quota]` prefix. The
daemon returns any stale display state to `READY` after 2 minutes without a new hook event. Set
`CODEX_SCREEN_HOOK_STALE_TIMEOUT_SEC=0` to disable that fallback.

The relay cache is bounded to 32 sessions. The log rotates at 1 MiB and keeps two backups.

## Installer Options

- `.\scripts\install.ps1 -SkipConfigUpdate`: copy runtime files without changing Codex `config.toml`.
- `.\scripts\install.ps1 -HookProfile quota`: default; install `SessionStart`, `UserPromptSubmit`, and `SessionEnd` for quota refresh and clean shutdown.
- `.\scripts\install.ps1 -HookProfile full`: restore all legacy status hooks.
- `.\scripts\install.ps1 -HookProfile minimal`: retain the previous reduced status profile for compatibility.
- `.\scripts\install.ps1 -CodexHome D:\tmp\codex-home`: write config under a custom Codex home, useful for testing.

On Windows the installer writes a direct `pythonw.exe "<path>\codex_hook_relay.py"`
commandWindows entry. It does not install or invoke the old PowerShell launcher or
packaged exe runtime.

## Runtime Logs and Hook Smoke Test

Runtime logs are written to:

```powershell
$HOME\.codex_screen\codex_screen.log
```

Use this local smoke test to confirm relay -> daemon works before testing a real Codex hook:

```powershell
Remove-Item $HOME\.codex_screen\codex_screen.log -ErrorAction SilentlyContinue
'{"hook_event_name":"SessionStart","session_id":"manual-test"}' | & python $HOME\.codex_screen\codex_hook_relay.py
Get-Content $HOME\.codex_screen\codex_screen.log -Tail 80
```

Runtime log timestamps include milliseconds. Normal `received` and `state` lines include latency from relay start, so a warm daemon should normally be within a few to tens of milliseconds. The first event after idle timeout can take longer while the daemon process starts.

When testing real Codex hooks, keep the log open:

```powershell
Get-Content $HOME\.codex_screen\codex_screen.log -Wait -Tail 80
```

Check whether Codex loaded, enabled, and trusted the hooks:

```powershell
& python $HOME\.codex_screen\codex_screen_daemon.py --diagnose-hooks $PWD --expected-hook-count 2
```

The expected quota-profile result is `loaded=3 executable=3`; full-profile remains
`loaded=11 executable=11`. Visible hooks marked `untrusted`
or `modified` are not executable until the user reviews and trusts them in Settings > Hooks.

## Firmware Protocol Note

The daemon opens the Codex Micro-compatible device at `VID=0x303A`, `PID=0x8360`,
selecting the daemon-only vendor-defined collection `usage_page=0xFF01`, `usage=0x01`
(the daemon-only `MI_02` interface). The collection is Output-only and uses
Report ID `0x07`: `hidapi.write()` receives 1024 bytes total, with `0x07` as the
first byte and the first 1023 bytes of the firmware's 1024-byte Monitor payload
following it. The payload begins with `0x24, 0x01, 0x01` and contains status, icon
code, current/weekly usage percentages and reset seconds; the firmware restores
the final missing byte as zero before parsing.
