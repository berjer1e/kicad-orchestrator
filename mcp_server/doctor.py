"""Environment and installation diagnostics for every supported host."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from cli_wrapper import discover_kicad_cli
except ImportError:
    from .cli_wrapper import discover_kicad_cli


ROOT = Path(__file__).resolve().parent.parent


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    """Format a single diagnostic check item."""
    return {"name": name, "ok": ok, "detail": detail}


def diagnose() -> dict[str, Any]:
    """Run environment and installation diagnostics for Python, MCP, schemas, and `kicad-cli`."""
    checks: list[dict[str, Any]] = [
        _check("python", sys.version_info >= (3, 11), sys.version.split()[0]),
        _check(
            "mcp_dependency",
            importlib.util.find_spec("mcp") is not None,
            "installed" if importlib.util.find_spec("mcp") else "missing; install mcp_server/requirements.txt",
        ),
    ]
    kicad_cli_ok = False
    try:
        cli = discover_kicad_cli()
        kicad_cli_ok = True
        checks.append(_check("kicad_cli", True, str(cli)))
    except FileNotFoundError as exc:
        checks.append(_check("kicad_cli", False, str(exc)))

    schema_dir = ROOT / "mcp_server" / "schemas"
    invalid: list[str] = []
    for path in sorted(schema_dir.glob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid.append(str(path.relative_to(ROOT)))
    checks.append(
        _check(
            "schemas",
            not invalid,
            "all valid" if not invalid else f"invalid: {', '.join(invalid)}",
        )
    )
    checks.append(_check("mcp_config", (ROOT / "plugin" / "mcp_config.json").is_file(), "present"))
    checks.append(_check("agent_contract", (ROOT / "AGENTS.md").is_file(), "present"))
    import pcb_editor

    java_bin, jar_path, kicad_py, fr_reason = pcb_editor._discover_freerouting_env()
    freerouting_ready = bool(java_bin and jar_path and kicad_py)
    core_ok = all(item["ok"] for item in checks if item["name"] != "kicad_cli")
    return {
        "status": "ok" if (core_ok and kicad_cli_ok) else ("degraded_no_kicad_cli" if core_ok else "blocked"),
        "offline_s_expr_tools_ready": core_ok,
        "kicad_cli_ready": kicad_cli_ok,
        "freerouting_ready": freerouting_ready,
        "freerouting_detail": str(jar_path) if freerouting_ready else f"{fr_reason}; using built-in Python A* fallback",
        "checks": checks,
        "kicad_cli_path_env": os.environ.get("KICAD_CLI_PATH"),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `doctor.py`."""
    parser = argparse.ArgumentParser(description="Diagnose KiCad MCP host setup.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)
    result = diagnose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
