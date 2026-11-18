"""FastMCP server exposing 10 unified CRUD/workflow tools for KiCad 10 hardware design."""

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
import electrical_editor
import pcb_editor
import pcb_inspector
import pipeline
import project_generator
import schematic_generator

DEFAULT_SCHEMA_DIR: Path = (
    Path.home() / ".gemini" / "antigravity" / "mcp" / "kicad-orchestrator_kicad_mcp"
)

mcp = FastMCP("kicad-mcp")
_cli: KiCadCLI | None = None


def _get_cli() -> KiCadCLI:
    """Lazily instantiate and return the cached `KiCadCLI` wrapper."""
    global _cli
    if _cli is None:
        _cli = KiCadCLI()
    return _cli


def _call_generator(func_name: str, *args: Any, **kwargs: Any) -> Any:
    """Import `generators.<func_name>` on demand and invoke it with clear error reporting."""
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
def project(
    action: str = "decisions",
    project_dir: str = "",
    project_name: str = "LED_Controller",
    width_mm: float = 100.0,
    height_mm: float = 80.0,
    board_thickness_mm: float = 1.6,
    overwrite: bool = False,
    source: str = "",
    decision_action: str = "check",
    decisions: dict[str, Any] | None = None,
    bom_or_pcb_path: str | None = None,
) -> Any:
    """Manage KiCad project lifecycle: `create` project files, `parse_bom` spec, or `decisions` (`check`/`load`/`save`)."""
    act = action.strip().lower()
    if act == "create":
        return project_generator.create_kicad_project(
            project_dir=project_dir,
            project_name=project_name,
            width_mm=width_mm,
            height_mm=height_mm,
            board_thickness_mm=board_thickness_mm,
            overwrite=overwrite,
        )
    if act in ("parse_bom", "parse"):
        return _call_generator("parse_bom_spec", source or bom_or_pcb_path or project_dir)
    if act in ("decisions", "check", "load", "save"):
        d_act = decision_action if act == "decisions" else act
        return pcb_inspector.manage_design_decisions(
            project_dir=project_dir,
            action=d_act,
            decisions=decisions,
            bom_or_pcb_path=bom_or_pcb_path,
        )
    return {
        "status": "error",
        "error": f"Unsupported project action '{action}'. Use: create, parse_bom, decisions.",
    }


@mcp.tool()
def module(
    action: str = "list",
    module_name: str = "",
    output_dir: str = "",
    kind: str = "both",
    overrides: dict[str, Any] | None = None,
) -> Any:
    """List supported hardware modules (`action='list'`) or generate `.kicad_mod`/`.kicad_sym` (`action='generate'`)."""
    act = action.strip().lower()
    if act == "list":
        return _call_generator("list_supported_modules")
    if act == "generate":
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
    return {"status": "error", "error": f"Unsupported module action '{action}'. Use: list, generate."}


@mcp.tool()
def schematic(
    action: str = "create",
    output_path: str = "",
    project_name: str = "",
    components: list[dict[str, Any]] | None = None,
    nets: list[dict[str, Any]] | None = None,
    title: str = "",
    remove_references: list[str] | None = None,
) -> dict[str, Any]:
    """CRUD for KiCad 10 `.kicad_sch`: `create` new schematic, `append` components/nets, or `remove` symbols by reference."""
    act = action.strip().lower()
    if act == "create":
        return schematic_generator.create_schematic(
            output_path=output_path,
            project_name=project_name,
            components=components or [],
            nets=nets or [],
            title=title,
            append=False,
        )
    if act in ("append", "add", "update"):
        return schematic_generator.create_schematic(
            output_path=output_path,
            project_name=project_name,
            components=components or [],
            nets=nets or [],
            title=title,
            append=True,
            remove_references=remove_references,
        )
    if act == "remove":
        return schematic_generator.create_schematic(
            output_path=output_path,
            project_name=project_name,
            components=components or [],
            nets=nets or [],
            title=title,
            append=True,
            remove_references=remove_references,
        )
    return {"status": "error", "error": f"Unsupported schematic action '{action}'. Use: create, append, remove."}


@mcp.tool()
def pcb_layout(
    action: str = "read",
    pcb_path: str = "",
    compact: bool = True,
    footprints: list[dict[str, Any]] | None = None,
    board_outline: dict[str, Any] | None = None,
    footprint_placements: dict[str, dict[str, Any]] | None = None,
    replace_footprints: dict[str, str | dict[str, Any]] | None = None,
    remove_footprints: list[str] | None = None,
    auto_fit_outline: dict[str, Any] | float | None = None,
    zones: list[dict[str, Any]] | None = None,
    clean_silkscreen: bool | dict[str, Any] = False,
    clear_graphics_layers: list[str] | None = None,
    graphics: list[dict[str, Any]] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """CRUD for `.kicad_pcb` layout: `read`, `add` footprints, `update` placements/replacements/Edge.Cuts/zones/graphics, or `remove` footprints/graphics."""
    act = action.strip().lower()
    if act in ("read", "inspect", "get"):
        return pcb_inspector.inspect_pcb_layout(pcb_path=pcb_path, compact=compact)
    if act == "add":
        return pcb_editor.add_footprints_to_pcb(
            pcb_path=pcb_path,
            footprints=footprints or [],
            output_path=output_path,
        )
    if act in ("update", "edit", "remove", "clean"):
        return pcb_editor.edit_pcb_layout(
            pcb_path=pcb_path,
            board_outline=board_outline,
            footprint_placements=footprint_placements,
            zones=zones,
            output_path=output_path,
            replace_footprints=replace_footprints,
            auto_fit_outline=auto_fit_outline,
            clean_silkscreen=clean_silkscreen,
            graphics=graphics,
            remove_footprints=remove_footprints,
            clear_graphics_layers=clear_graphics_layers,
        )
    return {"status": "error", "error": f"Unsupported pcb_layout action '{action}'. Use: read, add, update, remove."}


@mcp.tool()
def pcb_nets(
    action: str = "apply",
    pcb_path: str = "",
    output_path: str | None = None,
    nets: list[Any] | None = None,
    components: list[dict[str, Any]] | None = None,
    assignments: list[dict[str, Any]] | None = None,
    netlist_path: str = "",
    gnd_zone: dict[str, Any] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Manage PCB nets: `create_manifest` JSON, `declare` nets, `assign` pad nets (auto-UUID), or `apply` netlist manifest."""
    act = action.strip().lower()
    if act in ("create_manifest", "create_netlist"):
        return electrical_editor.create_netlist_from_bom(
            output_path=output_path or netlist_path,
            nets=nets or [],
            components=components,
        )
    if act in ("declare", "add"):
        return electrical_editor.add_nets_to_pcb(
            pcb_path=pcb_path,
            nets=nets or [],
            output_path=output_path,
        )
    if act == "assign":
        return electrical_editor.assign_pad_nets(
            pcb_path=pcb_path,
            assignments=assignments or [],
            output_path=output_path,
            strict=strict,
        )
    if act == "apply":
        return electrical_editor.apply_netlist_to_pcb(
            pcb_path=pcb_path,
            netlist_path=netlist_path,
            output_path=output_path,
            gnd_zone=gnd_zone,
        )
    return {"status": "error", "error": f"Unsupported pcb_nets action '{action}'. Use: create_manifest, declare, assign, apply."}


@mcp.tool()
def pcb_traces(
    action: str = "edit",
    pcb_path: str = "",
    output_path: str | None = None,
    add_segments: list[dict[str, Any]] | None = None,
    add_vias: list[dict[str, Any]] | None = None,
    buses: list[dict[str, Any]] | None = None,
    clear_nets: list[str | int] | None = None,
    clear_bbox: dict[str, Any] | None = None,
    clear_all: bool = False,
    nets: list[str] | None = None,
    grid_step_mm: float = 0.5,
    trace_width_mm: float = 0.3,
    clearance_mm: float = 0.25,
    allow_vias: bool = True,
    via_size_mm: float = 0.6,
    via_drill_mm: float = 0.3,
    net_widths: dict[str, float] | None = None,
    auto_neckdown: bool = True,
    router: str = "auto",
    freerouting_jar: str | None = None,
    freerouting_timeout_s: int = 120,
    force_when_locked: bool = False,
) -> dict[str, Any]:
    """Manage copper routing: `edit` (segments/vias/buses/clear), `autoroute` (Freerouting + A* fallback), or `refill_zones`."""
    act = action.strip().lower()
    if act in ("edit", "add", "clear", "buses"):
        return pcb_editor.edit_pcb_traces(
            pcb_path=pcb_path,
            add_segments=add_segments,
            add_vias=add_vias,
            clear_nets=clear_nets,
            clear_bbox=clear_bbox,
            output_path=output_path,
            buses=buses,
            clear_all=clear_all or act == "clear" and not clear_nets and not clear_bbox,
        )
    if act == "autoroute":
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
            router=router,
            freerouting_jar=freerouting_jar,
            freerouting_timeout_s=freerouting_timeout_s,
        )
    if act in ("refill_zones", "refill"):
        return _get_cli().refill_pcb_zones(
            pcb_path=pcb_path,
            output_path=output_path,
            force_when_locked=force_when_locked,
        )
    return {"status": "error", "error": f"Unsupported pcb_traces action '{action}'. Use: edit, autoroute, refill_zones."}


@mcp.tool()
def pcb_audit(
    pcb_path: str,
    check: str = "all",
    net_filter: str | None = None,
    tolerance_mm: float = 0.05,
    net_currents_a: dict[str, float] | None = None,
    copper_oz: float = 1.0,
    max_temp_rise_c: float = 10.0,
    max_voltage_drop_v: float = 0.1,
    neckdown_max_length_mm: float = 3.0,
    min_clearance_mm: float = 0.5,
    connector_clearance_mm: float = 2.0,
    standoff_keepout_dia_mm: float = 6.5,
    standoff_height_mm: float = 10.0,
    mezzanine_modules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run PCB inspections: `unrouted` pads, IPC-2221 `traces`, `mechanical` fit (+ edge interface orientation), or `all`."""
    kind = check.strip().lower()
    if kind == "unrouted":
        return pcb_inspector.find_unrouted_pads(
            pcb_path=pcb_path,
            net_filter=net_filter,
            tolerance_mm=tolerance_mm,
        )
    if kind == "traces":
        return pcb_inspector.analyze_pcb_traces(
            pcb_path=pcb_path,
            net_currents_a=net_currents_a,
            copper_oz=copper_oz,
            max_temp_rise_c=max_temp_rise_c,
            max_voltage_drop_v=max_voltage_drop_v,
            neckdown_max_length_mm=neckdown_max_length_mm,
        )
    if kind == "mechanical":
        return pcb_inspector.check_mechanical_fit(
            pcb_path=pcb_path,
            min_clearance_mm=min_clearance_mm,
            connector_clearance_mm=connector_clearance_mm,
            standoff_keepout_dia_mm=standoff_keepout_dia_mm,
            standoff_height_mm=standoff_height_mm,
            mezzanine_modules=mezzanine_modules,
        )
    if kind == "all":
        unrouted = pcb_inspector.find_unrouted_pads(
            pcb_path=pcb_path,
            net_filter=net_filter,
            tolerance_mm=tolerance_mm,
        )
        traces = pcb_inspector.analyze_pcb_traces(
            pcb_path=pcb_path,
            net_currents_a=net_currents_a,
            copper_oz=copper_oz,
            max_temp_rise_c=max_temp_rise_c,
            max_voltage_drop_v=max_voltage_drop_v,
            neckdown_max_length_mm=neckdown_max_length_mm,
        )
        mech = pcb_inspector.check_mechanical_fit(
            pcb_path=pcb_path,
            min_clearance_mm=min_clearance_mm,
            connector_clearance_mm=connector_clearance_mm,
            standoff_keepout_dia_mm=standoff_keepout_dia_mm,
            standoff_height_mm=standoff_height_mm,
            mezzanine_modules=mezzanine_modules,
        )
        return {
            "status": "ok",
            "passed": bool(unrouted.get("passed") and traces.get("passed") and mech.get("passed")),
            "unrouted_pads": unrouted,
            "traces": traces,
            "mechanical_fit": mech,
        }
    return {"status": "error", "error": f"Unsupported pcb_audit check '{check}'. Use: all, unrouted, traces, mechanical."}


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
    """Run the deterministic electrical PCB pipeline (init, netlist, routing, inspection, DRC) in one call."""
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
def run_validations(
    target_path: str = "",
    validation_type: str = "drc",
    output_path: str | None = None,
    schematic_parity: bool = True,
    refill_zones: bool = True,
    save_board: bool = False,
    compact: bool = True,
    errors_only: bool = False,
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
            compact=compact,
            errors_only=errors_only,
        )
    if kind == "erc":
        return cli.run_erc(
            sch_path=target_path,
            output_path=output_path,
            compact=compact,
            errors_only=errors_only,
        )
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


if __name__ == "__main__":
    mcp.run()
