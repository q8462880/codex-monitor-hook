# Codex Monitor Hook Install

Follow these instructions to install or update `codex-monitor-hook` for the current user.

This installer configures a local Codex hook relay and a Python HID daemon. The hook writes to
Codex `config.toml`, so always preserve the automatic backup created by `scripts/install.ps1`.

## Safety Rules

- Do not delete an existing Codex config.
- Do not edit USB or HID firmware files during install.
- Do not install system startup tasks.
- Do not run destructive git commands.
- On Windows, use PowerShell.
- Keep the default `full` hook profile unless the user explicitly asks for fewer hook events.

## Install From GitHub

1. Choose a local checkout directory.

   Recommended paths:

   - Windows: `%USERPROFILE%\.codex\codex-monitor-hook`
   - macOS/Linux: `$HOME/.codex/codex-monitor-hook`

2. Clone or update the repository.

   Windows PowerShell:

   ```powershell
   $Repo = "https://github.com/<OWNER>/codex-monitor-hook.git"
   $Target = Join-Path $HOME ".codex\codex-monitor-hook"
   if (Test-Path -LiteralPath $Target) {
       git -C $Target pull --ff-only
   } else {
       git clone $Repo $Target
   }
   ```

   macOS/Linux shell:

   ```bash
   REPO="https://github.com/<OWNER>/codex-monitor-hook.git"
   TARGET="$HOME/.codex/codex-monitor-hook"
   if [ -d "$TARGET/.git" ]; then
     git -C "$TARGET" pull --ff-only
   else
     git clone "$REPO" "$TARGET"
   fi
   ```

3. Install the Codex skill copy.

   This step lets future Codex sessions discover the `codex-monitor-hook` skill.

   Windows PowerShell:

   ```powershell
   $SkillDir = Join-Path $HOME ".codex\skills\codex-monitor-hook"
   New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
   Get-ChildItem -LiteralPath $Target -Force | Copy-Item -Destination $SkillDir -Recurse -Force
   ```

   macOS/Linux shell:

   ```bash
   SKILL_DIR="$HOME/.codex/skills/codex-monitor-hook"
   mkdir -p "$SKILL_DIR"
   cp -R "$TARGET"/. "$SKILL_DIR"/
   ```

4. Install runtime files and hooks.

   Windows PowerShell:

   ```powershell
   Set-Location $Target
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -HookProfile full
   ```

   macOS/Linux shell:

   ```bash
   cd "$TARGET"
   python3 -m pip install hidapi
   mkdir -p "$HOME/.codex_screen"
   cp scripts/codex_hook_relay.py \
      scripts/codex_screen_daemon.py \
      scripts/codex_state_manager.py \
      scripts/codex_quota_client.py \
      scripts/codex_screen_log.py \
      "$HOME/.codex_screen/"
   python3 -m py_compile "$HOME/.codex_screen/codex_hook_relay.py" \
      "$HOME/.codex_screen/codex_screen_daemon.py" \
      "$HOME/.codex_screen/codex_quota_client.py"
   ```

   On macOS/Linux, append hook config from `references/codex_config_hooks.toml` manually after replacing paths.
   The Windows installer updates Codex `config.toml` automatically and creates a timestamped backup.

5. Verify installation.

   Windows PowerShell:

   ```powershell
   python $HOME\.codex_screen\codex_screen_daemon.py --self-test
   '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' | python $HOME\.codex_screen\codex_hook_relay.py
   Get-Content $HOME\.codex_screen\codex_screen.log -Tail 80
   ```

   macOS/Linux shell:

   ```bash
   python3 "$HOME/.codex_screen/codex_screen_daemon.py" --self-test
   printf '%s\n' '{"hook_event_name":"SessionStart","session_id":"install-smoke"}' | \
     python3 "$HOME/.codex_screen/codex_hook_relay.py"
   tail -80 "$HOME/.codex_screen/codex_screen.log"
   ```

6. Restart Codex.

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
