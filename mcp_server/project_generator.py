"""Small, deterministic KiCad project bootstrapper.

This intentionally creates only a valid empty project and board outline.  All
component placement and routing remains the responsibility of the existing
PCB editor tools, which makes creation safe and idempotent.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any


def _uid() -> str:
    return str(uuid.uuid4())


def create_kicad_project(
    project_dir: str,
    project_name: str = "LED_Controller",
    width_mm: float = 100.0,
    height_mm: float = 80.0,
    board_thickness_mm: float = 1.6,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a minimal KiCad project that later MCP tools can edit."""
    if width_mm <= 0 or height_mm <= 0:
        return {"status": "error", "error": "Board dimensions must be positive."}
    root = Path(project_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    pcb = root / f"{project_name}.kicad_pcb"
    sch = root / f"{project_name}.kicad_sch"
    pro = root / f"{project_name}.kicad_pro"
    existing = [p for p in (pcb, sch, pro) if p.exists()]
    if existing and not overwrite:
        return {"status": "error", "error": "Project files already exist; set overwrite=true to replace.", "existing_files": [str(p) for p in existing]}

    pcb_text = f'''(kicad_pcb (version 20260206) (generator pcbnew)
  (general (thickness {board_thickness_mm:g}))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "b.silkscreen")
    (37 "F.SilkS" user "f.silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (gr_rect (start 0 0) (end {width_mm:g} {height_mm:g})
    (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts")
    (uuid "{_uid()}"))
)\n'''
    sch_text = f'''(kicad_sch (version 20231120) (generator eeschema)
  (uuid "{_uid()}")
  (paper "A4")
  (lib_symbols)
  (sheet_instances (path "/" (page "1")))
)\n'''
    pro_data = {
        "board": {"design_settings": {"board_stackup": {"layer_presets": [], "layers": {}}}},
        "meta": {"filename": f"{project_name}.kicad_pro", "version": 1},
        "net_settings": {},
        "text_variables": {},
    }
    pcb.write_text(pcb_text, encoding="utf-8")
    sch.write_text(sch_text, encoding="utf-8")
    pro.write_text(json.dumps(pro_data, indent=2), encoding="utf-8")
    (root / "sym-lib-table").write_text("(sym_lib_table)\n", encoding="utf-8")
    (root / "fp-lib-table").write_text("(fp_lib_table)\n", encoding="utf-8")
    return {"status": "ok", "project_dir": str(root), "files": [str(pcb), str(sch), str(pro)], "board": {"width_mm": width_mm, "height_mm": height_mm, "thickness_mm": board_thickness_mm}}
