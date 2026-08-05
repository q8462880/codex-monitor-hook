# Install Codex Monitor Hook

For Codex, use this prompt after replacing `<OWNER>` with the GitHub owner:

```text
Fetch and follow instructions from https://raw.githubusercontent.com/<OWNER>/codex-monitor-hook/refs/heads/main/.codex/INSTALL.md
```

The fetched installer document tells Codex how to clone or update this repository, install the
`codex-monitor-hook` skill, copy runtime files to `~/.codex_screen`, update Codex hooks, and verify
the local daemon.

Use the default `full` hook profile unless you explicitly want fewer Windows hook launches.
