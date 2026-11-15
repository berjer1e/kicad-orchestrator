"""Electrical connectivity mutators for KiCad PCB files.

The KiCad 10 files used by this project store net names inline on pads and
tracks. Older files may still contain a top-level integer net table; both
formats are supported here without importing ``pcbnew``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import pcb_editor
except ImportError:
    from . import pcb_editor


def _resolve_output(
    pcb_path: str,
    output_path: str | None,
    suffix: str,
) -> tuple[Path, Path]:
    source = Path(pcb_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PCB file does not exist: {source}")
    if output_path:
        destination = Path(output_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
    else:
        lock_info = pcb_editor.pcb_inspector.check_kicad_lock_files(str(source))
        destination = (
            source.with_name(f"{source.stem}{suffix}{source.suffix}")
            if lock_info.get("target_locked")
            else source
        )
    return source, destination


def _extract_footprint_blocks(text: str) -> list[tuple[int, int, str]]:
    return [
        (start, end, block)
        for start, end, tag, block in pcb_editor._extract_top_level_blocks(text, {"footprint"})
        if tag == "footprint"
    ]


def _quote_net(name: str) -> str:
    value = str(name).strip()
    if not value:
        raise ValueError("Net names must be non-empty strings.")
    if "\n" in value or "\r" in value or '"' in value:
        raise ValueError(f"Invalid net name: {name!r}")
    return value


def _existing_net_names(text: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(r'\(net(?:\s+\d+)?\s+"([^"]*)"\)', text)
    } | {
        match.group(1)
        for match in re.finditer(r'\(net\s+"([^"]*)"\)', text)
    }


def add_nets_to_pcb(
    pcb_path: str,
    nets: list[str | dict[str, Any]],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Declare PCB nets, supporting both legacy net tables and KiCad 10 inline nets."""
    source, destination = _resolve_output(pcb_path, output_path, "_nets")
    text = source.read_text(encoding="utf-8")
    requested: list[str] = []
    for item in nets:
        name = item.get("name") if isinstance(item, dict) else item
        normalized = _quote_net(str(name))
        if normalized not in requested:
            requested.append(normalized)

    existing = _existing_net_names(text)
    added = [name for name in requested if name not in existing]
    legacy_matches = list(re.finditer(r'^\s*\(net\s+(\d+)\s+"([^"]*)"\)', text, re.MULTILINE))
    if legacy_matches and added:
        next_id = max(int(match.group(1)) for match in legacy_matches) + 1
        anchor = legacy_matches[-1].end()
        declarations = "".join(
            f'\n\t(net {next_id + index} "{name}")' for index, name in enumerate(added)
        )
        text = text[:anchor] + declarations + text[anchor:]
    # Modern KiCad stores net names on pads/tracks.  No synthetic top-level
    # declaration is inserted because that would make the file invalid.
    destination.write_text(text, encoding="utf-8")
    return {
        "status": "ok" if destination == source else "ok_staged",
        "saved_path": str(destination),
        "requested_nets": requested,
        "existing_nets": sorted(existing),
        "added_nets": added,
        "format": "legacy_net_table" if legacy_matches else "kicad10_inline",
    }


def _replace_or_insert_pad_net(pad_block: str, net_name: str) -> str:
    quoted = _quote_net(net_name)
    replacement = f'\t\t\t(net "{quoted}")'
    if re.search(r'\(net\s+(?:"[^"]*"|\d+)\)', pad_block):
        return re.sub(
            r'\t+\(net\s+(?:"[^"]*"|\d+)\)',
            replacement,
            pad_block,
            count=1,
        )
    marker = re.search(r'\n(\s*)\((?:pinfunction|pintype|uuid)\b', pad_block)
    if marker:
        return pad_block[: marker.start()] + "\n" + replacement + pad_block[marker.start() :]
    close = pad_block.rfind(")")
    return pad_block[:close] + "\n" + replacement + "\n" + pad_block[close:]


def assign_pad_nets(
    pcb_path: str,
    assignments: list[dict[str, Any]],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Assign net names to footprint pads by reference and pad number."""
    source, destination = _resolve_output(pcb_path, output_path, "_pad_nets")
    text = source.read_text(encoding="utf-8")
    by_ref: dict[str, dict[str, str]] = {}
    for assignment in assignments:
        reference = str(assignment.get("reference", "")).strip()
        pad_number = str(assignment.get("pad", assignment.get("pad_number", ""))).strip()
        net = _quote_net(str(assignment.get("net", "")))
        if not reference or not pad_number:
            raise ValueError("Each assignment requires reference, pad, and net.")
        by_ref.setdefault(reference, {})[pad_number] = net

    changed: list[dict[str, str]] = []
    parts: list[str] = []
    last = 0
    for start, end, block in _extract_footprint_blocks(text):
        ref_match = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
        reference = ref_match.group(1) if ref_match else ""
        updates = by_ref.get(reference, {})
        if not updates:
            continue
        updated = block
        pad_blocks = pcb_editor._extract_top_level_blocks(updated, {"pad"})
        for pad_start, pad_end, _tag, pad_block in reversed(pad_blocks):
            number_match = re.match(r'\(pad\s+"([^"]+)"', pad_block)
            if not number_match or number_match.group(1) not in updates:
                continue
            net = updates[number_match.group(1)]
            replacement = _replace_or_insert_pad_net(pad_block, net)
            updated = updated[:pad_start] + replacement + updated[pad_end:]
            changed.append({"reference": reference, "pad": number_match.group(1), "net": net})
        parts.append(text[last:start])
        parts.append(updated)
        last = end
    if changed:
        parts.append(text[last:])
        text = "".join(parts)
    missing = [
        assignment
        for assignment in assignments
        if not any(
            item["reference"] == str(assignment.get("reference", ""))
            and item["pad"] == str(assignment.get("pad", assignment.get("pad_number", "")))
            for item in changed
        )
    ]
    destination.write_text(text, encoding="utf-8")
    return {
        "status": "ok" if destination == source else "ok_staged",
        "saved_path": str(destination),
        "assigned": changed,
        "missing_assignments": missing,
    }


def create_netlist_from_bom(
    output_path: str,
    nets: list[dict[str, Any]],
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write a portable connectivity manifest consumed by PCB and schematic workflows."""
    normalized_nets: list[dict[str, Any]] = []
    for item in nets:
        name = _quote_net(str(item.get("name", "")))
        connections = item.get("connections", [])
        if not isinstance(connections, list):
            raise ValueError(f"Connections for net {name!r} must be a list.")
        normalized_nets.append(
            {
                "name": name,
                "class": str(item.get("class", "Default")),
                "current_a": float(item.get("current_a", 0.0)),
                "connections": [
                    {"reference": str(conn["reference"]), "pad": str(conn["pad"])}
                    for conn in connections
                ],
            }
        )
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "kicad-orchestrator-netlist-v1",
        "components": components or [],
        "nets": normalized_nets,
    }
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "output_path": str(destination),
        "net_count": len(normalized_nets),
        "connection_count": sum(len(net["connections"]) for net in normalized_nets),
    }


def apply_netlist_to_pcb(
    pcb_path: str,
    netlist_path: str,
    output_path: str | None = None,
    gnd_zone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a connectivity manifest and optionally add its GND copper zone."""
    manifest = Path(netlist_path).expanduser().resolve()
    if not manifest.is_file():
        raise FileNotFoundError(f"Netlist file does not exist: {manifest}")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("nets"), list):
        raise ValueError("Netlist must be an object containing a 'nets' list.")

    source, destination = _resolve_output(pcb_path, output_path, "_electrical")
    destination_str = str(destination)
    net_specs = payload["nets"]
    nets = [
        {
            "name": item["name"],
            "class": item.get("class", "Default"),
            "current_a": item.get("current_a", 0.0),
        }
        for item in net_specs
    ]
    assignments = [
        {
            "reference": connection["reference"],
            "pad": connection["pad"],
            "net": item["name"],
        }
        for item in net_specs
        for connection in item.get("connections", [])
    ]
    declared = add_nets_to_pcb(str(source), nets, destination_str)
    assigned = assign_pad_nets(destination_str, assignments, destination_str)
    zone_result: dict[str, Any] | None = None
    if gnd_zone is not None:
        zone = dict(gnd_zone)
        zone.setdefault("net", "GND")
        zone_result = pcb_editor.edit_pcb_layout(
            pcb_path=destination_str,
            zones=[zone],
            output_path=destination_str,
        )
    return {
        "status": "ok",
        "saved_path": destination_str,
        "declared": declared,
        "assigned": assigned,
        "gnd_zone": zone_result,
        "missing_assignments": assigned["missing_assignments"],
    }
