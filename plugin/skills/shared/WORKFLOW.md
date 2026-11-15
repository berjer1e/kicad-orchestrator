# Shared KiCad workflow contract

- Prefer the consolidated MCP tools or `mcp_server/run_tool.py`; do not invent ad-hoc scripts.
- Pass artifact paths and compact JSON manifests, not full reports or repeated BOM text.
- Check locks before edits. Mutators stage a side file when the target is locked.
- After PCB mutations, run the smallest relevant inspection and pass only failures/warnings onward.
- Use `run_electrical_pipeline` for lock check → netlist → optional GND zone → routing → inspection.
- Use `output/state_summary.json` as the handoff artifact; regenerate it after major mutations.
- Never claim validation or export success without the corresponding tool result.
