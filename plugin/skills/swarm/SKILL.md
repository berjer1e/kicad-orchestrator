---
name: swarm
description: >-
  Host-agnostic KiCad workflow coordinator. Uses native subagents when available
  and falls back to deterministic MCP pipeline calls when they are unavailable.
---

# KiCad Swarm Coordinator

Read [`../shared/WORKFLOW.md`](../shared/WORKFLOW.md) and the repository `AGENTS.md` first.
The MCP server supplies tools; the host supplies workers. Never assume that MCP can create
subagents.

## Native mode

When the host exposes subagent/swarm APIs, delegate bounded roles:

- `searcher`: verify external module dimensions, pinout, variants, and footprint candidates.
- `generator`: create schematic, symbols, footprints, and the netlist manifest.
- `tracer`: apply the netlist, place/route PCB copper, and add explicit GND connectivity.
- `tester-converter`: run ERC, DRC with schematic parity, mechanical checks, and exports.

Pass only artifact paths and compact failure summaries between workers. The coordinator owns
the final gate and must stop on unresolved design decisions or missing pad assignments.

## Fallback mode

If workers are unavailable, quota-limited, or denied, run the deterministic tools directly:

1. `create_netlist_from_bom` and `create_schematic`
2. `run_electrical_pipeline` with `netlist_path` and optional `gnd_zone`
3. `run_validations` for ERC, DRC, and locks
4. `inspect_pcb_layout`, `find_unrouted_pads`, `analyze_pcb_traces`, and `check_mechanical_fit`

Return `PASS`, `PASS WITH WARNINGS`, or `BLOCKED` with exact artifact paths and tool results.
Never report a clean electrical design without actual ERC/DRC output.
