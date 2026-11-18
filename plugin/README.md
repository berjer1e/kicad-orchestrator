# KiCad Orchestrator Skills & Plugin (`plugin/`)

Prompt orchestration layer (`SKILL.md` definitions and plugin manifest) for multi-agent and single-agent KiCad 10 hardware workflows.

## Included Skills

- **`kicad-orchestrator`** — Master coordinator, Design QA Gate (`manage_design_decisions`), and stage handoffs.
- **`kicad-searcher`** — Component datasheet, footprint geometry, and 3D clearance research.
- **`kicad-generator`** — BOM parsing, symbol/footprint generation, deterministic schematic and netlist creation.
- **`kicad-tracer`** — PCB placement, netlist assignment, IPC-2221 trace sizing, A* routing, and zone pours.
- **`kicad-tester-converter`** — ERC, schematic-parity DRC, mechanical fit, unrouted pad checks, and Gerber/Drill/STEP/SVG/PDF/3D exports.
- **`swarm`** — Host-agnostic coordinator with deterministic single-agent fallback.

## Requirements

These skills invoke the tools provided by [`../mcp_server/`](../mcp_server/). Install and configure `mcp_server` first, or use the host adapters in [`../adapters/`](../adapters/).
