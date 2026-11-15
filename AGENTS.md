# KiCad Orchestrator Agent Contract

This repository has two layers:

- `mcp_server/` is the client-independent KiCad runtime.
- `plugin/skills/` is the prompt/orchestration layer for hosts that support Skills or Swarm.

Use the MCP tools or `mcp_server/run_tool.py`; do not generate ad-hoc KiCad S-expressions in chat.
Prefer structured manifests and artifact paths over prose, full reports, or repeated BOM data.
Check locks before edits and pass `output/state_summary.json` between stages.

## Host modes

1. **Native swarm:** load `plugin/skills/swarm/SKILL.md`, then delegate search, generation,
   routing, and verification roles when the host exposes subagents.
2. **Single-agent fallback:** call `run_electrical_pipeline` and the other deterministic tools
   directly. Do not emulate workers in prose.
3. **Validation:** run `python mcp_server/doctor.py --json` before diagnosing tool failures.

Subagents are host capabilities, not MCP capabilities. A host may refuse them because of
quota, permissions, or policy; the deterministic fallback must still work.
