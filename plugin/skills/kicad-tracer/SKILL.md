---
name: kicad-tracer
description: >-
  PCB layout, netlist, IPC-2221 trace-width/neck-down/voltage-drop analysis,
  explicit GND routing, silkscreen polarity traceability, and stacked mezzanine
  mechanical audit specialist for KiCad 10. Use when planning or auditing power
  rail widths, ground planes, decoupling placement, keepouts, unrouted-pad
  checks, silkscreen labels, or mechanical fit on KiCad boards.
---

# KiCad Tracer (Routing Rules, Power Integrity, Silkscreen & Mezzanine Audit)

Your job is to translate parsed electrical and mechanical requirements (BOM/spec files such as `.txt`, `.docx`, `.csv`, `<project_dir>/design_decisions.json`, or existing `.kicad_sch` / `.kicad_pcb` projects, module current ratings, and pinout maps) into concrete PCB layout, routing, silkscreen, and verification constraints.

If `<project_dir>/design_decisions.json` exists, load it first via `manage_design_decisions(project_dir, action="load")` (or inspect it directly) so confirmed copper weight (`1 oz` vs `2 oz`), `display_mounting_mode`, connector orientation/keying, module variants, and silkscreen conventions govern all checks.

**Routing Tool Mandate:** Never write ad-hoc custom Python autorouters (`python -` or `scratch/*.py` routing scripts). Always route through `autoroute_pcb_nets` (which automatically uses `tools/freerouting.jar` / `freerouting.jar` when available and falls back to built-in A* with `net_widths`, `clearance_mm`, and `auto_neckdown`) and `edit_pcb_traces` (via `--args-file` for large payloads).

## Core Design Checks

1. **IPC-2221 Trace Widths, Pad Neck-Down & Voltage Drop (`ΔU = I·R`):**
   - **IPC-2221 calculation:** Compute required external trace width for each power net's current `I`, copper thickness (`copper_oz`: `1 oz` / `2 oz` from `design_decisions.json`), and allowable temperature rise `ΔT` (`max_temp_rise_c`, default `10.0 °C`) using **`analyze_pcb_traces`** (e.g. `Mini560` up to 5A: wide trunk or copper pour; `Mini360` 1.8–3A, `MT3608` 2A, `TP4056` 1A, Li-ion 18650: `>= 1.0–1.5 mm` trunk; logic/signals: `0.25–0.35 mm`).
   - **Pad-aware neck-down:** Route power trunks at full IPC-2221 width, permitting local narrowing (**`<= 3.0 mm` segment length** via `neckdown_max_length_mm`) **ONLY when entering small or fine-pitch component pads** (`valid_neckdown` vs `undersized_segment`). Never run long narrow power tracks across the board.
   - **Resistance & voltage drop:** Verify segment resistance and path voltage drop **`ΔU = I·R <= 0.1 V`** (`max_voltage_drop_v`) on all power rails (`GND`, `+5V`, battery/boost rails).
   - **Critical net lengths & parasitics (verified via `analyze_pcb_traces`):**
     - Crystal (`16 MHz HC-49S`) + `22 pF` load capacitors (`C1`/`C2`) nets: **`<= 15 mm`** total trace length.
     - `100 nF` decoupling loop from capacitor pads to IC `VCC`/`GND` pins: **`<= 8 mm`**.
     - `I2C` (`SDA`/`SCL`) & clock lines: track total trace length (`<= 150 mm`) and estimated parasitic capacitance (`est_capacitance_pf`, ~`0.1 pF/mm`).

2. **Explicit `GND` Tracks + Copper Zone ("Доріжки + Зона"):**
   - **Mandatory rule:** EVERY `GND` pad—especially decoupling capacitors (e.g. `C4`, `C5`), crystal load capacitors (`C1`/`C2`), sense/pull-down resistors, and IC `GND` pins—**MUST have an explicit routed copper track (`segment`)** connecting it to the ground bus or a dedicated `GND` via.
   - **Never rely solely on a `B.Cu`/`F.Cu` copper zone pour (`filled_polygon`)** for pad connectivity, so no `GND` pad appears unrouted or isolated when zone fills are hidden or thermal reliefs are obstructed.
   - **Required verification:** Always run `find_unrouted_pads(pcb_path, net_filter="GND")` (plus full `find_unrouted_pads(pcb_path)`) and confirm `zone_only_pads` and `completely_unrouted_pads` contain no unrouted `GND` pads.

3. **Mandatory Placement & Decoupling Rules:**
   - Crystal (`16 MHz HC-49S`) and `22 pF` load capacitors as close as possible to `XTAL1`/`XTAL2`, surrounded by solid GND return with explicit ground tracks (`<= 15 mm` total crystal net length).
   - `100 nF` decoupling capacitors immediately adjacent to MCU/IC `VCC`/`GND` pins (`<= 8 mm` loop).
   - Separate noisy switching loops (`MT3608`, `Mini560`, `Mini360`) from analog/I2C/sensor lines.

4. **Silkscreen & Connector Polarity Traceability:**
   - **Connector pin & polarity labels:** Every external and internal connector (`J*`) must have visible silkscreen pin/polarity labels (e.g., `+`, `-`, `GND`, `VCC`, `5V`, `BATT`, `SDA`, `SCL`, `TX`, `RX`, `LCD`, `KEY`, `1`) within **`8.0 mm`** of the connector footprint.
   - **Functional designators:** Use clear functional designator naming (e.g. `U_CHG1`, `U_BOOST1`, `J_LCD1`, `J_BATT1`) matching `design_decisions.json`.
   - **Legibility & fabrication rules:** All silkscreen text on `F.SilkS` / `B.SilkS` must maintain font height **`>= 1.0 mm`** and thickness **`>= 0.15 mm`**.
   - **Pad overlap prohibition:** Zero visible silkscreen text or graphic lines overlapping exposed copper pads or vias (enforced by `inspect_pcb_layout` -> `silkscreen_audit`).

5. **External, Modular & Stacked Mezzanine Mechanical Audit & Keepouts:**
   - Respect edge overhangs (e.g. `TP4056_HW373` Type-C connector `1.5 mm` overhang past board edge).
   - Keep mounting holes and user-accessible switches/connectors clear of high-voltage or fragile components.
   - **Displays (`LCD1602`, `LCD2004`), buttons, and connectors (`JST-XH`):**
     - Explicitly check whether PCB mounting holes are required on the main board OR explicitly document enclosure/panel mounting via cable (e.g. `J_LCD*` header/harness) per `design_decisions.json`.
     - Verify 3D component height (`estimated_height_mm` from `inspect_pcb_layout`) and enforce **`>= 2.0 mm` plug insertion/extraction clearance** around `JST-XH` connectors so adjacent resistors, capacitors, or board edges do not obstruct plug mating or latch release.
   - **3 Mandatory Stacked/Mezzanine Checks (via `check_mechanical_fit` with `standoff_keepout_dia_mm`, `standoff_height_mm`, `mezzanine_modules`):**
     1. **Mounting hole + standoff/screw-head keepout:** Enforce **`M2.5 >= 5.5 mm`** or **`M3 >= 6.5 mm`** (`standoff_keepout_dia_mm`) diameter keepout around every mounting hole (`H*`), kept completely clear of components and non-`GND` copper tracks.
     2. **Top-floor 2D projection + Z-clearance audit:** Verify no tall bottom-board parts—such as vertical `HC-49S` (`11.0 mm`), DIP sockets (`8.5 mm`), `MT3608` inductor/potentiometer (`7.5 mm`), electrolytic capacitors, or tactile switches (`SW*`)—collide inside or exceed `standoff_height_mm - 1.5 mm` under a mezzanine display/module.
     3. **Board-to-board rigid header alignment:** For rigid `PinHeader` / `FemaleHeader` / `Socket_Strip` mezzanine interconnects, lock connector `(x, y)` coordinates strictly to upper-module mounting-hole `(dx, dy)` offsets and confirm paired mezzanine standoff holes exist.

6. **MCP Inspection, Layout Editing & Routing Tools (No Ad-Hoc `pcbnew` or Regex Scripts):**
   - Always use **`edit_pcb_layout`**, **`edit_pcb_traces`**, and **`autoroute_pcb_nets`** from `kicad-orchestrator_kicad_mcp` (or `run_tool.py`) to place/move footprints, set `Edge.Cuts` outlines, create copper zones, add/clear copper `(segment ...)` and `(via ...)` items with validated integer net IDs, or run obstacle-aware 2-layer A* autorouting.
   - **STRICT PROHIBITION (`import pcbnew` & raw S-expr scripts):** **NEVER** write custom Python scripts that `import pcbnew` (KiCad 10 SWIG API changes such as `PCB_VIA::GetWidth(layer)` trigger headless C++ `wxWidgets` assert dialogs that hang the process indefinitely) or concatenate raw `(segment ...)` strings with string net names (which causes `kicad-cli` to loop infinitely). Always call `edit_pcb_layout`, `edit_pcb_traces`, and `autoroute_pcb_nets`.
   - Always use **`analyze_pcb_traces`**, **`inspect_pcb_layout`**, **`find_unrouted_pads`**, **`check_mechanical_fit`**, and **`manage_design_decisions`** to analyze `.kicad_pcb` geometry, IPC-2221 widths/drops, track/via/zone counts, unrouted pads, silkscreen audit, collisions, edge overhangs, connector clearances, and mezzanine/standoff fit.
   - Check `<project_dir>/output/state_summary.json` first when present on disk instead of repeating inspection calls, and inspect `lock_warning` (or call `run_validations(target_path, validation_type="locks")`) before performing on-disk PCB edits while KiCad GUI holds `~*.lck` (`edit_pcb_*` and `refill_pcb_zones` automatically stage to a side file when locked).

7. **Routing Handoff:**
   - Document net classes (`Power_5A`, `Power_1A`, `Signal`, `I2C`), IPC-2221 trunk/neck-down limits, `ΔU = I·R` margins, explicit `GND` track status, silkscreen conventions, clearance rules, and external/mezzanine mechanical audit notes so `tester-converter` can verify them with `run_validations(..., validation_type="drc", schematic_parity=True)`, `analyze_pcb_traces`, `find_unrouted_pads`, and `check_mechanical_fit`.

8. **Hybrid Routing Strategy (Token-Saving: Critical Nets + Freerouting CLI):**
   - To minimize LLM token usage and avoid slow/error-prone manual routing of large low-speed signal matrices (e.g. keypads, diodes, shift registers, `I2C` lines), use a **2-stage hybrid workflow** whenever `freerouting` + `java` are available on the machine:
     1. **Stage 1 (`tracer` routes & locks critical nets):** Route ONLY high-current power rails (`+5V`, `GND`, `/VBUS_IN`, `/VBAT*`, `/BATT_NEG`), explicit `GND` pad segments, decoupling loops (`<= 8 mm`), and sensitive crystal/clock nets (`16 MHz HC-49S` + `C1`/`C2`, `<= 15 mm`), then mark those segments/vias as **`locked`** in `.kicad_pcb` so the autorouter cannot alter them.
     2. **Stage 2 (Local `Freerouting` CLI for non-critical signals):** Export Specctra `.dsn` (`File -> Export -> Specctra DSN` or KiCad CLI/Python), run `java -jar freerouting.jar -de board.dsn -do board.ses` locally with zero LLM tokens to route remaining non-critical signals with `45°` miters, import `.ses` back into `.kicad_pcb`, and verify with `run_validations(..., validation_type="drc", schematic_parity=True)`.
