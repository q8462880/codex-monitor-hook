# Codex Monitor Hook Install

Follow these instructions to install or update `codex-monitor-hook` for the current Windows or macOS user.

This installer uses the user's existing standard Python installation. It does not install a
separate Python runtime or use PowerShell to execute Hooks. It automatically installs the
single `hidapi` package if the selected Python does not have it.

## Safety Rules

- Do not delete an existing Codex config.
- Do not edit USB or HID firmware files during install.
- Do not install a separate Python runtime, Git, or developer tools.
- Do not install system startup tasks.
- Do not run destructive git commands.
- Use Windows PowerShell on Windows, or a POSIX shell on macOS.
- Keep the default `quota` hook profile; use `full` only when restoring legacy HID status signals.

## Install From GitHub

1. Download and unpack the repository zip.

   Windows PowerShell:

   ```powershell
   $Url = "https://github.com/q8462880/codex-monitor-hook/archive/refs/heads/master.zip"
   $Target = Join-Path $HOME ".codex\codex-monitor-hook"
   $Parent = Split-Path -Parent $Target
   $ZipPath = Join-Path $env:TEMP "codex-monitor-hook-master.zip"
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

   # 兼容 GitHub zip 的外层目录和 OSS 发布包的根目录两种结构。
   $PackageRoot = Join-Path $ExtractRoot "codex-monitor-hook-master"
   if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot "scripts\install.ps1"))) {
       $PackageRoot = Join-Path $ExtractRoot "codex-monitor-hook"
   }
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

   This step lets future Codex sessions discover the `codex-monitor-hook` skill.

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
   It checks the existing Python installation for both `python.exe` and its matching
   `pythonw.exe`. Missing `hidapi` is installed automatically with that same Python.
   Hook configuration calls `pythonw.exe` and the relay Python script directly; it does not
   use a cmd, `.ps1`, or packaged-executable launcher. Codex Desktop currently evaluates
   `commandWindows` with PowerShell, so that field uses PowerShell's built-in `&` call
   operator to invoke the same Python executable. The installer adds a project-specific,
   conditional `.pth` bootstrap to the selected Python user's `site-packages`. It only
   handles the Windows Codex runner case where Hook JSON appears before the relay script;
   ordinary Python commands are unaffected. The installer validates that bootstrap using
   the same JSON-before-script argument order before it changes the Hook configuration. It backs up `~/.codex/config.toml` before
   replacing this project hook block. Reinstall stops only Python processes whose command line points to this project's
   relay or daemon script. It also stops and removes legacy `codex_hook_relay.exe`,
   `codex_screen_daemon.exe`, and the old PowerShell launcher from `~/.codex_screen`.
   `12688` is only the preferred local port. If Windows has reserved it, the daemon
   automatically selects another port and records it in `~/.codex_screen/runtime.json`.

4. Verify installation.

   Windows PowerShell:

   ```powershell
   & python $HOME\.codex_screen\codex_screen_daemon.py --self-test
   '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' | & python $HOME\.codex_screen\codex_hook_relay.py
   Get-Content $HOME\.codex_screen\codex_screen.log -Tail 80
   ```

5. Restart Codex.

   Codex must reload `config.toml` and discover the installed skill.

The standard Windows Python installation includes `pythonw.exe`. The installer refuses to
fall back to `python.exe`, because a console Python process can create a visible terminal
window.

## macOS Install

On macOS, use the existing `python3`; `pythonw.exe`, PowerShell, and the Windows bootstrap are
not required. From the unpacked package directory, run:

```sh
chmod +x ./scripts/install.sh
./scripts/install.sh --hook-profile quota
```

This copies runtime files to `~/.codex_screen`, installs the skill, backs up
`~/.codex/config.toml`, and writes a POSIX `command` Hook that invokes `python3` directly.
It does not create a LaunchAgent or other system startup task. Set
`CODEX_SCREEN_PYTHON=/path/to/python3` when Python is outside `PATH`.

For a custom Codex home, use the same directory that Codex will read after restart:

```sh
./scripts/install.sh --codex-home "$CODEX_HOME" --hook-profile quota
```

The installer prints elapsed time for each stage. Add `--skip-hook-diagnostic` only when
the app-server check is slow or unavailable; it still validates the written hook block.

Verify with:

```sh
python3 "$HOME/.codex_screen/codex_screen_daemon.py" --self-test
printf '%s\n' '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' |
  python3 "$HOME/.codex_screen/codex_hook_relay.py"
tail -n 80 "$HOME/.codex_screen/codex_screen.log"
```

## Quota Login Requirements

- API key login (`auth_mode = "apikey"`) can run Codex，但不一定具备 ChatGPT 账户额度权限。daemon 会实际请求一次额度接口；服务端拒绝时设备会隐藏额度区域并保持在线，具体原因记录在日志中。
- Official ChatGPT account login must be completed with Codex login. Verify it with:

  ```powershell
  & $HOME\.codex\.sandbox-bin\codex.exe login status
  ```

  The daemon inherits `CODEX_HOME`, so a custom Codex home uses the same official account credentials. After login, restart Codex so the next daemon instance refreshes the quota.
- Quota diagnostics are in `~/.codex_screen/codex_screen.log`, using `[quota]` lines. The expected successful line contains `quota available via`; authentication failures include the app-server error returned by `account/rateLimits/read`.

The daemon returns any displayed state to `READY` after 2 minutes without a new hook event. Set `CODEX_SCREEN_HOOK_STALE_TIMEOUT_SEC=0` to disable this fallback.

After installation, restart Codex and review/trust the hooks in Settings > Hooks. A blue enabled toggle alone does not mean trusted. The default quota profile expects `loaded=3 executable=3`.

## Performance Mode

The default quota profile installs `SessionStart`, `UserPromptSubmit`, and `SessionEnd`. The first
two start the daemon and request a throttled quota refresh; `SessionEnd` closes the daemon cleanly.
No status signal is sent to HID. To restore the
legacy status display hooks, reinstall explicitly with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -HookProfile full
```

The legacy relay branch is disabled by default. Set `CODEX_SCREEN_ENABLE_STATUS_HOOKS=1`
before starting Codex when you also need those status signals.

`minimal` remains as the previous reduced status profile for compatibility.

## After Install

Tell the user:

- Codex should be restarted.
- Logs are at `~/.codex_screen/codex_screen.log`.
- The installer backed up `~/.codex/config.toml` before editing it.
- The daemon starts automatically on the next `SessionStart` hook and exits after idle timeout.
