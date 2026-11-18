"""FastMCP server exposing KiCad 10.0.8 CLI exports, zone refilling, PCB inspection, DRC/ERC, and generators."""

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

_PACKAGE_DIR = Path(__file__).resolve().parent
if str(_PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_DIR))

from cli_wrapper import KiCadCLI
import pcb_editor
import pcb_inspector
import electrical_editor
import pipeline
import project_generator
import schematic_generator

DEFAULT_SCHEMA_DIR: Path = (
    Path.home() / ".gemini" / "antigravity" / "mcp" / "kicad-orchestrator_kicad_mcp"
)

mcp = FastMCP("kicad-mcp")
_cli: KiCadCLI | None = None


def _get_cli() -> KiCadCLI:
    """Lazily instantiate and return the cached `KiCadCLI` wrapper.

    Returns:
        Cached `KiCadCLI` instance.
    """
    global _cli
    if _cli is None:
        _cli = KiCadCLI()
    return _cli


def _call_generator(func_name: str, *args: Any, **kwargs: Any) -> Any:
    """Import `generators.<func_name>` on demand and invoke it with clear error reporting.

    Args:
        func_name: Name of the callable exposed by the `generators` package.
        *args: Positional arguments forwarded to the generator function.
        **kwargs: Keyword arguments forwarded to the generator function.

    Returns:
        Result returned by `generators.<func_name>`, or an error dictionary if unavailable.
    """
    try:
        generators_mod = importlib.import_module("generators")
    except ModuleNotFoundError as exc:
        return {
            "status": "error",
            "error": f"The 'generators' package is not available yet: {exc}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to import 'generators': {exc}",
        }

    func: Callable[..., Any] | None = getattr(generators_mod, func_name, None)
    if func is None or not callable(func):
        return {
            "status": "error",
            "error": f"Generator function 'generators.{func_name}' is not implemented yet.",
        }

    return func(*args, **kwargs)


def sync_mcp_schemas(schema_dir: str | Path | None = None) -> dict[str, Any]:
    """Write `<tool.name>.json` schema files for active FastMCP tools and remove stale JSON schemas."""
    target_dir = (
        Path(schema_dir).expanduser().resolve()
        if schema_dir is not None
        else DEFAULT_SCHEMA_DIR
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    tools = mcp._tool_manager.list_tools()
    active_names = {tool.name for tool in tools}
    removed_tools: list[str] = []
    for existing_file in target_dir.glob("*.json"):
        if existing_file.stem not in active_names:
            existing_file.unlink()
            removed_tools.append(existing_file.stem)

    written_tools: list[str] = []
    for tool in tools:
        params = tool.parameters
        assert isinstance(params, dict) and params is not None, (
            f"Tool '{tool.name}' generated null or invalid parameters schema: {params!r}"
        )
        assert params.get("type") == "object", (
            f"Tool '{tool.name}' parameters schema must have type='object', got {params.get('type')!r}"
        )
        payload = {
            "name": tool.name,
            "description": tool.description,
            "parameters": params,
        }
        schema_file = target_dir / f"{tool.name}.json"
        schema_file.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        written_tools.append(tool.name)

    return {
        "status": "ok",
        "schema_dir": str(target_dir),
        "tool_count": len(written_tools),
        "tools": sorted(written_tools),
        "removed_tools": sorted(removed_tools),
    }


@mcp.tool()
def export_project(
    target_path: str,
    output_path: str,
    export_type: str = "pcb_svg",
    layers: list[str] | None = None,
    width: int = 1600,
    height: int = 900,
    side: str = "top",
    quality: str = "basic",
    refill_zones: bool = True,
) -> dict[str, Any]:
    """Export KiCad project outputs (gerbers, drill, step, sch_pdf, pcb_svg, or 3d render) to output_path."""
    cli = _get_cli()
    kind = export_type.strip().lower()
    if kind == "gerbers":
        return cli.export_gerbers(
            pcb_path=target_path,
            output_dir=output_path,
            layers=layers,
            refill_zones=refill_zones,
        )
    if kind == "drill":
        return cli.export_drill(pcb_path=target_path, output_dir=output_path)
    if kind == "step":
        return cli.export_step(
            pcb_path=target_path,
            output_path=output_path,
            refill_zones=refill_zones,
        )
    if kind in ("sch_pdf", "pdf"):
        return cli.export_sch_pdf(sch_path=target_path, output_path=output_path)
    if kind in ("pcb_svg", "svg"):
        return cli.export_pcb_svg(
            pcb_path=target_path,
            output_path=output_path,
            layers=layers,
            refill_zones=refill_zones,
        )
    if kind in ("3d", "render_3d", "png"):
        return cli.export_pcb_render(
            pcb_path=target_path,
            output_path=output_path,
            width=width,
            height=height,
            side=side,
            quality=quality,
            refill_zones=refill_zones,
        )
    return {
        "status": "error",
        "error": f"Unsupported export_type '{export_type}'. Use one of: gerbers, drill, step, sch_pdf, pcb_svg, 3d.",
    }


@mcp.tool()
def run_validations(
    target_path: str = "",
    validation_type: str = "drc",
    output_path: str | None = None,
    schematic_parity: bool = True,
    refill_zones: bool = True,
    save_board: bool = False,
) -> dict[str, Any]:
    """Run KiCad DRC, ERC, footprint/symbol SVG validation, lock check, or CLI version lookup."""
    cli = _get_cli()
    kind = validation_type.strip().lower()
    if kind == "drc":
        return cli.run_drc(
            pcb_path=target_path,
            output_path=output_path,
            schematic_parity=schematic_parity,
            refill_zones=refill_zones,
            save_board=save_board,
        )
    if kind == "erc":
        return cli.run_erc(sch_path=target_path, output_path=output_path)
    if kind == "footprint":
        return cli.validate_footprint(fp_path=target_path, output_dir=output_path)
    if kind == "symbol":
        return cli.validate_symbol(sym_path=target_path, output_dir=output_path)
    if kind in ("locks", "lock"):
        return pcb_inspector.check_kicad_lock_files(target_path=target_path)
    if kind == "version":
        return {"status": "ok", "kicad_version": cli.get_version()}
    return {
        "status": "error",
        "error": f"Unsupported validation_type '{validation_type}'. Use one of: drc, erc, footprint, symbol, locks, version.",
    }


@mcp.tool()
def refill_pcb_zones(
    pcb_path: str,
    output_path: str | None = None,
    force_when_locked: bool = False,
) -> dict[str, Any]:
    """Refill copper zones on a `.kicad_pcb` board in-place or to a staged output copy when locked."""
    return _get_cli().refill_pcb_zones(
        pcb_path=pcb_path,
        output_path=output_path,
        force_when_locked=force_when_locked,
    )


@mcp.tool()
def inspect_pcb_layout(pcb_path: str, compact: bool = True) -> dict[str, Any]:
    """Parse `.kicad_pcb` board bounds, stackup, silkscreen audit, footprints, traces, vias, zones, and locks."""
    return pcb_inspector.inspect_pcb_layout(pcb_path=pcb_path, compact=compact)


@mcp.tool()
def find_unrouted_pads(
    pcb_path: str,
    net_filter: str | None = None,
    tolerance_mm: float = 0.05,
) -> dict[str, Any]:
    """Detect pads on multi-pad nets missing explicit copper segments or relying solely on zone pours."""
    return pcb_inspector.find_unrouted_pads(
        pcb_path=pcb_path,
        net_filter=net_filter,
        tolerance_mm=tolerance_mm,
    )


@mcp.tool()
def check_mechanical_fit(
    pcb_path: str,
    min_clearance_mm: float = 0.5,
    connector_clearance_mm: float = 2.0,
    standoff_keepout_dia_mm: float = 6.5,
    standoff_height_mm: float = 10.0,
    mezzanine_modules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit PCB footprint collisions, edge overhangs, connector clearances, and standoff/mezzanine fit."""
    return pcb_inspector.check_mechanical_fit(
        pcb_path=pcb_path,
        min_clearance_mm=min_clearance_mm,
        connector_clearance_mm=connector_clearance_mm,
        standoff_keepout_dia_mm=standoff_keepout_dia_mm,
        standoff_height_mm=standoff_height_mm,
        mezzanine_modules=mezzanine_modules,
    )


@mcp.tool()
def analyze_pcb_traces(
    pcb_path: str,
    net_currents_a: dict[str, float] | None = None,
    copper_oz: float = 1.0,
    max_temp_rise_c: float = 10.0,
    max_voltage_drop_v: float = 0.1,
    neckdown_max_length_mm: float = 3.0,
) -> dict[str, Any]:
    """Analyze PCB power traces (IPC-2221, neck-down, voltage drop) and critical signal lengths."""
    return pcb_inspector.analyze_pcb_traces(
        pcb_path=pcb_path,
        net_currents_a=net_currents_a,
        copper_oz=copper_oz,
        max_temp_rise_c=max_temp_rise_c,
        max_voltage_drop_v=max_voltage_drop_v,
        neckdown_max_length_mm=neckdown_max_length_mm,
    )


@mcp.tool()
def manage_design_decisions(
    project_dir: str,
    action: str = "check",
    decisions: dict[str, Any] | None = None,
    bom_or_pcb_path: str | None = None,
) -> dict[str, Any]:
    """Check, load, or save persistent Step 1.5 architectural choices in `design_decisions.json`."""
    return pcb_inspector.manage_design_decisions(
        project_dir=project_dir,
        action=action,
        decisions=decisions,
        bom_or_pcb_path=bom_or_pcb_path,
    )


@mcp.tool()
def list_modules() -> Any:
    """List all supported hardware modules in the parametric `generators` package."""
    return _call_generator("list_supported_modules")


@mcp.tool()
def generate_module(
    module_name: str,
    output_dir: str,
    kind: str = "both",
    overrides: dict[str, Any] | None = None,
) -> Any:
    """Generate KiCad 10 `.kicad_mod` footprint and/or `.kicad_sym` symbol files for a supported module."""
    normalized = kind.strip().lower()
    if normalized == "footprint":
        return _call_generator("generate_footprint", module_name, output_dir, overrides)
    if normalized == "symbol":
        return _call_generator("generate_symbol", module_name, output_dir, overrides)
    if normalized == "both":
        fp_res = _call_generator("generate_footprint", module_name, output_dir, overrides)
        sym_res = _call_generator("generate_symbol", module_name, output_dir, overrides)
        ok = fp_res.get("status") != "error" and sym_res.get("status") != "error"
        return {
            "status": "ok" if ok else "error",
            "module_name": module_name,
            "footprint": fp_res,
            "symbol": sym_res,
        }
    return {
        "status": "error",
        "error": f"Unsupported kind '{kind}'. Use one of: footprint, symbol, both.",
    }


@mcp.tool()
def parse_bom(source: str) -> Any:
    """Parse a BOM specification string or file path into components, board properties, and connector details."""
    return _call_generator("parse_bom_spec", source)


@mcp.tool()
def create_kicad_project(
    project_dir: str,
    project_name: str = "LED_Controller",
    width_mm: float = 100.0,
    height_mm: float = 80.0,
    board_thickness_mm: float = 1.6,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a new KiCad project and empty PCB outline for subsequent MCP editing."""
    return project_generator.create_kicad_project(
        project_dir=project_dir,
        project_name=project_name,
        width_mm=width_mm,
        height_mm=height_mm,
        board_thickness_mm=board_thickness_mm,
        overwrite=overwrite,
    )


@mcp.tool()
def add_footprints_to_pcb(
    pcb_path: str,
    footprints: list[dict[str, Any]],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Insert generated `.kicad_mod` footprints into a PCB at explicit positions."""
    return pcb_editor.add_footprints_to_pcb(
        pcb_path=pcb_path, footprints=footprints, output_path=output_path
    )


@mcp.tool()
def edit_pcb_layout(
    pcb_path: str,
    board_outline: dict[str, Any] | None = None,
    footprint_placements: dict[str, dict[str, Any]] | None = None,
    zones: list[dict[str, Any]] | None = None,
    output_path: str | None = None,
    replace_footprints: dict[str, str | dict[str, Any]] | None = None,
    auto_fit_outline: dict[str, Any] | float | None = None,
    clean_silkscreen: bool | dict[str, Any] = False,
) -> dict[str, Any]:
    """Update board outline (`Edge.Cuts`), footprint placements/replacements, silkscreen, and copper zones."""
    return pcb_editor.edit_pcb_layout(
        pcb_path=pcb_path,
        board_outline=board_outline,
        footprint_placements=footprint_placements,
        zones=zones,
        output_path=output_path,
        replace_footprints=replace_footprints,
        auto_fit_outline=auto_fit_outline,
        clean_silkscreen=clean_silkscreen,
    )


@mcp.tool()
def add_nets_to_pcb(
    pcb_path: str,
    nets: list[str | dict[str, Any]],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Declare electrical nets in a legacy or KiCad 10 inline-net PCB."""
    return electrical_editor.add_nets_to_pcb(pcb_path, nets, output_path)


@mcp.tool()
def assign_pad_nets(
    pcb_path: str,
    assignments: list[dict[str, Any]],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Assign net names to PCB pads using footprint references and pad numbers."""
    return electrical_editor.assign_pad_nets(pcb_path, assignments, output_path)


@mcp.tool()
def create_netlist_from_bom(
    output_path: str,
    nets: list[dict[str, Any]],
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write a validated connectivity manifest for schematic, PCB, and routing stages."""
    return electrical_editor.create_netlist_from_bom(output_path, nets, components)


@mcp.tool()
def apply_netlist_to_pcb(
    pcb_path: str,
    netlist_path: str,
    output_path: str | None = None,
    gnd_zone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a netlist manifest to PCB pads and optionally add a GND copper zone."""
    return electrical_editor.apply_netlist_to_pcb(
        pcb_path, netlist_path, output_path, gnd_zone
    )


@mcp.tool()
def run_electrical_pipeline(
    pcb_path: str,
    netlist_path: str | None = None,
    output_path: str | None = None,
    gnd_zone: dict[str, Any] | None = None,
    route: bool = True,
    nets: list[str] | None = None,
    grid_step_mm: float = 0.5,
    trace_width_mm: float = 0.3,
    clearance_mm: float = 0.25,
    refill_zones: bool = False,
    run_drc: bool = False,
    drc_output_path: str | None = None,
    create_if_missing: bool = True,
    width_mm: float = 100.0,
    height_mm: float = 80.0,
) -> dict[str, Any]:
    """Run the deterministic electrical PCB pipeline (init, netlist, A* autorouting, inspection, DRC) in one call."""
    return pipeline.run_electrical_pipeline(
        pcb_path,
        netlist_path,
        output_path,
        gnd_zone,
        route,
        nets,
        grid_step_mm,
        trace_width_mm,
        clearance_mm,
        refill_zones,
        run_drc,
        drc_output_path,
        create_if_missing,
        width_mm,
        height_mm,
    )


@mcp.tool()
def create_schematic(
    output_path: str,
    project_name: str,
    components: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    title: str = "",
) -> dict[str, Any]:
    """Generate a KiCad 10 schematic with embedded symbols, pins, wires, and net labels."""
    return schematic_generator.create_schematic(
        output_path, project_name, components, nets, title
    )


@mcp.tool()
def edit_pcb_traces(
    pcb_path: str,
    add_segments: list[dict[str, Any]] | None = None,
    add_vias: list[dict[str, Any]] | None = None,
    clear_nets: list[str | int] | None = None,
    clear_bbox: dict[str, Any] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Add or remove copper `(segment ...)` and `(via ...)` items with KiCad-version-safe net encoding."""
    return pcb_editor.edit_pcb_traces(
        pcb_path=pcb_path,
        add_segments=add_segments,
        add_vias=add_vias,
        clear_nets=clear_nets,
        clear_bbox=clear_bbox,
        output_path=output_path,
    )


@mcp.tool()
def autoroute_pcb_nets(
    pcb_path: str,
    nets: list[str] | None = None,
    grid_step_mm: float = 0.5,
    trace_width_mm: float = 0.3,
    clearance_mm: float = 0.25,
    allow_vias: bool = True,
    via_size_mm: float = 0.6,
    via_drill_mm: float = 0.3,
    output_path: str | None = None,
    net_widths: dict[str, float] | None = None,
    auto_neckdown: bool = True,
) -> dict[str, Any]:
    """Autoroute unrouted connections on a 2-layer `.kicad_pcb` with per-net widths and pad neck-down."""
    return pcb_editor.autoroute_pcb_nets(
        pcb_path=pcb_path,
        nets=nets,
        grid_step_mm=grid_step_mm,
        trace_width_mm=trace_width_mm,
        clearance_mm=clearance_mm,
        allow_vias=allow_vias,
        via_size_mm=via_size_mm,
        via_drill_mm=via_drill_mm,
        output_path=output_path,
        net_widths=net_widths,
        auto_neckdown=auto_neckdown,
    )


if __name__ == "__main__":
    mcp.run()
