# OpenCode adapter

Read the repository root `AGENTS.md` and `plugin/skills/swarm/SKILL.md`.
If native subagents are available, use the four bounded KiCad roles. Otherwise call
`python mcp_server/run_tool.py run_electrical_pipeline` and report its bounded JSON result.
Do not claim ERC/DRC success without actual KiCad CLI output.
