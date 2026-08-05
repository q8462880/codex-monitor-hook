# Codex Monitor Hook Install

Follow these instructions to install or update `codex-monitor-hook` for the current Windows user.

This installer deploys prebuilt executables. Do not install Python, `pip`, `hidapi`, Git, or any
system startup task for the user.

## Safety Rules

- Do not delete an existing Codex config.
- Do not edit USB or HID firmware files during install.
- Do not install Python, `pip`, `hidapi`, Git, or developer tools.
- Do not install system startup tasks.
- Do not run destructive git commands.
- Use Windows PowerShell.
- Keep the default `full` hook profile unless the user explicitly asks for fewer hook events.

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
   Move-Item -LiteralPath (Join-Path $ExtractRoot "codex-monitor-hook-master") -Destination $Target -Force
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
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -HookProfile full
   ```

   The installer copies `codex_hook_relay.exe`, `codex_screen_daemon.exe`, and the hidden-window
   hook launcher to `~/.codex_screen`. It backs up `~/.codex/config.toml` before replacing this
   project hook block.

4. Verify installation.

   Windows PowerShell:

   ```powershell
   & $HOME\.codex_screen\codex_screen_daemon.exe --self-test
   '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' | & $HOME\.codex_screen\codex_hook_relay.exe
   Get-Content $HOME\.codex_screen\codex_screen.log -Tail 80
   ```

5. Restart Codex.

   Codex must reload `config.toml` and discover the installed skill.

## Performance Mode

Use full hooks by default so the screen can show `EXECUTING`, `COMPACTING`, and `SUBAGENT`.

Only when the user explicitly prefers fewer Windows hook process launches, reinstall with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -HookProfile minimal
```

`minimal` removes `PreToolUse`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, and
`SubagentStop`, so those detailed states will not be shown.

## After Install

Tell the user:

- Codex should be restarted.
- Logs are at `~/.codex_screen/codex_screen.log`.
- The installer backed up `~/.codex/config.toml` before editing it.
- The daemon starts automatically on the next `SessionStart` hook and exits after idle timeout.
