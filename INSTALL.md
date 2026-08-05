# Install Codex Monitor Hook

For Codex, use this prompt:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/q8462880/codex-monitor-hook/refs/heads/master/.codex/INSTALL.md
```

The fetched installer document tells Codex how to clone or update this repository, install the
`codex-monitor-hook` skill, copy runtime files to `~/.codex_screen`, update Codex hooks, and verify
the local daemon.

On Windows, the installer checks Python automatically. If `pip` is missing, it bootstraps `pip`
with `ensurepip`; if `hidapi` is missing, it installs `hidapi`. If Python itself is missing, the
one-prompt install uses `winget` to try a current-user Python install.

Use the default `full` hook profile unless you explicitly want fewer Windows hook launches.
