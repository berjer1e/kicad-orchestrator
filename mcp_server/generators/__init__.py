"""KiCad 10 parametric footprint/symbol generators and BOM specification parser.

Public API exported for MCP tool integration:
- `list_supported_modules()`
- `generate_footprint(module_name, output_dir, overrides=None, **extra_overrides)`
- `generate_symbol(module_name, output_dir, overrides=None, **extra_overrides)`
- `parse_bom_spec(source)`
"""

from typing import Any

from .bom_parser import parse_bom_spec
from .footprint_gen import generate_footprint_file
from .symbol_gen import generate_symbol_file
from .templates import MODULE_SPECS, spec_to_summary_dict

__all__ = [
    "list_supported_modules",
    "generate_footprint",
    "generate_symbol",
    "parse_bom_spec",
]


def list_supported_modules() -> list[dict[str, Any]]:
    """Return metadata summaries for all supported parametric KiCad module templates."""
    return [spec_to_summary_dict(spec) for spec in MODULE_SPECS.values()]


def generate_footprint(
    module_name: str,
    output_dir: str,
    overrides: dict[str, Any] | None = None,
    **extra_overrides: Any,
) -> dict[str, Any]:
    """Generate a KiCad 10 `.kicad_mod` footprint file with optional parametric overrides."""
    merged: dict[str, Any] = {}
    if overrides:
        merged.update(overrides)
    if extra_overrides:
        merged.update(extra_overrides)
    return generate_footprint_file(module_name, output_dir, overrides=merged or None)


def generate_symbol(
    module_name: str,
    output_dir: str,
    overrides: dict[str, Any] | None = None,
    **extra_overrides: Any,
) -> dict[str, Any]:
    """Generate a KiCad 10 `.kicad_sym` symbol library file with optional overrides."""
    merged: dict[str, Any] = {}
    if overrides:
        merged.update(overrides)
    if extra_overrides:
        merged.update(extra_overrides)
    return generate_symbol_file(module_name, output_dir, overrides=merged or None)
