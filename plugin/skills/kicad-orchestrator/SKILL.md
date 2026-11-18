---
name: kicad-orchestrator
description: >-
  Master multi-agent coordinator for KiCad 10 hardware design workflows.
  Activate whenever the user runs "kicad orchestrator", "кікад оркестратор",
  submits a hardware BOM/specification file (.txt, .docx, .csv) or existing
  KiCad project (.kicad_sch, .kicad_pcb), or asks to build, generate, inspect,
  validate, or export a KiCad PCB/schematic end-to-end.
---

# KiCad Multi-Agent Orchestrator

Coordinate KiCad 10 hardware design from specification or existing project to verified fabrication outputs (`v0.5.0` token-optimized pipeline).

## Tooling, Disk Cache & Quota Policy

0. **Single-Board No-Swarm Rule (Strict):**
   - **NEVER launch a multi-agent Swarm** when designing, routing, or auditing a single KiCad project / single `.kicad_pcb` file — even if the prompt mentions `/teamwork` or parallel workers.
   - Schematic/netlist setup -> PCB placement/routing -> verification/export are strictly sequential stages on the same `.kicad_pcb` file; parallel workers cannot edit one PCB concurrently and waste quota waiting on locks. Always execute sequentially in a single agent context via `run_electrical_pipeline` / `run_tool.py`.

1. **22 Consolidated Tools (`kicad-orchestrator_kicad_mcp` or `run_tool.py`):**
   - `parse_bom`, `manage_design_decisions`, `list_modules`, `generate_module(module_name, output_dir, kind="both", overrides=None)`
   - `inspect_pcb_layout(pcb_path, compact=True)`, `find_unrouted_pads`, `check_mechanical_fit`, `analyze_pcb_traces`
   - `create_kicad_project`, `add_footprints_to_pcb`, `edit_pcb_layout(board_outline, footprint_placements, replace_footprints, auto_fit_outline, clean_silkscreen, zones, graphics)`, `add_nets_to_pcb`, `assign_pad_nets`, `create_netlist_from_bom`, `apply_netlist_to_pcb`
   - `run_electrical_pipeline`
   - `create_schematic(..., append=False)` (`append=True` adds components/nets to an existing `.kicad_sch`)
   - `edit_pcb_traces`, `autoroute_pcb_nets`, `refill_pcb_zones`
   - `run_validations(target_path, validation_type="drc"|"erc"|"footprint"|"symbol"|"locks"|"version", compact=True, errors_only=False, ...)`
   - `export_project(target_path, output_path, export_type="gerbers"|"drill"|"step"|"sch_pdf"|"pcb_svg"|"3d", ...)`
2. **Portable CLI Fallback (`run_tool.py`):** invoke `$env:KICAD_MCP_ROOT\run_tool.py` (located in the MCP server's `mcp_server/` directory, NOT inside the plugin folder) with compact JSON arguments (`--args` or `--args-file <path.json>` for large payloads); use `--save-state-summary` for the cached handoff artifact. Follow [`../shared/WORKFLOW.md`](../shared/WORKFLOW.md).
3. **Zero-MCP Subagents + Disk-Cached State (`state_summary.json`):**
   - Run `--save-state-summary` once in Step 1 (and after major PCB mutations) so layout, unrouted pads, trace widths, and mechanical fit live in `<project_dir>/output/state_summary.json`.
   - Pass only `<project_dir>/output/state_summary.json` paths and concise targets to specialists. **Never set `EnableMCPTools: true`** on spawned workers; workers read `state_summary.json` and call `run_tool.py` when mutations or exports are needed.
4. **API Delegation & Lightweight Model Routing (`api_delegate.py`):**
   - Prefer delegating isolated analysis, BOM normalization, spec lookup synthesis, or script drafting to the Gemini API via `api_delegate.py` instead of consuming IDE quota on UI subagents:
     ```powershell
     & $mcpPy (Join-Path $mcpRoot "api_delegate.py") --task-tier simple --prompt-file "<task.txt>" --context-file "<project_dir>\output\state_summary.json"
     ```
   - Route by task complexity (`--task-tier trivial` -> `gemini-3.5-flash-lite`, `simple` -> `gemini-3.8-flash`, `complex` -> `gemini-4-argon`, `heavy` -> `gemini-4-carbon`). Only invoke IDE subagents when interactive workspace edits or visual verification require them, using lightweight models where appropriate.

## End-to-End Pipeline

### Step 1: Parse Specification & Snapshot Existing Board
- Call `parse_bom` on the user's BOM/specification (`.txt`, `.csv`, `.md`, `.docx`-extracted text) to extract `board_properties` (layer count, copper weight `oz`, substrate, dimensions, finish), `connector_details` (orientation, THT/SMD, pitch, keyed/polarized, current rating), `components`, `custom_modules_detected`, and `external_modules_detected`.
- For existing `.kicad_pcb` projects, run `run_validations(..., validation_type="locks")` and generate `<project_dir>/output/state_summary.json` via `run_tool.py --save-state-summary`.
- Always respect `.lck` warnings: use `edit_pcb_layout` / `edit_pcb_traces` / `autoroute_pcb_nets` (S-expression mutators; **strictly forbidden** to `import pcbnew` directly) or staged outputs (`refill_pcb_zones`, `export_project`) when KiCad GUI locks a file.

### Step 1.5: Design QA Gate (`design_decisions.json`)
- Run `manage_design_decisions(project_dir, action="check", bom_or_pcb_path=...)`.
- If `unresolved_questions` is non-empty, present them cleanly to the user before committing geometry, display/mezzanine mounting, module variants (`Mini560` THT vs SMD `15x18 mm`), connector orientation/keying, copper stackup, or silkscreen conventions, then persist answers with `action="save"`.

### Step 1.75: Electrical Netlist Preparation
- Convert BOM connectivity into a durable manifest with `create_netlist_from_bom`.
- Declare all signal and power nets with `add_nets_to_pcb`, then apply every reference/pad connection with `assign_pad_nets`.
- Do not invoke routing or GND-zone creation until the assignments report no missing pads; pass `current_a` and trace-class metadata from the netlist to trace analysis and routing.
- Prefer `run_electrical_pipeline` for deterministic lock-check → netlist → GND-zone → routing → inspection → optional DRC execution; the model should provide structured paths and thresholds, not generate imperative scripts.
- Generate the electrical source of truth with `create_schematic` from the same component/pin/net manifest used by `create_netlist_from_bom`; then run ERC on the generated `.kicad_sch` before PCB parity DRC.

### Step 2: Conditional Datasheet & Footprint Research (`kicad-searcher`)
- **Skip `searcher` completely** unless `parse_bom` / `state_summary.json` reveals unknown modules or connectors absent from `list_modules()` and KiCad 10 standard libraries (your KiCad installation's standard library directory).
- When needed, have `kicad-searcher` verify exact mechanical dimensions, pinout, pad/drill sizes, 3D Z-heights (`estimated_height_mm`), hole-to-header offsets, and standoff keepouts (`M2.5 >= 5.5 mm`, `M3 >= 6.5 mm`).

### Step 3: Footprint & Symbol Generation (`kicad-generator`)
- Load `design_decisions.json` (`manage_design_decisions(project_dir, action="load")`).
- Generate custom/parametric `.kicad_mod` and `.kicad_sym` files using `generate_module(..., kind="both", overrides=...)`.
- Enforce silkscreen readability (`height >= 1.0 mm`, `thickness >= 0.15 mm`), zero pad overlap, functional designators, and connector polarity/pin labels within `8.0 mm`.

### Step 4: Layout, Power & Mezzanine Routing (`kicad-tracer`)
- Instruct `kicad-tracer` to read `<project_dir>/output/state_summary.json` first and route/fix via `edit_pcb_layout`, `edit_pcb_traces`, and `autoroute_pcb_nets` (`router="auto"` uses `freerouting.jar` when present and falls back to built-in Python A* with `net_widths`, `clearance_mm`, and `auto_neckdown`).
- **Strictly forbidden:** never write ad-hoc custom Python autorouters (`python -` or `scratch/*.py` routing scripts) — always use `autoroute_pcb_nets` or `edit_pcb_traces` with `--args-file`.
- Verify IPC-2221 trunk widths with pad-entry neck-down (`<= 3.0 mm`), `ΔU = I·R <= 0.1 V`, explicit copper `GND` tracks on all critical components (`C*`, `R*`, `Y*`, `U*` — never zone-only!), crystal length `<= 15 mm`, `100 nF` decoupling `<= 8 mm`, connector mating clearance `>= 2.0 mm`, and mezzanine Z-height + rigid header alignment.
- Refresh `<project_dir>/output/state_summary.json` after PCB edits.

### Step 5: Verification & Manufacturing Exports (`kicad-tester-converter`)
- Refill zones (`refill_pcb_zones`), run full verification:
  - `run_validations(..., validation_type="drc", schematic_parity=True)`
  - `run_validations(..., validation_type="erc")`
  - Re-check `state_summary.json` (`inspect_pcb_layout`, `find_unrouted_pads`, `analyze_pcb_traces`, `check_mechanical_fit`).
- Only after `0` blocking errors, export review previews (`export_project` with `export_type="pcb_svg"`, `"sch_pdf"`, `"3d"`) and fabrication packages (`export_type="gerbers"`, `"drill"`, `"step"`).
- Report exact paths, pass/fail status, lock warnings, and remaining non-blocking warnings honestly.
