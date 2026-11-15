"""Deterministic KiCad 10 schematic generator.

The generator intentionally emits a small embedded-symbol schematic from
structured component/pin/net data. The model supplies the design manifest;
this module owns UUIDs, symbol syntax, pin geometry, labels, and instances.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any


VERSION = "20260306"


def _uid() -> str:
    return str(uuid.uuid4())


def _fmt(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _font() -> str:
    return "\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)"


def _property(name: str, value: str, x: float, y: float, hidden: bool = False) -> str:
    hide = "\n\t\t\t(hide yes)" if hidden else ""
    return (
        f'\t\t(property "{_esc(name)}" "{_esc(value)}"\n'
        f"\t\t\t(at {_fmt(x)} {_fmt(y)} 0)\n"
        "\t\t\t(show_name no)\n"
        "\t\t\t(do_not_autoplace no)"
        f"{hide}\n"
        f"\t\t\t(effects\n{_font()}\n\t\t\t)\n"
        "\t\t)"
    )


def _pin(pin: dict[str, Any], x: float, y: float, angle: int) -> str:
    number = _esc(str(pin["number"]))
    name = _esc(str(pin.get("name", number)))
    electrical = str(pin.get("electrical_type", "passive"))
    return (
        f"\t\t\t\t(pin {electrical} line\n"
        f"\t\t\t\t\t(at {_fmt(x)} {_fmt(y)} {angle})\n"
        "\t\t\t\t\t(length 5.08)\n"
        f'\t\t\t\t\t(name "{name}"\n'
        f"\t\t\t\t\t\t(effects\n{_font()}\n\t\t\t\t\t\t)\n"
        "\t\t\t\t\t)\n"
        f'\t\t\t\t\t(number "{number}"\n'
        f"\t\t\t\t\t\t(effects\n{_font()}\n\t\t\t\t\t\t)\n"
        "\t\t\t\t\t)\n"
        "\t\t\t\t)"
    )


def _symbol_definition(lib_id: str, value: str, pins: list[dict[str, Any]]) -> str:
    left = [pin for index, pin in enumerate(pins) if index % 2 == 0]
    right = [pin for index, pin in enumerate(pins) if index % 2 == 1]
    height = max(5.08, (max(len(left), len(right)) + 1) * 2.54)
    blocks: list[str] = [
        f'\t\t(symbol "{_esc(lib_id)}"',
        "\t\t\t(exclude_from_sim no)",
        "\t\t\t(in_bom yes)",
        "\t\t\t(on_board yes)",
        "\t\t\t(in_pos_files yes)",
        "\t\t\t(duplicate_pin_numbers_are_jumpers no)",
        _property("Reference", "U", -7.62, height + 2.54),
        _property("Value", value, 0, -height - 2.54),
        _property("Footprint", "", 0, 0, True),
        _property("Datasheet", "", 0, 0, True),
        _property("Description", "", 0, 0, True),
        f'\t\t\t(symbol "{_esc(lib_id)}_0_1"',
        "\t\t\t\t(rectangle",
        f"\t\t\t\t\t(start -7.62 {_fmt(height)})",
        f"\t\t\t\t\t(end 7.62 -{_fmt(height)})",
        "\t\t\t\t\t(stroke (width 0.254) (type default))",
        "\t\t\t\t\t(fill (type background))",
        "\t\t\t\t)",
        "\t\t\t)",
        f'\t\t\t(symbol "{_esc(lib_id)}_1_1"',
    ]
    for index, pin in enumerate(left):
        blocks.append(_pin(pin, -12.7, (len(left) - 1 - index) * 2.54, 0))
    for index, pin in enumerate(right):
        blocks.append(_pin(pin, 12.7, (len(right) - 1 - index) * 2.54, 180))
    blocks.extend(["\t\t\t)", "\t\t\t(embedded_fonts no)", "\t\t)"])
    return "\n".join(blocks)


def _instance(
    project_uuid: str,
    project_name: str,
    component: dict[str, Any],
    lib_id: str,
    pin_uuids: dict[str, str],
) -> str:
    reference = str(component["reference"])
    value = str(component.get("value", lib_id.split(":")[-1]))
    x, y = component.get("at", [50, 50])
    footprint = str(component.get("footprint", ""))
    lines = [
        "\t(symbol",
        f'\t\t(lib_id "{_esc(lib_id)}")',
        f"\t\t(at {_fmt(x)} {_fmt(y)} 0)",
        "\t\t(unit 1)",
        "\t\t(body_style 1)",
        "\t\t(exclude_from_sim no)",
        "\t\t(in_bom yes)",
        "\t\t(on_board yes)",
        "\t\t(in_pos_files yes)",
        "\t\t(dnp no)",
        "\t\t(fields_autoplaced yes)",
        f'\t\t(uuid "{_uid()}")',
        _property("Reference", reference, float(x), float(y) - 15.24),
        _property("Value", value, float(x), float(y) + 15.24),
        _property("Footprint", footprint, float(x), float(y), True),
        _property("Datasheet", str(component.get("datasheet", "")), float(x), float(y), True),
        _property("Description", str(component.get("description", "")), float(x), float(y), True),
    ]
    for pin in component["pins"]:
        number = str(pin["number"])
        lines.extend([f'\t\t(pin "{_esc(number)}"', f'\t\t\t(uuid "{pin_uuids[number]}")', "\t\t)"])
    lines.extend(
        [
            "\t\t(instances",
            f'\t\t\t(project "{_esc(project_name)}"',
            f'\t\t\t\t(path "/{project_uuid}"',
            f'\t\t\t\t\t(reference "{_esc(reference)}")',
            "\t\t\t\t\t(unit 1)",
            "\t\t\t\t)",
            "\t\t\t)",
            "\t\t)",
            "\t)",
        ]
    )
    return "\n".join(lines)


def _label(name: str, x: float, y: float) -> str:
    return (
        f'\t(label "{_esc(name)}"\n'
        f"\t\t(at {_fmt(x)} {_fmt(y)} 0)\n"
        "\t\t(fields_autoplaced yes)\n"
        "\t\t(effects\n"
        "\t\t\t(font\n"
        "\t\t\t\t(size 1.27 1.27)\n"
        "\t\t\t)\n"
        "\t\t)\n"
        f'\t\t(uuid "{_uid()}")\n'
        "\t)"
    )


def create_schematic(
    output_path: str,
    project_name: str,
    components: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    title: str = "",
) -> dict[str, Any]:
    """Create a KiCad 10 schematic from structured components and net connections."""
    if not project_name.strip():
        raise ValueError("project_name must be non-empty.")
    if not components:
        raise ValueError("At least one component is required.")
    component_by_ref = {str(item["reference"]): item for item in components}
    if len(component_by_ref) != len(components):
        raise ValueError("Component references must be unique.")

    definitions: dict[str, str] = {}
    lib_ids: dict[str, str] = {}
    pin_uuids: dict[str, dict[str, str]] = {}
    for component in components:
        reference = str(component["reference"])
        pins = component.get("pins", [])
        if not pins:
            raise ValueError(f"Component {reference} has no pins.")
        numbers = [str(pin["number"]) for pin in pins]
        if len(set(numbers)) != len(numbers):
            raise ValueError(f"Component {reference} has duplicate pin numbers.")
        lib_id = str(component.get("lib_id", f"Orchestrator:{reference}"))
        lib_ids[reference] = lib_id
        definitions.setdefault(
            lib_id,
            _symbol_definition(lib_id, str(component.get("value", reference)), pins),
        )
        pin_uuids[reference] = {number: _uid() for number in numbers}

    project_uuid = _uid()
    lines = [
        "(kicad_sch",
        f"\t(version {VERSION})",
        '\t(generator "eeschema")',
        '\t(generator_version "10.0")',
        f'\t(uuid "{project_uuid}")',
        '\t(paper "A4")',
        "\t(lib_symbols",
        *definitions.values(),
        "\t)",
    ]
    if title:
        lines.extend(
            [
                f'\t(text "{_esc(title)}"',
                "\t\t(exclude_from_sim no)",
                "\t\t(at 50 20 0)",
                "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 2.54 2.54)\n\t\t\t)\n\t\t)",
                f'\t\t(uuid "{_uid()}")',
                "\t)",
            ]
        )
    lines.extend(
        _instance(project_uuid, project_name, component, lib_ids[str(component["reference"])], pin_uuids[str(component["reference"])])
        for component in components
    )

    positions: dict[tuple[str, str], tuple[float, float]] = {}
    for component in components:
        x, y = component.get("at", [50, 50])
        pins = component["pins"]
        left = [pin for index, pin in enumerate(pins) if index % 2 == 0]
        right = [pin for index, pin in enumerate(pins) if index % 2 == 1]
        for index, pin in enumerate(left):
            positions[(str(component["reference"]), str(pin["number"]))] = (
                float(x) - 12.7,
                float(y) + (len(left) - 1 - index) * 2.54,
            )
        for index, pin in enumerate(right):
            positions[(str(component["reference"]), str(pin["number"]))] = (
                float(x) + 12.7,
                float(y) + (len(right) - 1 - index) * 2.54,
            )

    for net in nets:
        name = str(net["name"])
        for connection in net.get("connections", []):
            key = (str(connection["reference"]), str(connection["pad"]))
            if key not in positions:
                raise ValueError(f"Net {name!r} references unknown pin {key[0]}:{key[1]}.")
            x, y = positions[key]
            lines.append(
                f'\t(wire\n\t\t(pts\n\t\t\t(xy {_fmt(x)} {_fmt(y)}) (xy {_fmt(x + 5.08)} {_fmt(y)})\n'
                '\t\t)\n\t\t(stroke (width 0) (type default))\n'
                f'\t\t(uuid "{_uid()}")\n\t)'
            )
            lines.append(_label(name, x + 5.08, y))
    lines.extend(["\t(sheet_instances", '\t\t(path "/"', '\t\t\t(page "1")', "\t\t)", "\t)", "\t(embedded_fonts no)", ")"])
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "output_path": str(destination),
        "project_name": project_name,
        "component_count": len(components),
        "net_count": len(nets),
        "connection_count": sum(len(net.get("connections", [])) for net in nets),
    }


def create_schematic_from_netlist(
    output_path: str,
    project_name: str,
    components: list[dict[str, Any]],
    netlist_path: str,
    title: str = "",
) -> dict[str, Any]:
    """Create a schematic using the same JSON netlist consumed by PCB tooling."""
    payload = json.loads(Path(netlist_path).expanduser().resolve().read_text(encoding="utf-8"))
    return create_schematic(output_path, project_name, components, payload["nets"], title)
