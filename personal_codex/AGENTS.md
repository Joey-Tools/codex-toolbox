# Personal Guidelines

- Prefer `rg` or `rg --files` for text and file searches when available.
- Before running a command that may produce large or unbounded output, narrow its inputs or results, or capture complete output in a task-scoped file; surface only counts, candidate filenames, decisive key lines, or a short tail. Treat display or output caps as backstops, not execution-time bounds.
- When polling with `wait_agent`, omit `timeout_ms` or set it to at least `10000` milliseconds; shorter values are invalid.
