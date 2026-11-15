# Client adapters

The KiCad MCP runtime is shared. These small files teach different hosts how to load the
same repository contract without duplicating tool logic:

- `adapters/antigravity/` — install or reference the plugin Skills, including `swarm`.
- `adapters/opencode/AGENTS.md` — copy or symlink to the workspace root.
- `adapters/claude-code/CLAUDE.md` — include from the project instructions.
- `adapters/codex/AGENTS.md` — copy or symlink to the workspace root.

If a host does not support native workers, use the fallback described in `AGENTS.md`.
