"""Electrical connectivity mutators for KiCad PCB files.

The KiCad 10 files used by this project store net names inline on pads and
tracks. Older files may still contain a top-level integer net table; both
formats are supported here without importing ``pcbnew``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from pathlib import Path
from typing import Any

NETCLASS_PRESETS: dict[str, dict[str, Any]] = {
    "standard_2layer": {
        "classes": [
            {"name": "Default", "track_width": 0.25, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Signal", "track_width": 0.25, "clearance": 0.20, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Bus", "track_width": 0.30, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Clock", "track_width": 0.25, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "track_width": 0.80, "clearance": 0.25, "via_diameter": 0.8, "via_drill": 0.4},
            {"name": "Power_High", "track_width": 1.50, "clearance": 0.30, "via_diameter": 1.0, "via_drill": 0.5},
        ],
        "patterns": [
            {"pattern": "*VBAT*", "netclass": "Power_High"},
            {"pattern": "*VBUS*", "netclass": "Power_High"},
            {"pattern": "*BATT*", "netclass": "Power_High"},
            {"pattern": "+12V*", "netclass": "Power_High"},
            {"pattern": "+24V*", "netclass": "Power_High"},
            {"pattern": "PWR*", "netclass": "Power_High"},
            {"pattern": "+5V*", "netclass": "Power"},
            {"pattern": "+3V3*", "netclass": "Power"},
            {"pattern": "VCC*", "netclass": "Power"},
            {"pattern": "VDD*", "netclass": "Power"},
            {"pattern": "GND*", "netclass": "Power"},
            {"pattern": "VSS*", "netclass": "Power"},
            {"pattern": "*XTAL*", "netclass": "Clock"},
            {"pattern": "*CLK*", "netclass": "Clock"},
            {"pattern": "*SCK*", "netclass": "Clock"},
            {"pattern": "*SDA*", "netclass": "Bus"},
            {"pattern": "*SCL*", "netclass": "Bus"},
            {"pattern": "*ROW*", "netclass": "Bus"},
            {"pattern": "*COL*", "netclass": "Bus"},
            {"pattern": "/*", "netclass": "Signal"},
        ],
    },
    "dense_smd": {
        "classes": [
            {"name": "Default", "track_width": 0.20, "clearance": 0.20, "via_diameter": 0.5, "via_drill": 0.25},
            {"name": "Signal", "track_width": 0.20, "clearance": 0.18, "via_diameter": 0.5, "via_drill": 0.25},
            {"name": "Bus", "track_width": 0.25, "clearance": 0.20, "via_diameter": 0.5, "via_drill": 0.25},
            {"name": "Clock", "track_width": 0.20, "clearance": 0.20, "via_diameter": 0.5, "via_drill": 0.25},
            {"name": "Power", "track_width": 0.60, "clearance": 0.20, "via_diameter": 0.7, "via_drill": 0.35},
            {"name": "Power_High", "track_width": 1.20, "clearance": 0.25, "via_diameter": 0.9, "via_drill": 0.45},
        ],
        "patterns": [],
    },
    "high_current": {
        "classes": [
            {"name": "Default", "track_width": 0.30, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Signal", "track_width": 0.30, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Bus", "track_width": 0.35, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Clock", "track_width": 0.30, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "track_width": 1.20, "clearance": 0.30, "via_diameter": 0.9, "via_drill": 0.45},
            {"name": "Power_High", "track_width": 2.50, "clearance": 0.35, "via_diameter": 1.2, "via_drill": 0.6},
        ],
        "patterns": [],
    },
}
NETCLASS_PRESETS["dense_smd"]["patterns"] = list(NETCLASS_PRESETS["standard_2layer"]["patterns"])
NETCLASS_PRESETS["high_current"]["patterns"] = list(NETCLASS_PRESETS["standard_2layer"]["patterns"])

try:
    import pcb_editor
except ImportError:
    from . import pcb_editor


def _resolve_output(
    pcb_path: str,
    output_path: str | None,
    suffix: str,
) -> tuple[Path, Path]:
    """Resolve source PCB path and lock-safe destination path."""
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
    """Extract top-level `(footprint ...)` spans from PCB S-expression text."""
    return [
        (start, end, block)
        for start, end, tag, block in pcb_editor._extract_top_level_blocks(text, {"footprint"})
        if tag == "footprint"
    ]


def _quote_net(name: str) -> str:
    """Validate and normalize a KiCad net name string."""
    value = str(name).strip()
    if not value:
        raise ValueError("Net names must be non-empty strings.")
    if "\n" in value or "\r" in value or '"' in value:
        raise ValueError(f"Invalid net name: {name!r}")
    return value


def _existing_net_names(text: str) -> set[str]:
    """Collect all net names currently declared or referenced in PCB text."""
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
    """Replace or insert `(net ...)` and ensure a `(uuid ...)` exists on a pad S-expression."""
    quoted = _quote_net(net_name)
    replacement = f'\t\t\t(net "{quoted}")'
    if re.search(r'\(net\s+(?:"[^"]*"|\d+)\)', pad_block):
        updated = re.sub(
            r'\t+\(net\s+(?:"[^"]*"|\d+)\)',
            replacement,
            pad_block,
            count=1,
        )
    else:
        marker = re.search(r'\n(\s*)\((?:pinfunction|pintype|uuid)\b', pad_block)
        if marker:
            updated = pad_block[: marker.start()] + "\n" + replacement + pad_block[marker.start() :]
        else:
            close = pad_block.rfind(")")
            updated = pad_block[:close] + "\n" + replacement + "\n" + pad_block[close:]
    if not re.search(r'\(uuid\s+"[^"]+"\)', updated):
        close = updated.rfind(")")
        updated = updated[:close] + f'\n\t\t\t(uuid "{uuid.uuid4()}")\n\t\t' + updated[close:]
    return updated


def assign_pad_nets(
    pcb_path: str,
    assignments: list[dict[str, Any]],
    output_path: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Assign net names to footprint pads by reference and pad number, failing on missing pads when strict."""
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
    status = "error" if (strict and missing) else ("ok" if destination == source else "ok_staged")
    result: dict[str, Any] = {
        "status": status,
        "saved_path": str(destination),
        "assigned": changed,
        "missing_assignments": missing,
    }
    if strict and missing:
        result["error"] = f"{len(missing)} pad assignment(s) did not match any footprint/pad on PCB."
    return result


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
        "status": "error" if assigned.get("status") == "error" else "ok",
        "saved_path": destination_str,
        "declared": declared,
        "assigned": assigned,
        "gnd_zone": zone_result,
        "missing_assignments": assigned["missing_assignments"],
        **({"error": assigned["error"]} if assigned.get("error") else {}),
    }


def set_netclasses(
    pcb_path: str,
    preset: str = "standard_2layer",
    classes: list[dict[str, Any]] | None = None,
    patterns: list[dict[str, str]] | None = None,
    min_track_width_mm: float = 0.2,
) -> dict[str, Any]:
    """Write KiCad 10 `net_settings.classes`, `netclass_patterns`, and `min_track_width` into `<stem>.kicad_pro`."""
    pcb = Path(pcb_path).expanduser().resolve()
    pro_path = pcb.with_suffix(".kicad_pro")
    data: dict[str, Any] = {}
    if pro_path.is_file():
        try:
            data = json.loads(pro_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    preset_key = str(preset).strip().lower()
    if preset_key not in NETCLASS_PRESETS:
        raise ValueError(f"Unknown preset '{preset}'. Use one of: {sorted(NETCLASS_PRESETS)}.")
    base = NETCLASS_PRESETS[preset_key]

    merged_classes: dict[str, dict[str, Any]] = {
        str(c["name"]): dict(c) for c in base["classes"]
    }
    for custom in classes or []:
        cname = str(custom["name"]).strip()
        entry = dict(merged_classes.get(cname, {"name": cname}))
        entry.update(custom)
        entry["name"] = cname
        if "track_width" in entry:
            entry["track_width"] = max(float(min_track_width_mm), float(entry["track_width"]))
        merged_classes[cname] = entry

    for entry in merged_classes.values():
        entry["track_width"] = max(float(min_track_width_mm), float(entry.get("track_width", 0.25)))

    merged_patterns = list(patterns or []) + [
        p
        for p in base["patterns"]
        if not any(str(cp.get("pattern")) == p["pattern"] for cp in (patterns or []))
    ]

    net_settings = data.setdefault("net_settings", {})
    net_settings["classes"] = list(merged_classes.values())
    net_settings["netclass_patterns"] = merged_patterns

    board_cfg = data.setdefault("board", {})
    design_cfg = board_cfg.setdefault("design_settings", {})
    rules_cfg = design_cfg.setdefault("rules", {})
    rules_cfg["min_track_width"] = max(float(min_track_width_mm), float(rules_cfg.get("min_track_width", 0.0)))

    pro_path.parent.mkdir(parents=True, exist_ok=True)
    pro_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "preset": preset_key,
        "pro_path": str(pro_path),
        "class_count": len(net_settings["classes"]),
        "pattern_count": len(merged_patterns),
        "min_track_width_mm": rules_cfg["min_track_width"],
    }


def resolve_net_widths_from_pro(
    pcb_path: str,
    net_names: list[str] | set[str],
) -> dict[str, float]:
    """Resolve per-net track widths from `<stem>.kicad_pro` classes, assignments, and glob patterns."""
    pro_path = Path(pcb_path).expanduser().resolve().with_suffix(".kicad_pro")
    if not pro_path.is_file():
        return {}
    try:
        data = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    net_settings = data.get("net_settings") or {}
    class_widths: dict[str, float] = {}
    for cls in net_settings.get("classes") or []:
        if isinstance(cls, dict) and cls.get("name") and cls.get("track_width") is not None:
            class_widths[str(cls["name"])] = float(cls["track_width"])

    assignments = net_settings.get("netclass_assignments") or {}
    patterns = net_settings.get("netclass_patterns") or []

    resolved: dict[str, float] = {}
    for raw_net in net_names:
        net = str(raw_net).strip()
        if not net:
            continue
        cls_name = assignments.get(net)
        if not cls_name:
            for pat_entry in patterns:
                if not isinstance(pat_entry, dict):
                    continue
                pat = str(pat_entry.get("pattern", ""))
                if pat and (fnmatch.fnmatchcase(net, pat) or fnmatch.fnmatch(net.upper(), pat.upper())):
                    cls_name = str(pat_entry.get("netclass", ""))
                    break
        if cls_name and cls_name in class_widths:
            resolved[net] = class_widths[cls_name]
    return resolved
