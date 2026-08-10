# Personal Guidelines

- Prefer `rg` or `rg --files` for text and file searches when available.
- Before running a command that may produce large or unbounded output, narrow its inputs or results, or capture complete output in a task-scoped file; surface only counts, candidate filenames, decisive key lines, or a short tail. Treat display or output caps as backstops, not execution-time bounds.
- When polling with `wait_agent`, omit `timeout_ms` to use the `30000` millisecond default or keep it within the supported `10000`–`3600000` millisecond range. Prefer `10000` when a response is imminent and `30000`–`60000` for ordinary or reviewer polling; longer single waits are valid but usually weaken user-facing progress updates.
- When using `spawn_agent`, do not combine an `agent_type` override with a full-history fork (`fork_turns` omitted or `"all"`): omit `agent_type` to inherit the parent role, or set `fork_turns` to `"none"` or a positive turn count when a specialized role is required.
- After `agent thread limit reached`, inspect `list_agents` once and do not repeat the same `spawn_agent` or `followup_task` attempt while the agent tree is unchanged; reuse an existing running owner with `send_message`, continue locally, or move genuinely new parallel work to a fresh root task.
