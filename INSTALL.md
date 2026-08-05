# Install Codex Monitor Hook

For Codex, use this prompt:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/q8462880/codex-monitor-hook/refs/heads/master/.codex/INSTALL.md
```

The fetched installer document tells Codex how to download this repository zip, install the
`codex-monitor-hook` skill, copy runtime files to `~/.codex_screen`, update Codex hooks, and verify
the local daemon.

The Windows installer uses prebuilt executables. Users do not need Python, `pip`, `hidapi`, Git,
or other developer tools. Current packaged install support is Windows only.

Use the default `full` hook profile unless you explicitly want fewer Windows hook launches.
