"""KiCad 10 S-expression symbol library (`.kicad_sym`) generator.

Generates parametric KiCad 10.0 schematic symbol libraries (`version 20251024`)
with 100-mil (`2.54 mm`) grid-aligned pins, proper electrical pin types,
bounding rectangle, Reference, Value, and Footprint metadata.
"""

from pathlib import Path
from typing import Any

from .templates import ModuleSpec, PinSpec, apply_spec_overrides, resolve_module_spec

KICAD_SYM_VERSION = "20251024"
GENERATOR_NAME = "kicad_symbol_editor"
GENERATOR_VERSION = "10.0"
GRID_MM = 2.54


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


def _sym_property(
    name: str,
    value: str,
    x: float,
    y: float,
    hide: bool = False,
) -> str:
    """Render a KiCad 10 symbol `(property ...)` block."""
    lines = [
        f'\t\t(property "{_esc(name)}" "{_esc(value)}"',
        f'\t\t\t(at {_fmt(x)} {_fmt(y)} 0)',
        "\t\t\t(show_name no)",
        "\t\t\t(do_not_autoplace no)",
    ]
    if hide:
        lines.append("\t\t\t(hide yes)")
    lines.extend(
        [
            "\t\t\t(effects",
            "\t\t\t\t(font",
            "\t\t\t\t\t(size 1.27 1.27)",
            "\t\t\t\t)",
            "\t\t\t)",
            "\t\t)",
        ]
    )
    return "\n".join(lines)


def _grid_y_positions(count: int) -> list[float]:
    """Return Y coordinates (top-to-bottom) snapped strictly to the 2.54 mm grid."""
    if count <= 0:
        return []
    if count == 1:
        return [0.0]
    if count % 2 == 1:
        half = count // 2
        return [round((half - i) * GRID_MM, 4) for i in range(count)]
    half = count // 2
    offsets = list(range(half, 0, -1)) + list(range(-1, -half - 1, -1))
    return [round(k * GRID_MM, 4) for k in offsets]


def _pin_sexpr(pin: PinSpec, x: float, y: float, angle: int, length: float = 2.54) -> str:
    """Render a KiCad 10 symbol `(pin ...)` definition."""
    return (
        f'\t\t\t(pin {pin.electrical_type} line\n'
        f'\t\t\t\t(at {_fmt(x)} {_fmt(y)} {angle})\n'
        f'\t\t\t\t(length {_fmt(length)})\n'
        f'\t\t\t\t(name "{_esc(pin.name)}"\n'
        f'\t\t\t\t\t(effects\n'
        f'\t\t\t\t\t\t(font\n'
        f'\t\t\t\t\t\t\t(size 1.27 1.27)\n'
        f'\t\t\t\t\t\t)\n'
        f'\t\t\t\t\t)\n'
        f'\t\t\t\t)\n'
        f'\t\t\t\t(number "{_esc(pin.number)}"\n'
        f'\t\t\t\t\t(effects\n'
        f'\t\t\t\t\t\t(font\n'
        f'\t\t\t\t\t\t\t(size 1.27 1.27)\n'
        f'\t\t\t\t\t\t)\n'
        f'\t\t\t\t\t)\n'
        f'\t\t\t\t)\n'
        f'\t\t\t)'
    )


def build_symbol_sexpr(
    spec: ModuleSpec,
    footprint_ref: str | None = None,
) -> str:
    """Build a complete KiCad 10 `.kicad_sym` library S-expression string."""
    fp_ref = footprint_ref if footprint_ref is not None else f"Custom_Modules:{spec.name}"

    left_pins = [p for p in spec.pins if p.side == "left"]
    right_pins = [p for p in spec.pins if p.side != "left"]

    left_ys = _grid_y_positions(len(left_pins))
    right_ys = _grid_y_positions(len(right_pins))

    all_ys = left_ys + right_ys or [0.0]
    max_y = max(abs(y) for y in all_ys)
    box_hh = round(max_y + GRID_MM, 4)
    box_hw = 7.62
    pin_len = 2.54
    pin_x_left = -(box_hw + pin_len)
    pin_x_right = box_hw + pin_len

    pin_blocks: list[str] = []
    for pin, py in zip(left_pins, left_ys):
        pin_blocks.append(_pin_sexpr(pin, pin_x_left, py, 0, pin_len))
    for pin, py in zip(right_pins, right_ys):
        pin_blocks.append(_pin_sexpr(pin, pin_x_right, py, 180, pin_len))

    lines: list[str] = [
        "(kicad_symbol_lib",
        f"\t(version {KICAD_SYM_VERSION})",
        f'\t(generator "{GENERATOR_NAME}")',
        f'\t(generator_version "{GENERATOR_VERSION}")',
        f'\t(symbol "{_esc(spec.name)}"',
        "\t\t(exclude_from_sim no)",
        "\t\t(in_bom yes)",
        "\t\t(on_board yes)",
        "\t\t(in_pos_files yes)",
        "\t\t(duplicate_pin_numbers_are_jumpers no)",
        _sym_property(spec_name := "Reference", spec.reference_prefix, -box_hw, box_hh + 1.27),
        _sym_property("Value", spec.name, 0.0, -(box_hh + 1.27)),
        _sym_property("Footprint", fp_ref, 0.0, 0.0, hide=True),
        _sym_property("Datasheet", spec.datasheet, 0.0, 0.0, hide=True),
        _sym_property("Description", spec.description, 0.0, 0.0, hide=True),
        _sym_property("ki_keywords", spec.tags, 0.0, 0.0, hide=True),
        _sym_property("ki_fp_filters", f"*{spec.name}*", 0.0, 0.0, hide=True),
        f'\t\t(symbol "{_esc(spec.name)}_0_1"',
        "\t\t\t(rectangle",
        f"\t\t\t\t(start {_fmt(-box_hw)} {_fmt(box_hh)})",
        f"\t\t\t\t(end {_fmt(box_hw)} {_fmt(-box_hh)})",
        "\t\t\t\t(stroke",
        "\t\t\t\t\t(width 0.254)",
        "\t\t\t\t\t(type default)",
        "\t\t\t\t)",
        "\t\t\t\t(fill",
        "\t\t\t\t\t(type background)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
        f'\t\t(symbol "{_esc(spec.name)}_1_1"',
        *pin_blocks,
        "\t\t)",
        "\t\t(embedded_fonts no)",
        "\t)",
        ")",
    ]
    _ = spec_name
    return "\n".join(lines) + "\n"


def generate_symbol_file(
    module_name: str,
    output_dir: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a KiCad 10 `.kicad_sym` library file in `output_dir` and return metadata."""
    base_spec = resolve_module_spec(module_name)
    spec = apply_spec_overrides(base_spec, overrides)

    footprint_ref = None
    if overrides and "footprint_ref" in overrides:
        footprint_ref = str(overrides["footprint_ref"])

    lib_name = str(overrides["library_name"]) if (overrides and "library_name" in overrides) else spec.name
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{lib_name}.kicad_sym"
    content = build_symbol_sexpr(spec, footprint_ref=footprint_ref)
    file_path.write_text(content, encoding="utf-8")

    return {
        "status": "ok",
        "module": spec.name,
        "symbol_name": spec.name,
        "file_path": str(file_path.resolve()),
        "kicad_version": KICAD_SYM_VERSION,
        "pin_count": len(spec.pins),
        "footprint_ref": footprint_ref or f"Custom_Modules:{spec.name}",
        "pins": [
            {
                "number": p.number,
                "name": p.name,
                "electrical_type": p.electrical_type,
                "side": p.side,
            }
            for p in spec.pins
        ],
    }
