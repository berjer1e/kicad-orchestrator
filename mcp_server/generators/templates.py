"""Parametric physical and electrical module templates for KiCad 10 generation.

Defines specifications for common hobbyist/industrial DC-DC and Li-ion charger
breakout boards (`Mini360`, `Mini560`, `MT3608`, `TP4056_HW373`) including exact
dimensions, pad coordinates, drill diameters, electrical pin types, and
supported parametric overrides.
"""

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class PinSpec:
    """Specification for a single module pin/pad."""

    number: str
    name: str
    x: float
    y: float
    electrical_type: str = "passive"
    side: str = "left"
    shape: str = "circle"
    description: str = ""


@dataclass(frozen=True)
class ModuleSpec:
    """Complete physical, footprint, and schematic specification for a module."""

    name: str
    display_name: str
    category: str
    description: str
    tags: str
    board_width: float
    board_height: float
    drill: float
    pad_size: float
    pins: tuple[PinSpec, ...]
    pitch_x: float = 0.0
    pitch_y: float = 0.0
    attr: str = "through_hole"
    reference_prefix: str = "U"
    datasheet: str = ""
    voltage_in: str = ""
    voltage_out: str = ""
    max_current: str = ""
    usb_overhang_left: float = 0.0
    usb_width: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)


MODULE_SPECS: dict[str, ModuleSpec] = {
    "Mini360": ModuleSpec(
        name="Mini360",
        display_name="Mini-360 MP2307 DC-DC Buck Converter",
        category="DC-DC Buck",
        description=(
            "Mini-360 MP2307 synchronous step-down buck converter module, "
            "17.0x11.0 mm, 4 corner THT/castellated pads (IN+, IN-, OUT+, OUT-), "
            "4.75-23V in, 1.0-17V out, 1.8A continuous / 3A peak."
        ),
        tags="Mini360 Mini-360 MP2307 DC-DC buck step-down",
        board_width=17.0,
        board_height=11.0,
        pitch_x=14.0,
        pitch_y=8.0,
        drill=1.0,
        pad_size=2.0,
        voltage_in="4.75V - 23V",
        voltage_out="1.0V - 17V (adj)",
        max_current="1.8A cont / 3.0A peak",
        pins=(
            PinSpec("1", "IN+", -7.0, -4.0, "power_in", "left", "roundrect", "Positive input voltage"),
            PinSpec("2", "IN-", -7.0, 4.0, "power_in", "left", "circle", "Input ground"),
            PinSpec("3", "OUT-", 7.0, 4.0, "power_in", "right", "circle", "Output ground"),
            PinSpec("4", "OUT+", 7.0, -4.0, "power_out", "right", "circle", "Regulated positive output"),
        ),
        notes=(
            "Adjust output voltage via onboard trimmer potentiometer before soldering.",
            "Mount with pin headers or flush SMD-style over the 4 corner pads.",
        ),
    ),
    "Mini560": ModuleSpec(
        name="Mini560",
        display_name="Mini-560 JW5068A 5A DC-DC Buck Converter",
        category="DC-DC Buck",
        description=(
            "Mini-560 JW5068A high-efficiency 5A step-down buck converter module, "
            "29.0x18.0 mm, 5-pin high-current THT header (IN+, IN-, OUT-, OUT+, EN), "
            "pitch 5.08 mm along bottom edge, drill 1.1 mm, pad 2.4 mm."
        ),
        tags="Mini560 Mini-560 JW5068A DC-DC buck 5A",
        board_width=29.0,
        board_height=18.0,
        pitch_x=5.08,
        pitch_y=0.0,
        drill=1.1,
        pad_size=2.4,
        voltage_in="4.5V - 20V (fixed variants up to 24V)",
        voltage_out="3.3V / 5V / 9V / 12V",
        max_current="5.0A max",
        pins=(
            PinSpec("1", "IN+", -10.16, 6.5, "power_in", "left", "roundrect", "Positive input voltage"),
            PinSpec("2", "IN-", -5.08, 6.5, "power_in", "left", "circle", "Input ground"),
            PinSpec("3", "OUT-", 0.0, 6.5, "power_in", "right", "circle", "Output ground (GND/OUT-)"),
            PinSpec("4", "OUT+", 5.08, 6.5, "power_out", "right", "circle", "Regulated 5A positive output"),
            PinSpec("5", "EN", 10.16, 6.5, "input", "left", "circle", "Enable pin (active high)"),
        ),
        notes=(
            "Use heavy copper traces/polygons (>= 1.5 mm) on IN+, IN-, OUT-, OUT+ for 5A load.",
            "Override pitch/pin_positions if using compact 15x18 mm 4-corner Mini-560 variant.",
        ),
    ),
    "MT3608": ModuleSpec(
        name="MT3608",
        display_name="MT3608 2A DC-DC Boost Converter",
        category="DC-DC Boost",
        description=(
            "MT3608 2A step-up boost converter module, 36.0x17.0 mm, "
            "4 corner THT pads (IN+, IN-, OUT+, OUT-), 2-24V in, 5-28V out."
        ),
        tags="MT3608 DC-DC boost step-up converter",
        board_width=36.0,
        board_height=17.0,
        pitch_x=31.0,
        pitch_y=12.0,
        drill=1.0,
        pad_size=2.2,
        voltage_in="2.0V - 24V",
        voltage_out="5.0V - 28V (adj)",
        max_current="2.0A peak",
        pins=(
            PinSpec("1", "IN+", -15.5, -6.0, "power_in", "left", "roundrect", "Positive input voltage"),
            PinSpec("2", "IN-", -15.5, 6.0, "power_in", "left", "circle", "Input ground"),
            PinSpec("3", "OUT-", 15.5, 6.0, "power_in", "right", "circle", "Output ground"),
            PinSpec("4", "OUT+", 15.5, -6.0, "power_out", "right", "circle", "Boosted positive output"),
        ),
        notes=(
            "Input voltage must stay below desired boosted output voltage.",
            "Multi-turn trimmer potentiometer adjusts output voltage.",
        ),
    ),
    "TP4056_HW373": ModuleSpec(
        name="TP4056_HW373",
        display_name="HW-373 TP4056 Type-C 1A Li-Ion Charger + Protection",
        category="Battery Charger",
        description=(
            "HW-373 TP4056 1A Li-Ion charger module with DW01A protection and USB Type-C, "
            "28.0x17.0 mm, 6 THT pads (IN+, IN-, OUT+, B+, B-, OUT-) and USB-C overhang keepout."
        ),
        tags="TP4056 HW-373 HW373 Li-Ion charger Type-C DW01A",
        board_width=28.0,
        board_height=17.0,
        pitch_x=23.0,
        pitch_y=4.0,
        drill=1.0,
        pad_size=2.0,
        voltage_in="4.5V - 5.5V (USB-C or IN+/IN-)",
        voltage_out="4.2V battery / protected load OUT+/OUT-",
        max_current="1.0A charge",
        usb_overhang_left=1.5,
        usb_width=9.0,
        pins=(
            PinSpec("1", "IN+", -11.5, -6.0, "power_in", "left", "roundrect", "5V external charger input +"),
            PinSpec("2", "IN-", -11.5, 6.0, "power_in", "left", "circle", "5V external charger input -"),
            PinSpec("3", "OUT+", 11.5, -6.0, "power_out", "right", "circle", "Protected load output +"),
            PinSpec("4", "B+", 11.5, -2.0, "bidirectional", "right", "circle", "Li-Ion battery positive"),
            PinSpec("5", "B-", 11.5, 2.0, "power_in", "right", "circle", "Li-Ion battery negative"),
            PinSpec("6", "OUT-", 11.5, 6.0, "power_in", "right", "circle", "Protected load output -"),
        ),
        notes=(
            "Leave USB Type-C overhang keepout clear at left board edge.",
            "Do not connect B- directly to system GND; use OUT- for protected load return.",
        ),
    ),
}

_MODULE_ALIASES: dict[str, str] = {
    "mini360": "Mini360",
    "mini-360": "Mini360",
    "mp2307": "Mini360",
    "mini560": "Mini560",
    "mini-560": "Mini560",
    "jw5068a": "Mini560",
    "mt3608": "MT3608",
    "mt-3608": "MT3608",
    "tp4056": "TP4056_HW373",
    "hw373": "TP4056_HW373",
    "hw-373": "TP4056_HW373",
    "tp4056_hw373": "TP4056_HW373",
    "tp4056-hw373": "TP4056_HW373",
}


def resolve_module_spec(module_name: str) -> ModuleSpec:
    """Resolve a module name or alias to its canonical `ModuleSpec`."""
    if not module_name or not isinstance(module_name, str):
        raise ValueError("module_name must be a non-empty string.")
    key = module_name.strip().lower().replace(" ", "_")
    canonical = _MODULE_ALIASES.get(key)
    if canonical and canonical in MODULE_SPECS:
        return MODULE_SPECS[canonical]
    if module_name in MODULE_SPECS:
        return MODULE_SPECS[module_name]
    supported = ", ".join(sorted(MODULE_SPECS.keys()))
    raise ValueError(f"Unsupported module '{module_name}'. Supported modules: {supported}")


def apply_spec_overrides(spec: ModuleSpec, overrides: dict[str, Any] | None = None) -> ModuleSpec:
    """Return a modified `ModuleSpec` with validated parametric overrides applied."""
    if not overrides:
        return spec

    variant = str(overrides.get("variant", "")).strip().lower()
    if spec.name == "Mini560" and variant in ("smd_15x18", "smd_4pad", "smd"):
        spec = replace(
            spec,
            display_name="Mini-560 Compact 4-Pad SMD Buck Converter (15x18 mm)",
            description=(
                "Compact Mini-560 DC-DC step-down buck converter module, "
                "15.0x18.0 mm, 4 corner SMD pads (IN+, IN-, OUT-, OUT+)."
            ),
            board_width=15.0,
            board_height=18.0,
            pitch_x=11.0,
            pitch_y=14.0,
            drill=0.0,
            pad_size=2.6,
            attr="smd",
            pins=(
                PinSpec("1", "IN+", -5.5, -7.0, "power_in", "left", "roundrect", "Positive input voltage"),
                PinSpec("2", "IN-", -5.5, 7.0, "power_in", "left", "rect", "Input ground"),
                PinSpec("3", "OUT-", 5.5, 7.0, "power_in", "right", "rect", "Output ground (GND/OUT-)"),
                PinSpec("4", "OUT+", 5.5, -7.0, "power_out", "right", "rect", "Regulated positive output"),
            ),
        )

    board_width = float(overrides.get("board_width", spec.board_width))
    board_height = float(overrides.get("board_height", spec.board_height))
    drill = float(overrides.get("drill_dia", overrides.get("drill", spec.drill)))
    pad_size = float(overrides.get("pad_dia", overrides.get("pad_size", spec.pad_size)))
    attr = str(overrides.get("attr", spec.attr))

    if board_width <= 0 or board_height <= 0:
        raise ValueError("board_width and board_height must be positive.")
    if pad_size <= 0:
        raise ValueError("pad_size / pad_dia must be positive.")
    if attr == "through_hole" and drill <= 0:
        raise ValueError("drill / drill_dia must be positive for through_hole pads.")

    pitch_override = overrides.get("pitch")
    pitch_x = float(overrides.get("pitch_x", pitch_override if pitch_override is not None else spec.pitch_x))
    pitch_y = float(overrides.get("pitch_y", pitch_override if pitch_override is not None else spec.pitch_y))

    pins = _recompute_pin_positions(spec, pitch_x, pitch_y, overrides)

    return replace(
        spec,
        board_width=board_width,
        board_height=board_height,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        drill=drill,
        pad_size=pad_size,
        attr=attr,
        pins=pins,
    )


def _recompute_pin_positions(
    spec: ModuleSpec,
    pitch_x: float,
    pitch_y: float,
    overrides: dict[str, Any],
) -> tuple[PinSpec, ...]:
    """Recompute pin coordinates when pitch or explicit `pin_positions` overrides are supplied."""
    custom_positions = overrides.get("pin_positions")
    if isinstance(custom_positions, dict):
        updated: list[PinSpec] = []
        for pin in spec.pins:
            coords = custom_positions.get(pin.name, custom_positions.get(pin.number))
            if coords is not None:
                updated.append(replace(pin, x=float(coords[0]), y=float(coords[1])))
            else:
                updated.append(pin)
        return tuple(updated)

    if isinstance(custom_positions, (list, tuple)) and len(custom_positions) == len(spec.pins):
        return tuple(
            replace(pin, x=float(pos[0]), y=float(pos[1]))
            for pin, pos in zip(spec.pins, custom_positions)
        )

    if (spec.name in ("Mini360", "MT3608") or (spec.name == "Mini560" and len(spec.pins) == 4)) and (
        "pitch_x" in overrides or "pitch_y" in overrides or "pitch" in overrides
    ):
        hx = pitch_x / 2.0
        hy = pitch_y / 2.0
        corner_map = {
            "IN+": (-hx, -hy),
            "IN-": (-hx, hy),
            "OUT-": (hx, hy),
            "OUT+": (hx, -hy),
        }
        return tuple(
            replace(pin, x=corner_map[pin.name][0], y=corner_map[pin.name][1])
            for pin in spec.pins
        )

    if spec.name == "Mini560" and len(spec.pins) == 5 and ("pitch" in overrides or "pitch_x" in overrides):
        count = len(spec.pins)
        start_x = -((count - 1) * pitch_x) / 2.0
        y_pos = float(overrides.get("header_y", spec.pins[0].y))
        return tuple(
            replace(pin, x=round(start_x + idx * pitch_x, 4), y=y_pos)
            for idx, pin in enumerate(spec.pins)
        )

    if spec.name == "TP4056_HW373" and (
        "pitch_x" in overrides or "pitch_y" in overrides or "pitch" in overrides
    ):
        hx = pitch_x / 2.0
        right_y = [-1.5 * pitch_y, -0.5 * pitch_y, 0.5 * pitch_y, 1.5 * pitch_y]
        pos_map = {
            "IN+": (-hx, -1.5 * pitch_y),
            "IN-": (-hx, 1.5 * pitch_y),
            "OUT+": (hx, right_y[0]),
            "B+": (hx, right_y[1]),
            "B-": (hx, right_y[2]),
            "OUT-": (hx, right_y[3]),
        }
        return tuple(
            replace(pin, x=round(pos_map[pin.name][0], 4), y=round(pos_map[pin.name][1], 4))
            for pin in spec.pins
        )

    return spec.pins


def spec_to_summary_dict(spec: ModuleSpec) -> dict[str, Any]:
    """Convert a `ModuleSpec` into a JSON-serializable summary dictionary."""
    return {
        "name": spec.name,
        "display_name": spec.display_name,
        "category": spec.category,
        "description": spec.description,
        "board_size_mm": {"width": spec.board_width, "height": spec.board_height},
        "pitch_mm": {"x": spec.pitch_x, "y": spec.pitch_y},
        "pad": {"drill_mm": spec.drill, "size_mm": spec.pad_size, "attr": spec.attr},
        "electrical": {
            "voltage_in": spec.voltage_in,
            "voltage_out": spec.voltage_out,
            "max_current": spec.max_current,
        },
        "pins": [
            {
                "number": pin.number,
                "name": pin.name,
                "x": pin.x,
                "y": pin.y,
                "electrical_type": pin.electrical_type,
                "description": pin.description,
            }
            for pin in spec.pins
        ],
        "notes": list(spec.notes),
    }
