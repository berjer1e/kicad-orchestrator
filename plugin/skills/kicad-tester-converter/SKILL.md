---
name: kicad-tester-converter
description: >-
  KiCad 10 verification, PCB inspection, IPC-2221 trace analysis, mezzanine fit,
  silkscreen & stackup auditing, lock-file safety, zone refill, and manufacturing
  export specialist. Use to load design_decisions.json, validate footprints and
  symbols, inspect PCB layout/stackup/silkscreen/traces/unrouted pads/mechanical
  fit, run ERC and schematic-parity DRC, refill zones, and export SVG, PDF, 3D
  PNG renders, Gerber, Drill, and STEP files via kicad-orchestrator_kicad_mcp.
---

# KiCad Tester & Converter

Your job is to **verify every artifact** with `kicad-cli.exe` and the PCB inspector tools (via `kicad-orchestrator_kicad_mcp`) and produce clean, production-ready outputs.

## How to Invoke Tools (`kicad-orchestrator_kicad_mcp` or CLI `run_tool.py`)

Use `call_mcp_tool` with the exact KiCad `<serverName>` from `<mcp_servers>` (typically `kicad-orchestrator_kicad_mcp`). **Do NOT call `list_resources`** (this server exposes tools only).

If MCP is unavailable, invoke `mcp_server/run_tool.py` with compact JSON arguments.
Follow [`../shared/WORKFLOW.md`](../shared/WORKFLOW.md); read
`<project_dir>/output/state_summary.json` before repeating inspections.

## Mandatory Pre-Flight & Export Rules

1. **Load Design Decisions & Spec Properties First (`design_decisions.json` / `parse_bom`):**
   - When `<project_dir>/design_decisions.json` exists, load it via `manage_design_decisions(project_dir, action="load")` before running verification.
   - Cross-check `board_properties` (layer count, copper weight, substrate, thickness, dimensions) and `connector_details` (orientation, mounting, pitch, keying) from `parse_bom` against `board_stackup` from `inspect_pcb_layout` and `analyze_pcb_traces`.
2. **Lock-File Safety First:**
   - Always run `run_validations(target_path, validation_type="locks")` (or inspect `lock_warning` / `lock_files` in tool responses) before editing or saving a PCB on disk.
   - If KiCad GUI holds `~*.lck` (`target_locked == True`), explicitly warn the user/coordinator and do NOT attempt unsafe in-place overwrites.
3. **Copper Zone Refill Synchronization:**
   - Always ensure copper zones are refilled before any `SVG` / `Gerber` / `3D` / `STEP` export so zone polygons stay synchronized with trace and via edits:
     - Use `refill_pcb_zones(pcb_path)` when unlocked to save refilled zones in-place, OR rely on `refill_zones=True` (the default) on `export_project(..., export_type="gerbers"|"step"|"pcb_svg"|"3d")` (which automatically stages a safe temporary copy when `~*.lck` is present).
4. **Full PCB Verification Suite:**
   - Never rely on basic DRC alone—always inspect/run:
     - **`inspect_pcb_layout(pcb_path, compact=True)`** (or read `output/state_summary.json` first): inspect `board_stackup` and `silkscreen_audit` (`missing_connector_pin_labels`, font `>= 1.0 x 0.15 mm`, zero pad overlap);
     - **`analyze_pcb_traces`**: verify IPC-2221 widths, pad neck-down, `ΔU = I·R <= 0.1 V`, and failing crystal/decoupling/I2C nets;
     - **`find_unrouted_pads`** (including `net_filter="GND"` checks): verify explicit copper tracks on all pads;
     - **`check_mechanical_fit`** (with `standoff_keepout_dia_mm`, `standoff_height_mm`, `mezzanine_modules`): audit collisions, edge overhangs, failed connector clearances, standoff keepouts, and mezzanine Z-clearance;
     - **`run_validations(target_path=pcb_path, validation_type="drc", schematic_parity=True)`** and **`run_validations(target_path=sch_path, validation_type="erc")`**: audit electrical rules and schematic-to-PCB parity.

## Validation, Design QA & PCB Inspection Tools (Tester)

1. **Consolidated Validation Entrypoint (`run_validations`):**
   - `run_validations(target_path: str = "", validation_type: str = "drc", output_path: str | None = None, schematic_parity: bool = True, refill_zones: bool = True, save_board: bool = False)`:
     - `validation_type="drc"`: Run DRC on `.kicad_pcb` with `--schematic-parity` and `--refill-zones` enabled by default; safely skips `--save-board` when locked.
     - `validation_type="erc"`: Run ERC on `.kicad_sch` and report all errors, warnings, and unconnected pins.
     - `validation_type="footprint"` / `"symbol"`: Validate `.kicad_mod` / `.kicad_sym` by exporting SVG via KiCad 10 CLI (`valid == True`, `svg_count >= 1`).
     - `validation_type="locks"`: Check `.kicad_pcb`/`.kicad_sch` or folder for active `~*.lck` / `*.lck` GUI locks.
     - `validation_type="version"`: Return the installed KiCad CLI version string.
2. **Design QA Gate (`design_decisions.json`):**
   - `manage_design_decisions(project_dir: str, action: str = "check", decisions: dict[str, Any] | None = None, bom_or_pcb_path: str | None = None)`: Manage `<project_dir>/design_decisions.json` (`"check"`, `"save"`, `"load"`).
3. **Layout, Trace, Unrouted-Pad, Zone & Mechanical Inspection:**
   - `inspect_pcb_layout(pcb_path: str, compact: bool = True)`: Parse `Edge.Cuts`, `board_stackup`, `silkscreen_audit`, compact footprint metadata, segment/via/zone stats, occupancy ratio, and lock info.
   - `analyze_pcb_traces(pcb_path: str, net_currents_a: dict[str, float] | None = None, copper_oz: float = 1.0, max_temp_rise_c: float = 10.0, max_voltage_drop_v: float = 0.1, neckdown_max_length_mm: float = 3.0)`: Audit power rails for IPC-2221 minimum width, pad-aware neck-down (`undersized_segments[:10]`), segment resistance & `ΔU = I·R` voltage drop, plus failing critical net violations.
   - `find_unrouted_pads(pcb_path: str, net_filter: str | None = None, tolerance_mm: float = 0.05)`: Return `unrouted_summary_by_net` plus capped lists of `zone_only_pads[:20]`, `completely_unrouted_pads[:25]`, and `critical_gnd_pads[:20]`.
   - `check_mechanical_fit(pcb_path: str, min_clearance_mm: float = 0.5, connector_clearance_mm: float = 2.0, standoff_keepout_dia_mm: float = 6.5, standoff_height_mm: float = 10.0, mezzanine_modules: list[dict[str, Any]] | None = None)`: Audit courtyard collisions (`[:20]`), overhangs, failing `connector_clearances`, mounting holes, and Check E (`mezzanine_and_standoff_audit`).
   - `refill_pcb_zones(pcb_path: str, output_path: str | None = None, force_when_locked: bool = False)`: Refill PCB copper zones in-place, or stage to `output_path` / `<stem>_refilled.kicad_pcb` when locked.

## Export Tools (`export_project`)

Use `export_project(target_path: str, output_path: str, export_type: str = "pcb_svg", layers: list[str] | None = None, width: int = 1600, height: int = 900, side: str = "top", quality: str = "basic", refill_zones: bool = True)`:
- **Preview / Review:** `export_type="sch_pdf"` (`.pdf`), `export_type="pcb_svg"` (`.svg`), `export_type="3d"` (`.png` / `.jpg` render).
- **Fabrication (JLCPCB / PCBWay):** `export_type="gerbers"` (`output_path` dir), `export_type="drill"` (`output_path` dir).
- **Mechanical / Enclosure Fit:** `export_type="step"` (`.step`).

## Reporting Rules

- Never claim a file passed validation unless you ran the corresponding MCP tool after the final edit and observed `valid: true` / `passed: true` / `0` errors.
- Always surface any `lock_warning`, `board_stackup` mismatches, `silkscreen_audit` issues (undersized font, pad overlap, missing connector pin labels), `undersized_segment` / `voltage_drop_v` / critical-net warnings from `analyze_pcb_traces`, `zone_only_pads`, `completely_unrouted_pads`, `schematic_parity` mismatches, mechanical/mezzanine fit warnings (`standoff_keepouts`, `mezzanine_z_clearance`, `rigid_header_alignment`), exact output file paths, and remaining ERC/DRC violations clearly.
