# Install Codex Monitor Hook

For Codex, use this prompt:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/q8462880/codex-monitor-hook/refs/heads/master/.codex/INSTALL.md
```

The fetched installer document tells Codex how to download this repository zip, install the
`codex-monitor-hook` skill, copy Python runtime files to `~/.codex_screen`, update Codex hooks,
and verify the local daemon.

The Windows installer uses the existing standard Python installation and `pythonw.exe`. The
macOS installer uses the existing `python3`. Both install `hidapi` if needed; on Python 3.9/3.10,
the macOS installer also installs `tomli` only for `config.toml` validation.
and run the Hook relay and daemon as Python scripts. Git and other developer tools are not required.

On Windows run `scripts/install.ps1 -HookProfile quota`. On macOS run
`chmod +x scripts/install.sh && scripts/install.sh --hook-profile quota`. For a custom
Codex home on macOS, pass `--codex-home "$CODEX_HOME"` so installation and Codex use the
same `config.toml`. Add `--skip-hook-diagnostic` only to bypass the optional app-server
diagnostic; the installer still verifies the hook block it writes.

The default installer uses the `quota` hook profile, registering `SessionStart`,
`UserPromptSubmit`, and `SessionEnd`. On macOS, use `--hook-profile full` only to restore the legacy
status signal path.

## ChatGPT Quota Auth on macOS

When the main Codex desktop client uses an API key or a third-party `openai_base_url`, it can
rewrite `~/.codex/auth.json` back to API-key mode. The monitor therefore checks the isolated
quota home first:

```text
~/.codex_screen/quota-codex-home/auth.json
```

Place a valid ChatGPT-format `auth.json` there with owner-only permissions. The monitor passes
that directory as `CODEX_HOME` only to its private app-server process; it does not change the
desktop client's normal login or copy credentials during installation. When an account-switching
tool replaces the main `~/.codex/auth.json` with a valid ChatGPT login, the daemon securely
syncs it to this private directory on the next quota refresh. API-key mode never overwrites the
last valid private ChatGPT credential.
