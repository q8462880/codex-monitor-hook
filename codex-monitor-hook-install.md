# Codex Monitor Hook Install

Follow these instructions to install or update `codex-monitor-hook` for the current Windows or macOS user.

This installer uses the user's existing standard Python installation. It does not install
a separate Python runtime or use PowerShell to execute Hooks. It automatically installs the
single `hidapi` package if the selected Python does not have it.

## Safety Rules

- Do not delete an existing Codex config.
- Do not edit USB or HID firmware files during install.
- Do not install a separate Python runtime, Git, or developer tools.
- Do not install system startup tasks.
- Do not run destructive git commands.
- Use Windows PowerShell on Windows, or a POSIX shell on macOS.
- Keep the default `quota` hook profile. Use `full` only when restoring the legacy status signal path.

## Install From OSS

1. Download and unpack the package zip.

   Windows PowerShell:

   ```powershell
   $Url = "https://angrymiao-diy.oss-cn-shenzhen.aliyuncs.com/setrt/battleyeR2/codex-monitor-hook.zip"
   $Target = Join-Path $HOME ".codex\codex-monitor-hook"
   $Parent = Split-Path -Parent $Target
   $ZipPath = Join-Path $env:TEMP "codex-monitor-hook.zip"
   $ExtractRoot = Join-Path $env:TEMP "codex-monitor-hook-install"
   $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

   New-Item -ItemType Directory -Force -Path $Parent | Out-Null

   if (Test-Path -LiteralPath $Target) {
       Move-Item -LiteralPath $Target -Destination "$Target.bak.$Stamp" -Force
   }

   Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
   Remove-Item -LiteralPath $ExtractRoot -Recurse -Force -ErrorAction SilentlyContinue

   Invoke-WebRequest -Uri $Url -OutFile $ZipPath
   Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractRoot -Force

   # 兼容两种发布包结构：
   # 1. 文件位于 codex-monitor-hook\ 目录内；
   # 2. 文件直接位于 zip 根目录。
   $PackageRoot = Join-Path $ExtractRoot "codex-monitor-hook"
   if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot "scripts\install.ps1"))) {
       $PackageRoot = $ExtractRoot
   }
   if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot "scripts\install.ps1"))) {
       throw "Invalid package: scripts\install.ps1 was not found."
   }

   New-Item -ItemType Directory -Force -Path $Target | Out-Null
   Get-ChildItem -LiteralPath $PackageRoot -Force |
       Move-Item -Destination $Target -Force
   ```

2. Install the Codex skill copy.

   Windows PowerShell:

   ```powershell
   $SkillDir = Join-Path $HOME ".codex\skills\codex-monitor-hook"
   New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
   Get-ChildItem -LiteralPath $Target -Force | Copy-Item -Destination $SkillDir -Recurse -Force
   ```

3. Install runtime files and hooks.

   Windows PowerShell:

   ```powershell
   Set-Location $Target
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -HookProfile quota
   ```

   The installer copies the Python relay, daemon, and support modules to `~/.codex_screen`.

   It checks the existing Python installation for both `python.exe` and its matching `pythonw.exe`.
   Missing `hidapi` is installed automatically with that same Python. Hook configuration calls
   `pythonw.exe` directly; it does not call PowerShell or the old `.ps1` launcher.
   It backs up `~/.codex/config.toml` before replacing this project hook block. Reinstall stops
   only Python processes whose command line points to this project's relay or daemon script.
   It also stops and removes legacy `codex_hook_relay.exe`, `codex_screen_daemon.exe`, and the
   old PowerShell launcher from `~/.codex_screen`. `12688` is only the preferred local port.
   If Windows has reserved it, the daemon automatically selects another port and records it in
   `~/.codex_screen/runtime.json`.

4. Verify installation.

   Windows PowerShell:

   ```powershell
   & python $HOME\.codex_screen\codex_screen_daemon.py --self-test
   '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' | & python $HOME\.codex_screen\codex_hook_relay.py
   Get-Content $HOME\.codex_screen\codex_screen.log -Tail 80
   ```

   A successful `UserPromptSubmit` automatically records its `session_id` as
   the active session. This is the current fallback for Desktop use because
   Hook does not expose a reliable event for simply clicking another
   conversation. Background events from other sessions remain cached and are
   logged with `forwarded=no`; they cannot take over the HID screen.

5. Restart Codex.

Codex must reload `config.toml` and discover the installed skill.

The standard Windows Python installation includes `pythonw.exe`. The installer refuses to
fall back to `python.exe`, because a console Python process can create a visible terminal window.

## macOS Install

On macOS, unpack the same package and run the POSIX installer from its directory:

```sh
chmod +x ./scripts/install.sh
./scripts/install.sh --hook-profile quota
```

It uses the existing `python3`, installs `hidapi` and (on Python 3.9/3.10) `tomli` if needed, copies runtime files to
`~/.codex_screen`, installs the skill, backs up `~/.codex/config.toml`, and writes POSIX
`command` Hook entries. It does not use PowerShell, `pythonw.exe`, or system startup tasks.
Set `CODEX_SCREEN_PYTHON=/path/to/python3` if Python is not on `PATH`.

When Codex uses a custom home, pass the same directory explicitly so the installer and
the restarted app read the same `config.toml`:

```sh
./scripts/install.sh --codex-home "$CODEX_HOME" --hook-profile quota
```

The installer prints elapsed seconds for dependency checks, runtime setup, config updates,
and the optional Codex hook diagnostic. To skip only the app-server diagnostic on a slow or
offline machine, add `--skip-hook-diagnostic`; config writing and hook-count verification still run.

Verify on macOS:

```sh
python3 "$HOME/.codex_screen/codex_screen_daemon.py" --self-test
printf '%s\n' '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' |
  python3 "$HOME/.codex_screen/codex_hook_relay.py"
tail -n 80 "$HOME/.codex_screen/codex_screen.log"
```

## Quota Login

- API key login can run Codex but may not have permission to read ChatGPT account rate limits. The daemon still probes the quota interface once; when the service rejects it, the device hides quota values and keeps its online status.
- Official ChatGPT account login is supported. It must be completed in Codex, and the daemon uses the same `CODEX_HOME` credentials. Restart Codex after logging in.
- Quota diagnostics are written to `~/.codex_screen/codex_screen.log` with a `[quota]` prefix.
- Any displayed state with no new hook event returns to `READY` after 2 minutes. Set `CODEX_SCREEN_HOOK_STALE_TIMEOUT_SEC=0` to disable this fallback.
- Restart Codex, then review and trust the hooks in Settings > Hooks. The default quota installation expects `[hook-check] loaded=3 executable=3`.

## Session Diagnostics

- `hook event=... session=...` means the Hook process reached relay.
- `cached event=... forwarded=no reason=inactive_session` means the event was
  received and saved for its own session but was not sent to HID.
- `sent event=...` means relay forwarded the selected session event.
- `daemon received/state=...` means daemon accepted the event and updated HID.
- Switching conversations without submitting a prompt does not provide a
  reliable active-session signal; the next `UserPromptSubmit` or explicit
  `--set-active-session` command selects the session.

## After Install

Tell the user:

- Codex should be restarted.
- Logs are at `~/.codex_screen/codex_screen.log`.
- The installer backed up `~/.codex/config.toml` before editing it.
- The daemon starts automatically on the next `SessionStart` hook and exits after idle timeout.
- Closing Codex normally sends `SessionEnd`, which closes the daemon and its app-server session; changing `auth.json` also forces the next quota refresh to reconnect with the new account.
- Runtime files are Python scripts under `~/.codex_screen`; no packaged exe is required.
