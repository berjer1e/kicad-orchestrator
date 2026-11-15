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
    return {"name": name, "ok": ok, "detail": detail}


def diagnose() -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        _check("python", sys.version_info >= (3, 11), sys.version.split()[0]),
        _check(
            "mcp_dependency",
            importlib.util.find_spec("mcp") is not None,
            "installed" if importlib.util.find_spec("mcp") else "missing; install mcp_server/requirements.txt",
        ),
    ]
    try:
        cli = discover_kicad_cli()
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
    return {
        "status": "ok" if all(item["ok"] for item in checks) else "blocked",
        "checks": checks,
        "kicad_cli_path_env": os.environ.get("KICAD_CLI_PATH"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose KiCad MCP host setup.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)
    result = diagnose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
