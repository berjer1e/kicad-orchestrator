"""KiCad 10 S-expression footprint (`.kicad_mod`) generator.

Generates parametric KiCad 10.0 footprints (`version 20260206`) with `F.SilkS`,
`F.Fab`, `F.CrtYd`, pin labels, USB Type-C overhang keepouts where applicable,
and deterministic UUIDs.
"""

from pathlib import Path
from typing import Any
import uuid

from .templates import ModuleSpec, PinSpec, apply_spec_overrides, resolve_module_spec

KICAD_MOD_VERSION = "20260206"
GENERATOR_NAME = "kicad_mcp"
GENERATOR_VERSION = "10.0"


def _det_uuid(scope: str, key: str) -> str:
    """Return a deterministic UUIDv5 string for reproducible KiCad S-expressions."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"kicad_mcp:{scope}:{key}"))


def _fmt(val: float) -> str:
    """Format floating-point coordinates cleanly without trailing zeros."""
    rounded = round(float(val), 4)
    if rounded == -0.0:
        rounded = 0.0
    text = f"{rounded:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _esc(text: str) -> str:
    """Escape quotes and backslashes for KiCad S-expression string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _fp_line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer: str,
    width: float,
    scope: str,
    key: str,
) -> str:
    """Render a KiCad 10 `(fp_line ...)` element."""
    uid = _det_uuid(scope, key)
    return (
        f'\t(fp_line\n'
        f'\t\t(start {_fmt(x1)} {_fmt(y1)})\n'
        f'\t\t(end {_fmt(x2)} {_fmt(y2)})\n'
        f'\t\t(stroke\n'
        f'\t\t\t(width {_fmt(width)})\n'
        f'\t\t\t(type solid)\n'
        f'\t\t)\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t)'
    )


def _fp_rect_lines(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer: str,
    width: float,
    scope: str,
    prefix: str,
) -> list[str]:
    """Render a rectangle as four `(fp_line ...)` segments."""
    return [
        _fp_line(x1, y1, x2, y1, layer, width, scope, f"{prefix}_top"),
        _fp_line(x2, y1, x2, y2, layer, width, scope, f"{prefix}_right"),
        _fp_line(x2, y2, x1, y2, layer, width, scope, f"{prefix}_bottom"),
        _fp_line(x1, y2, x1, y1, layer, width, scope, f"{prefix}_left"),
    ]


def _fp_text(
    kind: str,
    text: str,
    x: float,
    y: float,
    layer: str,
    size: float,
    thickness: float,
    scope: str,
    key: str,
) -> str:
    """Render a KiCad 10 `(fp_text ...)` element."""
    uid = _det_uuid(scope, key)
    return (
        f'\t(fp_text {kind} "{_esc(text)}"\n'
        f'\t\t(at {_fmt(x)} {_fmt(y)} 0)\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(effects\n'
        f'\t\t\t(font\n'
        f'\t\t\t\t(size {_fmt(size)} {_fmt(size)})\n'
        f'\t\t\t\t(thickness {_fmt(thickness)})\n'
        f'\t\t\t)\n'
        f'\t\t)\n'
        f'\t)'
    )


def _property(
    name: str,
    value: str,
    x: float,
    y: float,
    layer: str,
    size: float,
    scope: str,
    hide: bool = False,
    unlocked: bool = False,
) -> str:
    """Render a KiCad 10 footprint `(property ...)` element."""
    uid = _det_uuid(scope, f"prop_{name}")
    lines = [
        f'\t(property "{_esc(name)}" "{_esc(value)}"',
        f'\t\t(at {_fmt(x)} {_fmt(y)} 0)',
    ]
    if unlocked:
        lines.append("\t\t(unlocked yes)")
    lines.append(f'\t\t(layer "{layer}")')
    if hide:
        lines.append("\t\t(hide yes)")
    lines.extend(
        [
            f'\t\t(uuid "{uid}")',
            "\t\t(effects",
            "\t\t\t(font",
            f"\t\t\t\t(size {_fmt(size)} {_fmt(size)})",
            "\t\t\t\t(thickness 0.15)",
            "\t\t\t)",
            "\t\t)",
            "\t)",
        ]
    )
    return "\n".join(lines)


def _pad_label_position(pin: PinSpec, spec: ModuleSpec) -> tuple[float, float]:
    """Compute a non-overlapping `F.SilkS` label position inside the board near the pad."""
    offset = spec.pad_size / 2.0 + 1.15
    if abs(pin.x) > abs(pin.y) or abs(pin.x) >= spec.board_width / 2.0 - spec.pad_size - 1.5:
        lx = pin.x + offset if pin.x < 0 else pin.x - offset
        ly = pin.y
    else:
        lx = pin.x
        ly = pin.y + offset if pin.y < 0 else pin.y - offset
    return (lx, ly)


def _pad_sexpr(pin: PinSpec, spec: ModuleSpec, scope: str) -> str:
    """Render a KiCad 10 `(pad ...)` element for a module pin."""
    uid = _det_uuid(scope, f"pad_{pin.number}_{pin.name}")
    is_smd = spec.attr == "smd"
    pad_type = "smd" if is_smd else "thru_hole"
    shape = pin.shape if pin.shape in ("circle", "rect", "oval", "roundrect") else "circle"

    lines = [
        f'\t(pad "{_esc(pin.number)}" {pad_type} {shape}',
        f'\t\t(at {_fmt(pin.x)} {_fmt(pin.y)})',
        f'\t\t(size {_fmt(spec.pad_size)} {_fmt(spec.pad_size)})',
    ]
    if not is_smd:
        lines.append(f"\t\t(drill {_fmt(spec.drill)})")
        lines.append('\t\t(layers "*.Cu" "*.Mask")')
    else:
        lines.append('\t\t(layers "F.Cu" "F.Mask" "F.Paste")')

    if shape == "roundrect":
        lines.append("\t\t(roundrect_rratio 0.25)")
    if not is_smd:
        lines.append("\t\t(remove_unused_layers no)")
    lines.append(f'\t\t(uuid "{uid}")')
    lines.append("\t)")
    return "\n".join(lines)


def build_footprint_sexpr(spec: ModuleSpec) -> str:
    """Build the complete KiCad 10 `.kicad_mod` S-expression string for a `ModuleSpec`."""
    hw = spec.board_width / 2.0
    hh = spec.board_height / 2.0
    scope = f"fp_{spec.name}_{spec.attr}_{len(spec.pins)}_{_fmt(spec.board_width)}x{_fmt(spec.board_height)}"

    pad_r = spec.pad_size / 2.0
    min_x = min([-hw - spec.usb_overhang_left] + [p.x - pad_r for p in spec.pins]) - 0.5
    max_x = max([hw] + [p.x + pad_r for p in spec.pins]) + 0.5
    min_y = min([-hh] + [p.y - pad_r for p in spec.pins]) - 0.5
    max_y = max([hh] + [p.y + pad_r for p in spec.pins]) + 0.5

    ref_y = min_y - 1.0
    val_y = max_y + 1.0

    parts: list[str] = [
        f'(footprint "{_esc(spec.name)}"',
        f"\t(version {KICAD_MOD_VERSION})",
        f'\t(generator "{GENERATOR_NAME}")',
        f'\t(generator_version "{GENERATOR_VERSION}")',
        '\t(layer "F.Cu")',
        f'\t(descr "{_esc(spec.description)}")',
        f'\t(tags "{_esc(spec.tags)}")',
        _property("Reference", "REF**", 0.0, ref_y, "F.SilkS", 1.0, scope),
        _property("Value", spec.name, 0.0, val_y, "F.Fab", 1.0, scope),
        _property("Datasheet", spec.datasheet, 0.0, 0.0, "F.Fab", 1.27, scope, hide=True, unlocked=True),
        _property("Description", spec.description, 0.0, 0.0, "F.Fab", 1.27, scope, hide=True, unlocked=True),
        f"\t(attr {spec.attr})",
        "\t(duplicate_pad_numbers_are_jumpers no)",
    ]

    silk_margin = 0.15
    parts.extend(
        _fp_rect_lines(
            -hw - silk_margin,
            -hh - silk_margin,
            hw + silk_margin,
            hh + silk_margin,
            "F.SilkS",
            0.12,
            scope,
            "silk_outline",
        )
    )

    p1 = spec.pins[0]
    mark_x = -hw - silk_margin - 0.45 if p1.x <= 0 else hw + silk_margin + 0.45
    mark_y = -hh - silk_margin - 0.45 if p1.y <= 0 else hh + silk_margin + 0.45
    parts.append(
        _fp_line(
            mark_x,
            p1.y - 0.6,
            mark_x,
            p1.y + 0.6,
            "F.SilkS",
            0.25,
            scope,
            "pin1_marker",
        )
    )
    if spec.name == "Mini560" and len(spec.pins) == 5:
        parts.append(
            _fp_line(
                p1.x - 0.8,
                mark_y,
                p1.x + 0.8,
                mark_y,
                "F.SilkS",
                0.25,
                scope,
                "pin1_marker_bottom",
            )
        )

    if spec.usb_overhang_left > 0.0 and spec.usb_width > 0.0:
        uh = spec.usb_width / 2.0
        parts.extend(
            _fp_rect_lines(
                -hw - spec.usb_overhang_left,
                -uh,
                -hw + 5.5,
                uh,
                "F.Fab",
                0.1,
                scope,
                "usb_fab",
            )
        )
        parts.extend(
            _fp_rect_lines(
                -hw - spec.usb_overhang_left,
                -uh,
                -hw - silk_margin,
                uh,
                "F.SilkS",
                0.12,
                scope,
                "usb_silk",
            )
        )
        parts.append(
            _fp_text("user", "USB-C", -hw + 2.5, 0.0, "F.SilkS", 0.8, 0.15, scope, "usb_label")
        )

    parts.extend(
        _fp_rect_lines(min_x, min_y, max_x, max_y, "F.CrtYd", 0.05, scope, "courtyard")
    )

    chamfer = min(1.0, hw * 0.25, hh * 0.25)
    parts.extend(
        [
            _fp_line(-hw, -hh + chamfer, -hw + chamfer, -hh, "F.Fab", 0.1, scope, "fab_chamfer"),
            _fp_line(-hw + chamfer, -hh, hw, -hh, "F.Fab", 0.1, scope, "fab_top"),
            _fp_line(hw, -hh, hw, hh, "F.Fab", 0.1, scope, "fab_right"),
            _fp_line(hw, hh, -hw, hh, "F.Fab", 0.1, scope, "fab_bottom"),
            _fp_line(-hw, hh, -hw, -hh + chamfer, "F.Fab", 0.1, scope, "fab_left"),
        ]
    )

    parts.append(_fp_text("user", "${REFERENCE}", 0.0, 0.0, "F.Fab", 1.0, 0.15, scope, "fab_ref"))
    parts.append(
        _fp_text(
            "user",
            spec.name,
            0.0,
            -hh + 1.4 if (spec.name == "Mini560" and len(spec.pins) == 5) else 0.0,
            "F.SilkS",
            0.9,
            0.15,
            scope,
            "silk_name",
        )
    )

    for pin in spec.pins:
        lx, ly = _pad_label_position(pin, spec)
        parts.append(
            _fp_text(
                "user",
                pin.name,
                lx,
                ly,
                "F.SilkS",
                0.75,
                0.15,
                scope,
                f"label_{pin.number}_{pin.name}",
            )
        )

    for pin in spec.pins:
        parts.append(_pad_sexpr(pin, spec, scope))

    parts.append("\t(embedded_fonts no)")
    parts.append(")")
    return "\n".join(parts) + "\n"


def generate_footprint_file(
    module_name: str,
    output_dir: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a KiCad 10 `.kicad_mod` file in `output_dir` and return metadata."""
    base_spec = resolve_module_spec(module_name)
    spec = apply_spec_overrides(base_spec, overrides)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{spec.name}.kicad_mod"
    content = build_footprint_sexpr(spec)
    file_path.write_text(content, encoding="utf-8")

    return {
        "status": "ok",
        "module": spec.name,
        "file_path": str(file_path.resolve()),
        "kicad_version": KICAD_MOD_VERSION,
        "pad_count": len(spec.pins),
        "board_size_mm": {"width": spec.board_width, "height": spec.board_height},
        "drill_mm": spec.drill,
        "pad_size_mm": spec.pad_size,
        "overrides_applied": dict(overrides) if overrides else {},
        "pins": [
            {"number": p.number, "name": p.name, "x": p.x, "y": p.y}
            for p in spec.pins
        ],
    }
