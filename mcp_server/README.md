# KiCad 10 MCP Server (`mcp_server/`)

Standalone Python [FastMCP](https://github.com/modelcontextprotocol/python-sdk) backend exposing **22 deterministic tools** for KiCad 10 (`kicad-cli` 10.0+), S-expression PCB/schematic generation, netlist application, A* autorouting, IPC-2221 trace auditing, and manufacturing exports.

This package works independently of any prompt skills or multi-agent plugins and can be connected to any MCP-compatible client (Claude Desktop, Claude Code, Cursor, Windsurf, Codex, OpenCode, Antigravity) or invoked directly from the command line via `run_tool.py`.

## Setup

```powershell
cd mcp_server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Verify Environment

```powershell
.\.venv\Scripts\python.exe doctor.py --json
```

## CLI Usage (Without an MCP Client)

```powershell
.\.venv\Scripts\python.exe run_tool.py inspect_pcb_layout --args '{\"pcb_path\": \"..\\examples\\calculator\\Calculator.kicad_pcb\"}'
.\.venv\Scripts\python.exe run_tool.py run_electrical_pipeline --args '{\"pcb_path\": \"..\\examples\\calculator\\Calculator.kicad_pcb\"}'
```
