"""Focused regression tests for schematic pin and wire geometry."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from schematic_generator import create_schematic


def _balanced(text: str) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth < 0:
            return False
    return depth == 0 and not quoted


def test_wire_starts_at_embedded_pin_connection_point() -> None:
    with TemporaryDirectory() as directory:
        output = Path(directory) / "geometry.kicad_sch"
        create_schematic(
            str(output),
            "Geometry",
            [
                {
                    "reference": "U1",
                    "at": [80, 60],
                    "pins": [{"number": "1"}, {"number": "2"}],
                }
            ],
            [
                {"name": "VCC", "connections": [{"reference": "U1", "pad": "1"}]},
                {"name": "GND", "connections": [{"reference": "U1", "pad": "2"}]},
            ],
        )
        text = output.read_text(encoding="utf-8")
        assert _balanced(text)
        assert '(xy 67.3 60) (xy 72.38 60)' in text
        assert '(xy 92.7 60) (xy 97.78 60)' in text
        assert '(at 72.38 60 0)' in text
        assert '(at 97.78 60 0)' in text
