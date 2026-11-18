"""Direct CLI runner for KiCad MCP tools, schema sync, and disk-cached state summaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

_MCP_ROOT = Path(__file__).resolve().parent
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from doctor import diagnose

try:
    import server
    _SERVER_IMPORT_ERROR: str | None = None
except ModuleNotFoundError as exc:
    server = None
    _SERVER_IMPORT_ERROR = str(exc)

_TOOLS: dict[str, Any] = {}
if server is not None:
    _TOOLS = {
        "project": server.project,
        "module": server.module,
        "schematic": server.schematic,
        "pcb_layout": server.pcb_layout,
        "pcb_nets": server.pcb_nets,
        "pcb_traces": server.pcb_traces,
        "pcb_audit": server.pcb_audit,
        "run_electrical_pipeline": server.run_electrical_pipeline,
        "run_validations": server.run_validations,
        "export_project": server.export_project,
        "create_kicad_project": lambda **kw: server.project(action="create", **kw),
        "parse_bom": lambda **kw: server.project(action="parse_bom", **kw),
        "manage_design_decisions": lambda **kw: server.project(
            action="decisions",
            project_dir=kw.get("project_dir", ""),
            decision_action=kw.get("action", "check"),
            decisions=kw.get("decisions"),
            bom_or_pcb_path=kw.get("bom_or_pcb_path"),
        ),
        "list_modules": lambda **kw: server.module(action="list", **kw),
        "generate_module": lambda **kw: server.module(action="generate", **kw),
        "create_schematic": lambda **kw: server.schematic(
            action=("append" if kw.get("append") else "create"),
            output_path=kw.get("output_path", ""),
            project_name=kw.get("project_name", ""),
            components=kw.get("components"),
            nets=kw.get("nets"),
            title=kw.get("title", ""),
            remove_references=kw.get("remove_references"),
        ),
        "inspect_pcb_layout": lambda **kw: server.pcb_layout(action="read", **kw),
        "add_footprints_to_pcb": lambda **kw: server.pcb_layout(action="add", **kw),
        "edit_pcb_layout": lambda **kw: server.pcb_layout(action="update", **kw),
        "create_netlist_from_bom": lambda **kw: server.pcb_nets(action="create_manifest", **kw),
        "add_nets_to_pcb": lambda **kw: server.pcb_nets(action="declare", **kw),
        "assign_pad_nets": lambda **kw: server.pcb_nets(action="assign", **kw),
        "apply_netlist_to_pcb": lambda **kw: server.pcb_nets(action="apply", **kw),
        "edit_pcb_traces": lambda **kw: server.pcb_traces(action="edit", **kw),
        "autoroute_pcb_nets": lambda **kw: server.pcb_traces(action="autoroute", **kw),
        "refill_pcb_zones": lambda **kw: server.pcb_traces(action="refill_zones", **kw),
        "find_unrouted_pads": lambda **kw: server.pcb_audit(check="unrouted", **kw),
        "analyze_pcb_traces": lambda **kw: server.pcb_audit(check="traces", **kw),
        "check_mechanical_fit": lambda **kw: server.pcb_audit(check="mechanical", **kw),
    }


def run_tool(tool_name: str, args: dict[str, Any] | None = None) -> Any:
    """Invoke a consolidated KiCad MCP tool by name with keyword arguments.

    Args:
        tool_name: Registered tool key in `_TOOLS`.
        args: Optional dictionary of keyword arguments.

    Returns:
        JSON-serializable dictionary returned by the target tool.
    """
    func = _TOOLS.get(tool_name)
    if func is None:
        return {
            "status": "error",
            "error": (
                f"MCP server dependencies are unavailable: {_SERVER_IMPORT_ERROR}"
                if _SERVER_IMPORT_ERROR
                else f"Unknown tool '{tool_name}'. Available tools: {', '.join(sorted(_TOOLS))}."
            ),
        }
    kwargs = args or {}
    return func(**kwargs)


def save_state_summary(pcb_path: str, output_json: str) -> dict[str, Any]:
    """Run all core PCB inspections once and cache a unified compact state JSON file on disk.

    Args:
        pcb_path: Path to the target `.kicad_pcb` board file.
        output_json: Destination path for the cached JSON summary.

    Returns:
        Compact confirmation dictionary with `status`, `output_json`, `bytes`, and pass/fail flags.
    """
    resolved_pcb = Path(pcb_path).expanduser().resolve()
    out_file = Path(output_json).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    layout = server.pcb_layout(action="read", pcb_path=str(resolved_pcb), compact=True)
    audit = server.pcb_audit(pcb_path=str(resolved_pcb), check="all")
    unrouted = audit["unrouted_pads"]
    traces = audit["traces"]
    fit = audit["mechanical_fit"]

    payload = {
        "pcb_path": str(resolved_pcb),
        "inspect_pcb_layout": layout,
        "find_unrouted_pads": unrouted,
        "analyze_pcb_traces": traces,
        "check_mechanical_fit": fit,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    out_file.write_text(serialized, encoding="utf-8")

    return {
        "status": "ok",
        "pcb_path": str(resolved_pcb),
        "output_json": str(out_file),
        "bytes": len(serialized.encode("utf-8")),
        "passed": {
            "unrouted_pads": bool(unrouted.get("passed", False)),
            "traces": bool(traces.get("passed", False)),
            "mechanical_fit": bool(fit.get("passed", False)),
        },
        "locked": bool(layout.get("locked", False)),
    }


def _configure_utf8_stdout() -> None:
    """Configure UTF-8 console output on Windows when supported."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for invoking KiCad MCP tools, syncing schemas, or saving state summaries.

    Args:
        argv: Optional argument list overriding `sys.argv[1:]`.

    Returns:
        Process exit code (`0` on success, non-zero on error).
    """
    _configure_utf8_stdout()
    parser = argparse.ArgumentParser(description="Run a KiCad MCP tool directly.")
    parser.add_argument(
        "tool_name",
        nargs="?",
        choices=sorted([*list(_TOOLS), "doctor"]),
        help="Tool name from server.py",
    )
    parser.add_argument(
        "--args",
        default="{}",
        help="JSON object of keyword arguments for the tool",
    )
    parser.add_argument(
        "--args-file",
        help="Path to a UTF-8 JSON file containing keyword arguments (avoids Windows 32 KB CLI limits)",
    )
    parser.add_argument(
        "--sync-schemas",
        action="store_true",
        help="Write and validate JSON schema files for all MCP tools, then exit",
    )
    parser.add_argument(
        "--save-state-summary",
        nargs=2,
        metavar=("PCB_PATH", "OUTPUT_JSON"),
        help="Run layout/unrouted/trace/fit inspections once and write state JSON to disk",
    )
    parsed = parser.parse_args(argv)

    if parsed.sync_schemas:
        if server is None:
            print(json.dumps({"status": "error", "error": _SERVER_IMPORT_ERROR}))
            return 1
        result = server.sync_mcp_schemas()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ok" else 1

    if parsed.tool_name == "doctor":
        result = diagnose()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ok" else 1

    if parsed.save_state_summary:
        if server is None:
            print(json.dumps({"status": "error", "error": _SERVER_IMPORT_ERROR}))
            return 1
        pcb_path, output_json = parsed.save_state_summary
        result = save_state_summary(pcb_path=pcb_path, output_json=output_json)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ok" else 1

    if not parsed.tool_name:
        parser.error("Provide a tool_name, --sync-schemas, or --save-state-summary.")

    try:
        if parsed.args_file:
            args_path = Path(parsed.args_file).expanduser().resolve()
            if not args_path.is_file():
                print(json.dumps({"status": "error", "error": f"--args-file not found: {args_path}"}))
                return 1
            tool_args = json.loads(args_path.read_text(encoding="utf-8-sig"))
        else:
            tool_args = json.loads(parsed.args)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "error": f"Invalid arguments JSON: {exc}"}))
        return 1

    if not isinstance(tool_args, dict):
        print(json.dumps({"status": "error", "error": "Tool arguments must decode to a JSON object."}))
        return 1

    result = run_tool(parsed.tool_name, tool_args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not (isinstance(result, dict) and result.get("status") == "error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
