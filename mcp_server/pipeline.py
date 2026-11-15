"""High-level, single-call project workflows.

These workflows keep deterministic file operations in code so the model only
has to provide a project path and structured decisions instead of generating a
long sequence of tool calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

try:
    import electrical_editor
    import pcb_editor
    import pcb_inspector
except ImportError:
    from . import electrical_editor, pcb_editor, pcb_inspector


def _phase(
    phases: list[dict[str, Any]],
    name: str,
    action: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        result = action()
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    status = str(result.get("status", "ok"))
    phases.append({"phase": name, "status": status, "error": result.get("error")})
    return result


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    """Keep pipeline responses bounded while preserving actionable failures."""
    compact = {"status": result.get("status", "ok")}
    for key in (
        "saved_path",
        "missing_assignments",
        "failed_nets",
        "routed_connections",
        "unrouted_summary_by_net",
        "passed",
        "error",
    ):
        if key in result:
            compact[key] = result[key]
    return compact


def run_electrical_pipeline(
    pcb_path: str,
    netlist_path: str | None = None,
    output_path: str | None = None,
    gnd_zone: dict[str, Any] | None = None,
    route: bool = False,
    nets: list[str] | None = None,
    grid_step_mm: float = 0.5,
    trace_width_mm: float = 0.3,
    clearance_mm: float = 0.25,
    refill_zones: bool = False,
    run_drc: bool = False,
    drc_output_path: str | None = None,
) -> dict[str, Any]:
    """Run lock check, netlist application, routing, refill, and optional DRC."""
    source = Path(pcb_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PCB file does not exist: {source}")
    phases: list[dict[str, Any]] = []
    lock_result = _phase(
        phases,
        "locks",
        lambda: pcb_inspector.check_kicad_lock_files(str(source)),
    )
    if lock_result.get("target_locked") and output_path is None:
        output_path = str(source.with_name(f"{source.stem}_pipeline{source.suffix}"))

    current_path = output_path or str(source)
    if netlist_path:
        applied = _phase(
            phases,
            "netlist",
            lambda: electrical_editor.apply_netlist_to_pcb(
                str(source),
                netlist_path,
                current_path,
                gnd_zone,
            ),
        )
        if applied.get("status") == "error":
            return {"status": "error", "phases": phases}
        current_path = str(applied.get("saved_path", current_path))

    if route:
        route_result = _phase(
            phases,
            "routing",
            lambda: pcb_editor.autoroute_pcb_nets(
                current_path,
                nets=nets,
                grid_step_mm=grid_step_mm,
                trace_width_mm=trace_width_mm,
                clearance_mm=clearance_mm,
                output_path=current_path,
            ),
        )
        if route_result.get("status") == "error":
            return {"status": "error", "phases": phases}
        current_path = str(route_result.get("saved_path", current_path))

    if refill_zones:
        try:
            from cli_wrapper import KiCadCLI
        except ImportError:
            from .cli_wrapper import KiCadCLI
        refill_result = _phase(
            phases,
            "zone_refill",
            lambda: KiCadCLI().refill_pcb_zones(current_path, output_path=current_path),
        )
        if refill_result.get("status") == "error":
            return {"status": "error", "phases": phases}

    inspection = _phase(
        phases,
        "inspection",
        lambda: {
            **pcb_inspector.find_unrouted_pads(current_path),
            "status": "ok",
        },
    )
    validation: dict[str, Any] | None = None
    if run_drc:
        try:
            from cli_wrapper import KiCadCLI
        except ImportError:
            from .cli_wrapper import KiCadCLI
        validation = _phase(
            phases,
            "drc",
            lambda: KiCadCLI().run_drc(
                current_path,
                output_path=drc_output_path,
                schematic_parity=True,
                refill_zones=refill_zones,
            ),
        )

    failed = any(item["status"] == "error" for item in phases)
    return {
        "status": "error" if failed else "ok",
        "saved_path": current_path,
        "phases": phases,
        "inspection": _compact(inspection),
        "validation": _compact(validation) if validation else None,
    }
