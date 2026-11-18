"""Safe S-expression PCB layout editor, trace/via mutator, and obstacle-aware A* autorouter for KiCad 10.

This module eliminates the need for ad-hoc `pcbnew` SWIG scripts (which can hang on C++ wxWidgets
assertions in KiCad 10) or fragile regex edits by providing atomic, net-ID-validated S-expression
editing and 2-layer grid autorouting on `.kicad_pcb` files.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

try:
    import pcb_inspector
except ImportError:
    from . import pcb_inspector


def _resolve_target_and_output(
    pcb_path: str,
    output_path: str | None = None,
    suffix: str = "_edited",
) -> tuple[Path, Path, dict[str, Any]]:
    """Resolve input `.kicad_pcb` path and safe output destination respecting KiCad `.lck` files.

    Args:
        pcb_path: Path to the source `.kicad_pcb` file.
        output_path: Optional explicit output `.kicad_pcb` path.
        suffix: Filename stem suffix used when saving alongside a locked board.

    Returns:
        Tuple of `(source_path, destination_path, lock_info_dict)`.
    """
    source = Path(pcb_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PCB file does not exist: {source}")
    lock_info = pcb_inspector.check_kicad_lock_files(str(source))
    if output_path is not None:
        dest = Path(output_path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        return source, dest, lock_info
    if lock_info.get("target_locked"):
        dest = source.with_name(f"{source.stem}{suffix}{source.suffix}")
        return source, dest, lock_info
    return source, source, lock_info


def _parse_net_map(text: str) -> tuple[dict[str, int], dict[int, str], bool]:
    """Extract net declarations from a `.kicad_pcb` file (supporting both integer-ID tables and KiCad 10 string nets).

    Args:
        text: Raw `.kicad_pcb` file text.

    Returns:
        Tuple of `(name_to_id, id_to_name, uses_integer_net_ids)`.
    """
    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    for match in re.finditer(r'^\s*\(net\s+(\d+)\s+"([^"]*)"\)', text, flags=re.MULTILINE):
        net_id = int(match.group(1))
        net_name = match.group(2)
        name_to_id[net_name] = net_id
        id_to_name[net_id] = net_name
    if name_to_id:
        return name_to_id, id_to_name, True

    all_names = sorted({m.group(1) for m in re.finditer(r'\(net\s+"([^"]+)"\)', text)})
    for idx, net_name in enumerate(all_names, start=1):
        name_to_id[net_name] = idx
        id_to_name[idx] = net_name
    return name_to_id, id_to_name, False


def _format_net_token(net_id: int, id_to_name: dict[int, str], uses_int_ids: bool) -> str:
    """Format a `(net ...)` token as `(net <id>)` or `(net "<name>")` matching the board's format.

    Args:
        net_id: Resolved integer net ID.
        id_to_name: Mapping from integer ID to net name.
        uses_int_ids: True if the board uses top-level integer `(net <id> "<name>")` declarations.

    Returns:
        Formatted `(net ...)` S-expression token.
    """
    if uses_int_ids:
        return f"(net {int(net_id)})"
    return f'(net "{id_to_name[net_id]}")'


def _resolve_net_id(net_spec: str | int, name_to_id: dict[str, int], id_to_name: dict[int, str]) -> int:
    """Resolve a net name or integer net ID into a validated integer net ID.

    Args:
        net_spec: Net name string (e.g. `"/SW17_D"` or `"GND"`) or integer net ID.
        name_to_id: Mapping from net name to integer ID.
        id_to_name: Mapping from integer ID to net name.

    Returns:
        Validated integer net ID.
    """
    if isinstance(net_spec, int):
        if net_spec in id_to_name:
            return net_spec
        raise ValueError(f"Unknown integer net ID {net_spec}.")
    raw = str(net_spec).strip()
    if raw in name_to_id:
        return name_to_id[raw]
    if raw.isdigit() and int(raw) in id_to_name:
        return int(raw)
    if not raw.startswith("/") and f"/{raw}" in name_to_id:
        return name_to_id[f"/{raw}"]
    if raw.startswith("/") and raw[1:] in name_to_id:
        return name_to_id[raw[1:]]
    raise ValueError(f"Unknown net '{net_spec}'. Available nets: {sorted(name_to_id)[:30]}")


def _extract_top_level_blocks(
    text: str,
    tags: set[str],
) -> list[tuple[int, int, str, str]]:
    """Scan `.kicad_pcb` text for top-level S-expression blocks matching `tags`.

    Args:
        text: Raw `.kicad_pcb` file text.
        tags: Set of S-expression head tokens (e.g. `{"segment", "via", "footprint", "zone"}`).

    Returns:
        List of tuples `(start_idx, end_idx, tag_name, block_text)`.
    """
    results: list[tuple[int, int, str, str]] = []
    depth = 0
    in_str = False
    escaped = False
    i = 0
    n = len(text)
    block_start = -1
    block_tag = ""
    while i < n:
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "(":
                if depth == 1:
                    m_tag = re.match(r"\(([a-zA-Z0-9_]+)", text[i : i + 32])
                    if m_tag and m_tag.group(1) in tags:
                        block_start = i
                        block_tag = m_tag.group(1)
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 1 and block_start != -1:
                    results.append((block_start, i + 1, block_tag, text[block_start : i + 1]))
                    block_start = -1
                    block_tag = ""
        i += 1
    return results


def _format_segment(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
    layer: str,
    net_token: str,
) -> str:
    """Format a KiCad 10 `(segment ...)` S-expression with the board-appropriate `(net ...)` token.

    Args:
        x1: Start X coordinate in mm.
        y1: Start Y coordinate in mm.
        x2: End X coordinate in mm.
        y2: End Y coordinate in mm.
        width: Trace width in mm.
        layer: Copper layer name (`"F.Cu"` or `"B.Cu"`).
        net_token: Formatted `(net ...)` token string.

    Returns:
        Formatted `(segment ...)` block string.
    """
    uid = str(uuid.uuid4())
    return (
        f"\t(segment\n"
        f"\t\t(start {round(x1, 4):g} {round(y1, 4):g})\n"
        f"\t\t(end {round(x2, 4):g} {round(y2, 4):g})\n"
        f"\t\t(width {round(width, 4):g})\n"
        f'\t\t(layer "{layer}")\n'
        f"\t\t{net_token}\n"
        f'\t\t(uuid "{uid}")\n'
        f"\t)"
    )


def _format_via(
    x: float,
    y: float,
    size: float,
    drill: float,
    layers: list[str],
    net_token: str,
) -> str:
    """Format a KiCad 10 `(via ...)` S-expression with the board-appropriate `(net ...)` token.

    Args:
        x: Via center X coordinate in mm.
        y: Via center Y coordinate in mm.
        size: Via pad diameter in mm.
        drill: Via drill diameter in mm.
        layers: Two-element layer list (e.g. `["F.Cu", "B.Cu"]`).
        net_token: Formatted `(net ...)` token string.

    Returns:
        Formatted `(via ...)` block string.
    """
    uid = str(uuid.uuid4())
    layer_str = " ".join(f'"{lyr}"' for lyr in layers)
    return (
        f"\t(via\n"
        f"\t\t(at {round(x, 4):g} {round(y, 4):g})\n"
        f"\t\t(size {round(size, 4):g})\n"
        f"\t\t(drill {round(drill, 4):g})\n"
        f"\t\t(layers {layer_str})\n"
        f"\t\t{net_token}\n"
        f'\t\t(uuid "{uid}")\n'
        f"\t)"
    )


def _format_edge_cuts(outline: dict[str, Any]) -> list[str]:
    """Generate `Edge.Cuts` graphic blocks for a rectangular or rounded-corner board outline.

    Args:
        outline: Dictionary with `x`, `y`, `width`, `height`, and optional `corner_radius_mm` and `stroke_width`.

    Returns:
        List of S-expression graphic item strings (`gr_rect`, `gr_line`, `gr_arc`).
    """
    x0 = float(outline.get("x", 0.0))
    y0 = float(outline.get("y", 0.0))
    w = float(outline["width"])
    h = float(outline["height"])
    r = float(outline.get("corner_radius_mm", 0.0))
    sw = float(outline.get("stroke_width", 0.1))
    x1 = x0 + w
    y1 = y0 + h

    if r <= 0.0:
        uid = str(uuid.uuid4())
        return [
            f"\t(gr_rect\n"
            f"\t\t(start {round(x0, 4):g} {round(y0, 4):g})\n"
            f"\t\t(end {round(x1, 4):g} {round(y1, 4):g})\n"
            f"\t\t(stroke (width {sw:g}) (type default))\n"
            f"\t\t(fill no)\n"
            f'\t\t(layer "Edge.Cuts")\n'
            f'\t\t(uuid "{uid}")\n'
            f"\t)"
        ]

    r = min(r, w / 2.0, h / 2.0)
    k = r * (1.0 - math.sqrt(0.5))
    lines = [
        (x0 + r, y0, x1 - r, y0),
        (x1, y0 + r, x1, y1 - r),
        (x1 - r, y1, x0 + r, y1),
        (x0, y1 - r, x0, y0 + r),
    ]
    arcs = [
        (x1 - r, y0, x1 - k, y0 + k, x1, y0 + r),
        (x1, y1 - r, x1 - k, y1 - k, x1 - r, y1),
        (x0 + r, y1, x0 + k, y1 - k, x0, y1 - r),
        (x0, y0 + r, x0 + k, y0 + k, x0 + r, y0),
    ]
    items: list[str] = []
    for sx, sy, ex, ey in lines:
        uid = str(uuid.uuid4())
        items.append(
            f"\t(gr_line\n"
            f"\t\t(start {round(sx, 4):g} {round(sy, 4):g})\n"
            f"\t\t(end {round(ex, 4):g} {round(ey, 4):g})\n"
            f"\t\t(stroke (width {sw:g}) (type default))\n"
            f'\t\t(layer "Edge.Cuts")\n'
            f'\t\t(uuid "{uid}")\n'
            f"\t)"
        )
    for sx, sy, mx, my, ex, ey in arcs:
        uid = str(uuid.uuid4())
        items.append(
            f"\t(gr_arc\n"
            f"\t\t(start {round(sx, 4):g} {round(sy, 4):g})\n"
            f"\t\t(mid {round(mx, 4):g} {round(my, 4):g})\n"
            f"\t\t(end {round(ex, 4):g} {round(ey, 4):g})\n"
            f"\t\t(stroke (width {sw:g}) (type default))\n"
            f'\t\t(layer "Edge.Cuts")\n'
            f'\t\t(uuid "{uid}")\n'
            f"\t)"
        )
    return items


def _format_zone(
    zone_spec: dict[str, Any],
    net_id: int,
    net_name: str,
    fallback_bbox: dict[str, float] | None = None,
) -> str:
    """Generate a KiCad 10 copper `(zone ...)` S-expression block.

    Supports `points`, `min_x/min_y/max_x/max_y`, `x/y/width/height`, or falls back to `fallback_bbox`.
    """
    uid = str(uuid.uuid4())
    layer = str(zone_spec.get("layer", "B.Cu"))
    clearance = float(zone_spec.get("clearance_mm", 0.3))
    min_thick = float(zone_spec.get("min_thickness_mm", 0.25))
    thermal_gap = float(zone_spec.get("thermal_gap_mm", 0.5))
    thermal_bridge = float(zone_spec.get("thermal_bridge_mm", 0.5))

    if "points" in zone_spec and zone_spec["points"]:
        pts = [(float(p[0]), float(p[1])) for p in zone_spec["points"]]
    elif all(k in zone_spec for k in ("min_x", "min_y", "max_x", "max_y")):
        x0, y0 = float(zone_spec["min_x"]), float(zone_spec["min_y"])
        x1, y1 = float(zone_spec["max_x"]), float(zone_spec["max_y"])
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    elif "width" in zone_spec and "height" in zone_spec:
        x0 = float(zone_spec.get("x", 0.0))
        y0 = float(zone_spec.get("y", 0.0))
        w = float(zone_spec["width"])
        h = float(zone_spec["height"])
        pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    elif fallback_bbox is not None:
        inset = float(zone_spec.get("inset_mm", 0.3))
        x0 = float(fallback_bbox["min_x"]) + inset
        y0 = float(fallback_bbox["min_y"]) + inset
        x1 = float(fallback_bbox["max_x"]) - inset
        y1 = float(fallback_bbox["max_y"]) - inset
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    else:
        raise ValueError("Zone spec requires 'width'+'height', 'min_x/min_y/max_x/max_y', or 'points'.")

    xy_str = " ".join(f"(xy {round(px, 4):g} {round(py, 4):g})" for px, py in pts)
    return (
        f"\t(zone\n"
        f"\t\t(net {int(net_id)})\n"
        f'\t\t(net_name "{net_name}")\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uid}")\n'
        f"\t\t(hatch edge 0.5)\n"
        f"\t\t(connect_pads (clearance {clearance:g}))\n"
        f"\t\t(min_thickness {min_thick:g})\n"
        f"\t\t(filled_areas_thickness no)\n"
        f"\t\t(fill yes (thermal_gap {thermal_gap:g}) (thermal_bridge_width {thermal_bridge:g}))\n"
        f"\t\t(polygon\n"
        f"\t\t\t(pts\n"
        f"\t\t\t\t{xy_str}\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        f"\t)"
    )


def _update_footprint_block(block: str, spec: dict[str, Any]) -> str:
    """Update a `(footprint ...)` block's position, rotation, layer, and property text settings.

    Args:
        block: Existing `(footprint ...)` S-expression string.
        spec: Placement dictionary with optional `x`, `y`, `rotation`, `ref_at`, and `hide_value`.

    Returns:
        Updated `(footprint ...)` S-expression string.
    """
    updated = block
    if "x" in spec and "y" in spec:
        x = float(spec["x"])
        y = float(spec["y"])
        rot = spec.get("rotation")
        at_str = (
            f"(at {round(x, 4):g} {round(y, 4):g} {round(float(rot), 4):g})"
            if rot is not None and float(rot) != 0.0
            else f"(at {round(x, 4):g} {round(y, 4):g})"
        )
        updated = re.sub(
            r"(\(footprint\s+\"[^\"]+\"(?:\s+\([a-zA-Z0-9_]+[^\)]*\))*?\s+)\(at\s+[-0-9.]+\s+[-0-9.]+(?:\s+[-0-9.]+)?\)",
            rf"\1{at_str}",
            updated,
            count=1,
        )
    if "ref_at" in spec and isinstance(spec["ref_at"], (list, tuple)) and len(spec["ref_at"]) >= 2:
        rx, ry = float(spec["ref_at"][0]), float(spec["ref_at"][1])
        rrot = float(spec["ref_at"][2]) if len(spec["ref_at"]) >= 3 else 0.0
        ref_at_str = (
            f"(at {round(rx, 4):g} {round(ry, 4):g} {round(rrot, 4):g})"
            if rrot != 0.0
            else f"(at {round(rx, 4):g} {round(ry, 4):g})"
        )
        updated = re.sub(
            r'(\(property\s+"Reference"\s+"[^"]+"\s+)\(at\s+[-0-9.]+\s+[-0-9.]+(?:\s+[-0-9.]+)?\)',
            rf"\1{ref_at_str}",
            updated,
            count=1,
        )
    return updated


def _insert_before_board_end(text: str, new_blocks: list[str]) -> str:
    """Insert formatted top-level S-expression blocks before `(zone ...)` or the closing `)` of a board.

    Args:
        text: Existing `.kicad_pcb` text.
        new_blocks: List of formatted top-level S-expression strings to insert.

    Returns:
        Updated `.kicad_pcb` text.
    """
    if not new_blocks:
        return text
    zone_idx = text.find("\t(zone\n")
    if zone_idx != -1:
        return text[:zone_idx] + "\n".join(new_blocks) + "\n" + text[zone_idx:]
    end_idx = text.rfind(")")
    if end_idx == -1:
        raise ValueError("Invalid .kicad_pcb file: missing closing parenthesis.")
    return text[:end_idx] + "\n".join(new_blocks) + "\n)\n"


def _replace_footprint_preserving_nets(
    old_block: str,
    spec: str | dict[str, Any],
) -> str:
    """Swap a footprint block with a `.kicad_mod` template while keeping Reference, Value, position, and pad nets."""
    fp_path_str = spec if isinstance(spec, str) else str(spec.get("fp_path", ""))
    fp_path = Path(fp_path_str).expanduser().resolve()
    if not fp_path.is_file():
        raise FileNotFoundError(f"Replacement footprint file not found: {fp_path}")

    raw = fp_path.read_text(encoding="utf-8")
    new_blocks = _extract_top_level_blocks(f"(library\n{raw}\n)\n", {"footprint"})
    if not new_blocks:
        raise ValueError(f"No (footprint ...) block found in {fp_path}")
    new_block = new_blocks[0][3]

    m_ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', old_block) or re.search(
        r'\(fp_text\s+reference\s+"([^"]+)"', old_block
    )
    m_val = re.search(r'\(property\s+"Value"\s+"([^"]+)"', old_block) or re.search(
        r'\(fp_text\s+value\s+"([^"]+)"', old_block
    )
    header_end = re.search(r"\((?:property|fp_text|fp_line|fp_rect|fp_circle|pad|model)\b", old_block)
    old_hdr = old_block[: header_end.start()] if header_end else old_block
    m_at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", old_hdr)

    ref = m_ref.group(1) if m_ref else "REF?"
    val = m_val.group(1) if m_val else ""
    old_x = float(m_at.group(1)) if m_at else 0.0
    old_y = float(m_at.group(2)) if m_at else 0.0
    old_rot = float(m_at.group(3)) if (m_at and m_at.group(3)) else 0.0

    if isinstance(spec, dict):
        x = float(spec.get("x", old_x))
        y = float(spec.get("y", old_y))
        rot = float(spec.get("rotation", old_rot))
    else:
        x, y, rot = old_x, old_y, old_rot

    old_pad_nets: dict[str, str] = {}
    for _ps, _pe, _pt, pblk in _extract_top_level_blocks(old_block, {"pad"}):
        pm = re.match(r'\(pad\s+"([^"]+)"', pblk)
        nm = re.search(r'\(net\s+(?:\d+\s+"([^"]*)"|"([^"]+)"|(\d+))\)', pblk)
        if pm and nm:
            net_tok = nm.group(1) or nm.group(2) or nm.group(3)
            if net_tok:
                old_pad_nets[pm.group(1)] = net_tok

    at_str = f"\t\t(at {x:g} {y:g}" + (f" {rot:g}" if rot else "") + ")"
    new_hdr_end = re.search(r"\((?:property|fp_text|fp_line|fp_rect|fp_circle|pad|model)\b", new_block)
    split_pos = new_hdr_end.start() if new_hdr_end else len(new_block)
    new_hdr, new_body = new_block[:split_pos], new_block[split_pos:]
    if re.search(r"\(at\s+[^\)]+\)", new_hdr):
        new_hdr = re.sub(r"[ \t]*\(at\s+[^\)]+\)", at_str, new_hdr, count=1)
    else:
        m_layer = re.search(r'(\(layer\s+"[^"]+"\))', new_hdr)
        if m_layer:
            new_hdr = new_hdr[: m_layer.end()] + "\n" + at_str + new_hdr[m_layer.end() :]
        else:
            new_hdr = new_hdr.replace("\n", f"\n{at_str}\n", 1)
    new_block = new_hdr + new_body

    new_block = re.sub(
        r'(\(property\s+"Reference"\s+)"[^"]*"',
        rf'\1"{ref}"',
        new_block,
        count=1,
    )
    new_block = re.sub(
        r'(\(fp_text\s+reference\s+)"[^"]*"',
        rf'\1"{ref}"',
        new_block,
        count=1,
    )
    if val:
        new_block = re.sub(
            r'(\(property\s+"Value"\s+)"[^"]*"',
            rf'\1"{val}"',
            new_block,
            count=1,
        )
        new_block = re.sub(
            r'(\(fp_text\s+value\s+)"[^"]*"',
            rf'\1"{val}"',
            new_block,
            count=1,
        )

    pad_spans = _extract_top_level_blocks(new_block, {"pad"})
    for pstart, pend, _ptag, pblk in reversed(pad_spans):
        pm = re.match(r'\(pad\s+"([^"]+)"', pblk)
        if not pm:
            continue
        pnum = pm.group(1)
        net_val = old_pad_nets.get(pnum)
        updated_pad = re.sub(r'\n?[ \t]*\(net\s+(?:\d+\s+"[^"]*"|"[^"]*"|\d+)\)', "", pblk)
        if net_val:
            net_line = f'\t\t\t(net "{net_val}")'
            close_p = updated_pad.rfind(")")
            updated_pad = updated_pad[:close_p].rstrip() + "\n" + net_line + "\n\t\t" + updated_pad[close_p:]
        if not re.search(r'\(uuid\s+"[^"]+"\)', updated_pad):
            close_p = updated_pad.rfind(")")
            updated_pad = updated_pad[:close_p].rstrip() + f'\n\t\t\t(uuid "{uuid.uuid4()}")\n\t\t' + updated_pad[close_p:]
        new_block = new_block[:pstart] + updated_pad + new_block[pend:]

    return "\t" + new_block.strip()


def _clean_footprint_silkscreen(
    block: str,
    hide_value: bool = False,
) -> tuple[str, int]:
    """Reposition overlapping Reference/Value silkscreen labels clear of pads and normalize font sizes."""
    m_fp_at = re.search(r"\n\s*\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", block)
    fp_rot = float(m_fp_at.group(3)) if (m_fp_at and m_fp_at.group(3)) else 0.0

    pad_rects: list[tuple[float, float, float, float]] = []
    for _ps, _pe, _pt, pblk in _extract_top_level_blocks(block, {"pad"}):
        pat = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", pblk)
        psz = re.search(r"\(size\s+([-\d.]+)\s+([-\d.]+)\)", pblk)
        if pat:
            px, py = float(pat.group(1)), float(pat.group(2))
            sx = float(psz.group(1)) * 0.5 if psz else 0.8
            sy = float(psz.group(2)) * 0.5 if psz else 0.8
            pad_rects.append((px - sx - 0.8, py - sy - 0.8, px + sx + 0.8, py + sy + 0.8))

    if not pad_rects:
        return block, 0

    min_py = min(r[1] for r in pad_rects)
    max_py = max(r[3] for r in pad_rects)
    mid_px = (min(r[0] for r in pad_rects) + max(r[2] for r in pad_rects)) * 0.5

    def _hits_pad(lx: float, ly: float) -> bool:
        """Check whether local silkscreen text center lies inside any expanded pad box."""
        return any(x0 <= lx <= x1 and y0 <= ly <= y1 for x0, y0, x1, y1 in pad_rects)

    adjusted = 0
    prop_spans = _extract_top_level_blocks(block, {"property"})
    for start_idx, end_idx, _tag, pblk in reversed(prop_spans):
        pm = re.match(r'\(property\s+"(Reference|Value)"', pblk)
        if not pm:
            continue
        pname = pm.group(1)
        new_pblk = pblk
        if pname == "Value" and hide_value and "(hide yes)" not in new_pblk:
            close_i = new_pblk.rfind(")")
            new_pblk = new_pblk[:close_i] + " (hide yes)" + new_pblk[close_i:]
            adjusted += 1

        m_pat = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", new_pblk)
        if m_pat:
            lx, ly = float(m_pat.group(1)), float(m_pat.group(2))
            if _hits_pad(lx, ly):
                cand_y = round(min_py - 1.2, 2) if pname == "Reference" else round(max_py + 1.2, 2)
                if _hits_pad(mid_px, cand_y):
                    cand_y = round(max_py + 2.4, 2)
                rot_str = f" {fp_rot:g}" if fp_rot else ""
                new_pblk = re.sub(
                    r"\(at\s+[^\)]+\)",
                    f"(at {mid_px:g} {cand_y:g}{rot_str})",
                    new_pblk,
                    count=1,
                )
                adjusted += 1

        new_pblk = re.sub(
            r"\(size\s+([-\d.]+)\s+([-\d.]+)\)",
            lambda m: f"(size {max(1.0, float(m.group(1))):g} {max(1.0, float(m.group(2))):g})",
            new_pblk,
            count=1,
        )
        new_pblk = re.sub(
            r"\(thickness\s+([-\d.]+)\)",
            lambda m: f"(thickness {max(0.15, float(m.group(1))):g})",
            new_pblk,
            count=1,
        )
        if new_pblk != pblk:
            block = block[:start_idx] + new_pblk + block[end_idx:]

    return block, adjusted


def _format_graphic_item(spec: dict[str, Any]) -> str:
    """Format a `(gr_text ...)`, `(gr_rect ...)`, or `(gr_line ...)` silkscreen/fab graphic S-expression."""
    kind = str(spec.get("type", "text")).strip().lower()
    layer = str(spec.get("layer", "F.SilkS"))
    uid = str(uuid.uuid4())
    if kind in ("text", "gr_text"):
        txt = str(spec.get("text", "")).replace('"', '\\"')
        x = float(spec.get("x", 0.0))
        y = float(spec.get("y", 0.0))
        rot = float(spec.get("rotation", 0.0))
        size = float(spec.get("size", 1.0))
        thick = float(spec.get("thickness", 0.15))
        rot_str = f" {rot:g}" if rot else ""
        return (
            f'\t(gr_text "{txt}"\n'
            f"\t\t(at {x:g} {y:g}{rot_str})\n"
            f'\t\t(layer "{layer}")\n'
            f'\t\t(uuid "{uid}")\n'
            f"\t\t(effects (font (size {size:g} {size:g}) (thickness {thick:g})))\n"
            f"\t)"
        )
    if kind in ("rect", "gr_rect"):
        x1 = float(spec.get("x1", spec.get("x", 0.0)))
        y1 = float(spec.get("y1", spec.get("y", 0.0)))
        x2 = float(spec.get("x2", x1 + float(spec.get("width", 10.0))))
        y2 = float(spec.get("y2", y1 + float(spec.get("height", 10.0))))
        width = float(spec.get("width_mm", spec.get("stroke_mm", 0.15)))
        return (
            f"\t(gr_rect\n"
            f"\t\t(start {x1:g} {y1:g})\n"
            f"\t\t(end {x2:g} {y2:g})\n"
            f"\t\t(stroke (width {width:g}) (type default))\n"
            f"\t\t(fill none)\n"
            f'\t\t(layer "{layer}")\n'
            f'\t\t(uuid "{uid}")\n'
            f"\t)"
        )
    if kind in ("line", "gr_line"):
        x1 = float(spec.get("x1", 0.0))
        y1 = float(spec.get("y1", 0.0))
        x2 = float(spec.get("x2", 0.0))
        y2 = float(spec.get("y2", 0.0))
        width = float(spec.get("width_mm", spec.get("stroke_mm", 0.15)))
        return (
            f"\t(gr_line\n"
            f"\t\t(start {x1:g} {y1:g})\n"
            f"\t\t(end {x2:g} {y2:g})\n"
            f"\t\t(stroke (width {width:g}) (type default))\n"
            f'\t\t(layer "{layer}")\n'
            f'\t\t(uuid "{uid}")\n'
            f"\t)"
        )
    raise ValueError(f"Unsupported graphic type: {kind!r}. Use 'text', 'rect', or 'line'.")


def edit_pcb_layout(
    pcb_path: str,
    board_outline: dict[str, Any] | None = None,
    footprint_placements: dict[str, dict[str, Any]] | None = None,
    zones: list[dict[str, Any]] | None = None,
    output_path: str | None = None,
    replace_footprints: dict[str, str | dict[str, Any]] | None = None,
    auto_fit_outline: dict[str, Any] | float | None = None,
    clean_silkscreen: bool | dict[str, Any] = False,
    graphics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Safely update board outline (`Edge.Cuts`), footprint placements/replacements, silkscreen, and copper zones."""
    source, dest, lock_info = _resolve_target_and_output(pcb_path, output_path, suffix="_layout")
    text = source.read_text(encoding="utf-8")
    name_to_id, id_to_name, uses_int_ids = _parse_net_map(text)

    placements = footprint_placements or {}
    replacements = replace_footprints or {}
    fit_cfg: dict[str, Any] | None = None
    if auto_fit_outline is not None:
        fit_cfg = (
            {"margin_mm": float(auto_fit_outline)}
            if isinstance(auto_fit_outline, (int, float))
            else dict(auto_fit_outline)
        )
    silk_cfg = clean_silkscreen if isinstance(clean_silkscreen, dict) else {}
    do_clean_silk = bool(clean_silkscreen)

    clear_edges = bool(
        (board_outline and board_outline.get("clear_existing", True))
        or (fit_cfg and fit_cfg.get("clear_existing", True))
    )
    replace_zone_keys: set[tuple[str, str]] = set()
    if zones:
        for zspec in zones:
            if zspec.get("replace_existing", True):
                znet = str(zspec.get("net", "GND"))
                if znet in name_to_id:
                    zid = _resolve_net_id(znet, name_to_id, id_to_name)
                    zlayer = str(zspec.get("layer", "B.Cu"))
                    replace_zone_keys.add((id_to_name[zid], zlayer))
                elif not uses_int_ids:
                    zlayer = str(zspec.get("layer", "B.Cu"))
                    replace_zone_keys.add((znet, zlayer))

    tags_to_scan = {"footprint", "gr_rect", "gr_line", "gr_arc", "gr_poly", "zone"}
    blocks = _extract_top_level_blocks(text, tags_to_scan)

    kept_parts: list[str] = []
    last_pos = 0
    moved_refs: list[str] = []
    replaced_refs: list[str] = []
    cleaned_silk_count = 0
    removed_edge_items = 0
    removed_zones = 0

    for start_idx, end_idx, tag, blk in blocks:
        if tag == "footprint" and (placements or replacements or do_clean_silk):
            m_ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', blk)
            ref = m_ref.group(1) if m_ref else None
            if ref and (ref in replacements or ref in placements or do_clean_silk):
                new_blk = blk
                if ref in replacements:
                    new_blk = _replace_footprint_preserving_nets(new_blk, replacements[ref])
                    replaced_refs.append(ref)
                if ref in placements:
                    new_blk = _update_footprint_block(new_blk, placements[ref])
                    moved_refs.append(ref)
                if do_clean_silk:
                    new_blk, adj = _clean_footprint_silkscreen(
                        new_blk, hide_value=bool(silk_cfg.get("hide_value", False))
                    )
                    cleaned_silk_count += adj
                kept_parts.append(text[last_pos:start_idx])
                kept_parts.append(new_blk)
                last_pos = end_idx
                continue
        elif tag in {"gr_rect", "gr_line", "gr_arc", "gr_poly"} and clear_edges:
            if '(layer "Edge.Cuts")' in blk:
                kept_parts.append(text[last_pos:start_idx].rstrip("\t"))
                if kept_parts[-1].endswith("\n"):
                    kept_parts[-1] = kept_parts[-1][:-1]
                last_pos = end_idx
                removed_edge_items += 1
                continue
        elif tag == "zone" and replace_zone_keys:
            m_zn_str = re.search(r'\(net_name\s+"([^"]+)"\)|\(net\s+"([^"]+)"\)', blk)
            m_zn_int = re.search(r"\(net\s+(\d+)\)", blk)
            z_net_name = (
                (m_zn_str.group(1) or m_zn_str.group(2))
                if m_zn_str
                else (id_to_name.get(int(m_zn_int.group(1)), "") if m_zn_int else "")
            )
            m_zl = re.search(r'\(layer\s+"([^"]+)"\)', blk)
            if z_net_name and m_zl and (z_net_name, m_zl.group(1)) in replace_zone_keys:
                kept_parts.append(text[last_pos:start_idx].rstrip("\t"))
                if kept_parts[-1].endswith("\n"):
                    kept_parts[-1] = kept_parts[-1][:-1]
                last_pos = end_idx
                removed_zones += 1
                continue

    kept_parts.append(text[last_pos:])
    updated_text = "".join(kept_parts)

    effective_outline = dict(board_outline) if board_outline else None
    if fit_cfg is not None:
        margin = float(fit_cfg.get("margin_mm", 3.0))
        tree = pcb_inspector._parse_sexpr(updated_text)
        net_map_tmp = pcb_inspector._extract_net_map(tree)
        fps, _ = pcb_inspector._parse_footprints_and_pads(tree, net_map_tmp)
        if fps:
            bx0 = min(fp["bbox"]["min_x"] for fp in fps) - margin
            by0 = min(fp["bbox"]["min_y"] for fp in fps) - margin
            bx1 = max(fp["bbox"]["max_x"] for fp in fps) + margin
            by1 = max(fp["bbox"]["max_y"] for fp in fps) + margin
            effective_outline = {
                "x": round(bx0, 3),
                "y": round(by0, 3),
                "width": round(bx1 - bx0, 3),
                "height": round(by1 - by0, 3),
                "corner_radius_mm": float(fit_cfg.get("corner_radius_mm", 0.0)),
                "clear_existing": bool(fit_cfg.get("clear_existing", True)),
            }

    appended_blocks: list[str] = []
    if effective_outline:
        appended_blocks.extend(_format_edge_cuts(effective_outline))

    added_zones = 0
    if zones:
        fallback_bbox = None
        if effective_outline:
            fallback_bbox = {
                "min_x": float(effective_outline["x"]),
                "min_y": float(effective_outline["y"]),
                "max_x": float(effective_outline["x"]) + float(effective_outline["width"]),
                "max_y": float(effective_outline["y"]) + float(effective_outline["height"]),
            }
        else:
            fallback_bbox = pcb_inspector._extract_board_bbox(pcb_inspector._parse_sexpr(updated_text))
        for zspec in zones:
            znet = str(zspec.get("net", "GND"))
            if znet not in name_to_id and not uses_int_ids:
                new_id = max(id_to_name, default=0) + 1
                name_to_id[znet] = new_id
                id_to_name[new_id] = znet
            zid = _resolve_net_id(znet, name_to_id, id_to_name)
            zname = id_to_name[zid]
            appended_blocks.append(_format_zone(zspec, zid, zname, fallback_bbox=fallback_bbox))
            added_zones += 1

    added_graphics = 0
    if graphics:
        for gspec in graphics:
            appended_blocks.append(_format_graphic_item(gspec))
            added_graphics += 1

    final_text = _insert_before_board_end(updated_text, appended_blocks)
    dest.write_text(final_text, encoding="utf-8")

    return {
        "status": "ok" if dest == source else "ok_staged",
        "source_pcb": str(source),
        "saved_path": str(dest),
        "modified_in_place": dest == source,
        "moved_footprints": sorted(moved_refs),
        "replaced_footprints": sorted(replaced_refs),
        "cleaned_silkscreen_items": cleaned_silk_count,
        "fitted_outline": effective_outline,
        "missing_footprints": sorted(set(placements) - set(moved_refs)),
        "removed_edge_items": removed_edge_items,
        "added_edge_items": len(_format_edge_cuts(effective_outline)) if effective_outline else 0,
        "removed_zones": removed_zones,
        "added_zones": added_zones,
        "added_graphics": added_graphics,
        "lock_files": lock_info.get("lock_files", []),
    }


def add_footprints_to_pcb(
    pcb_path: str,
    footprints: list[dict[str, Any]],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Insert library `.kicad_mod` footprints into a board at explicit coordinates.

    Each item requires `fp_path`, `reference`, `x`, and `y`; `value` and
    `rotation` are optional.  The source board is never changed when
    `output_path` is supplied.
    """
    source, dest, lock_info = _resolve_target_and_output(pcb_path, output_path, suffix="_footprints")
    text = source.read_text(encoding="utf-8")
    existing_refs = set(re.findall(r'\(property\s+"Reference"\s+"([^"]+)"', text))
    blocks: list[str] = []
    added: list[str] = []
    errors: list[str] = []
    for item in footprints:
        fp_path = Path(str(item.get("fp_path", ""))).expanduser().resolve()
        ref = str(item.get("reference", "")).strip()
        if not fp_path.is_file() or not ref or "x" not in item or "y" not in item:
            errors.append(f"Invalid footprint item: {item}")
            continue
        if ref in existing_refs or ref in added:
            errors.append(f"Duplicate reference: {ref}")
            continue
        raw = fp_path.read_text(encoding="utf-8")
        fp_blocks = _extract_top_level_blocks(f"(library\n{raw}\n)\n", {"footprint"})
        if not fp_blocks:
            errors.append(f"No footprint block found: {fp_path}")
            continue
        block = fp_blocks[0][3]
        rotation = float(item.get("rotation", 0.0))
        at = f"\n\t(at {float(item['x']):g} {float(item['y']):g}"
        if rotation:
            at += f" {rotation:g}"
        at += ")"
        block = re.sub(r'(\n\s+\(layer\s+"[^"]+"\))', rf"\1{at}", block, count=1)
        block = re.sub(r'(\(property\s+"Reference"\s+")([^"]+)(")', rf'\g<1>{ref}\g<3>', block, count=1)
        if item.get("value") is not None:
            block = re.sub(r'(\(property\s+"Value"\s+")([^"]+)(")', rf'\g<1>{str(item["value"])}\g<3>', block, count=1)
        block = re.sub(r'\(uuid\s+"[^"]+"\)', lambda _: f'(uuid "{uuid.uuid4()}")', block)
        blocks.append(block)
        added.append(ref)
    if errors:
        return {"status": "error", "errors": errors, "added_footprints": added}
    dest.write_text(_insert_before_board_end(text, blocks), encoding="utf-8")
    return {"status": "ok" if dest == source else "ok_staged", "source_pcb": str(source), "saved_path": str(dest), "added_footprints": added, "lock_files": lock_info.get("lock_files", [])}


def _item_matches_removal(
    block: str,
    clear_net_ids: set[int],
    clear_net_names: set[str],
    clear_bbox: dict[str, Any] | None,
) -> bool:
    """Check whether a `(segment ...)` or `(via ...)` block matches the removal criteria.

    Args:
        block: Top-level `(segment ...)` or `(via ...)` S-expression string.
        clear_net_ids: Set of integer net IDs scheduled for removal.
        clear_net_names: Set of string net names scheduled for removal.
        clear_bbox: Optional bounding box filter `{"min_x", "min_y", "max_x", "max_y", "layers", "nets"}`.

    Returns:
        True if the block should be removed, False otherwise.
    """
    m_net_int = re.search(r"\(net\s+(\d+)\)", block)
    if m_net_int and int(m_net_int.group(1)) in clear_net_ids:
        return True
    m_net_str = re.search(r'\(net\s+"([^"]+)"\)', block)
    if m_net_str and m_net_str.group(1) in clear_net_names:
        return True
    if not clear_bbox:
        return False

    bbox_layers = set(clear_bbox.get("layers", []))
    if bbox_layers:
        m_lyr = re.search(r'\(layer\s+"([^"]+)"\)', block)
        if m_lyr and m_lyr.group(1) not in bbox_layers:
            return False

    min_x = float(clear_bbox.get("min_x", -1e9))
    min_y = float(clear_bbox.get("min_y", -1e9))
    max_x = float(clear_bbox.get("max_x", 1e9))
    max_y = float(clear_bbox.get("max_y", 1e9))

    if block.startswith("(segment"):
        m_s = re.search(r"\(start\s+([-\d.]+)\s+([-\d.]+)\)", block)
        m_e = re.search(r"\(end\s+([-\d.]+)\s+([-\d.]+)\)", block)
        if m_s and m_e:
            sx, sy = float(m_s.group(1)), float(m_s.group(2))
            ex, ey = float(m_e.group(1)), float(m_e.group(2))
            if (min_x <= sx <= max_x and min_y <= sy <= max_y) or (
                min_x <= ex <= max_x and min_y <= ey <= max_y
            ):
                return True
    elif block.startswith("(via"):
        m_at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\)", block)
        if m_at:
            vx, vy = float(m_at.group(1)), float(m_at.group(2))
            if min_x <= vx <= max_x and min_y <= vy <= max_y:
                return True
    return False


def edit_pcb_traces(
    pcb_path: str,
    add_segments: list[dict[str, Any]] | None = None,
    add_vias: list[dict[str, Any]] | None = None,
    clear_nets: list[str | int] | None = None,
    clear_bbox: dict[str, Any] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Safely add or remove copper `(segment ...)` and `(via ...)` items with validated integer net IDs.

    Args:
        pcb_path: Path to the source `.kicad_pcb` file.
        add_segments: Optional list of segment dicts `[{"start": [x1, y1], "end": [x2, y2], "width": 0.3, "layer": "F.Cu", "net": "GND"}]`.
        add_vias: Optional list of via dicts `[{"at": [x, y], "size": 0.6, "drill": 0.3, "layers": ["F.Cu", "B.Cu"], "net": "GND"}]`.
        clear_nets: Optional list of net names or integer IDs whose existing segments and vias should be removed first.
        clear_bbox: Optional bounding-box filter `{"min_x": float, "min_y": float, "max_x": float, "max_y": float, "layers": ["F.Cu"]}` for removing items.
        output_path: Optional output `.kicad_pcb` path (stages to `<stem>_traces.kicad_pcb` if locked and `output_path` is None).

    Returns:
        Dictionary summarizing removed items, added segments/vias, `saved_path`, and lock status.
    """
    source, dest, lock_info = _resolve_target_and_output(pcb_path, output_path, suffix="_traces")
    text = source.read_text(encoding="utf-8")
    name_to_id, id_to_name, uses_int_ids = _parse_net_map(text)

    clear_net_ids: set[int] = set()
    clear_net_names: set[str] = set()
    for nspec in clear_nets or []:
        nid = _resolve_net_id(nspec, name_to_id, id_to_name)
        clear_net_ids.add(nid)
        clear_net_names.add(id_to_name[nid])

    removed_segments = 0
    removed_vias = 0
    if clear_net_ids or clear_bbox:
        blocks = _extract_top_level_blocks(text, {"segment", "via"})
        kept_parts: list[str] = []
        last_pos = 0
        for start_idx, end_idx, tag, blk in blocks:
            if _item_matches_removal(blk, clear_net_ids, clear_net_names, clear_bbox):
                kept_parts.append(text[last_pos:start_idx].rstrip("\t"))
                if kept_parts[-1].endswith("\n"):
                    kept_parts[-1] = kept_parts[-1][:-1]
                last_pos = end_idx
                if tag == "segment":
                    removed_segments += 1
                else:
                    removed_vias += 1
        kept_parts.append(text[last_pos:])
        text = "".join(kept_parts)

    new_blocks: list[str] = []
    added_seg_count = 0
    for seg in add_segments or []:
        sx, sy = float(seg["start"][0]), float(seg["start"][1])
        ex, ey = float(seg["end"][0]), float(seg["end"][1])
        if math.hypot(ex - sx, ey - sy) < 1e-4:
            continue
        width = float(seg.get("width", 0.3))
        layer = str(seg.get("layer", "F.Cu"))
        nid = _resolve_net_id(seg["net"], name_to_id, id_to_name)
        ntok = _format_net_token(nid, id_to_name, uses_int_ids)
        new_blocks.append(_format_segment(sx, sy, ex, ey, width, layer, ntok))
        added_seg_count += 1

    added_via_count = 0
    for via in add_vias or []:
        vx, vy = float(via["at"][0]), float(via["at"][1])
        size = float(via.get("size", 0.6))
        drill = float(via.get("drill", 0.3))
        layers = [str(x) for x in via.get("layers", ["F.Cu", "B.Cu"])]
        nid = _resolve_net_id(via["net"], name_to_id, id_to_name)
        ntok = _format_net_token(nid, id_to_name, uses_int_ids)
        new_blocks.append(_format_via(vx, vy, size, drill, layers, ntok))
        added_via_count += 1

    final_text = _insert_before_board_end(text, new_blocks)
    dest.write_text(final_text, encoding="utf-8")

    return {
        "status": "ok" if dest == source else "ok_staged",
        "source_pcb": str(source),
        "saved_path": str(dest),
        "modified_in_place": dest == source,
        "removed_segments": removed_segments,
        "removed_vias": removed_vias,
        "added_segments": added_seg_count,
        "added_vias": added_via_count,
        "lock_files": lock_info.get("lock_files", []),
    }


def _point_to_seg_dist(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    """Compute Euclidean distance from point `(px, py)` to line segment `(ax, ay)-(bx, by)`.

    Args:
        px: Point X coordinate.
        py: Point Y coordinate.
        ax: Segment start X coordinate.
        ay: Segment start Y coordinate.
        bx: Segment end X coordinate.
        by: Segment end Y coordinate.

    Returns:
        Shortest Euclidean distance in mm.
    """
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _simplify_grid_path(
    path: list[tuple[int, int, int]],
    gx_to_x: Any,
    gy_to_y: Any,
    net_name: str,
    width: float,
    via_size: float,
    via_drill: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert a 3D grid path `[(layer_idx, gx, gy), ...]` into simplified segment and via dicts.

    Args:
        path: Sequence of `(layer_idx, gx, gy)` grid states (`0` for `F.Cu`, `1` for `B.Cu`).
        gx_to_x: Callable converting grid X index to board X in mm.
        gy_to_y: Callable converting grid Y index to board Y in mm.
        net_name: Net name string.
        width: Trace width in mm.
        via_size: Via outer diameter in mm.
        via_drill: Via drill diameter in mm.

    Returns:
        Tuple of `(segments_list, vias_list)`.
    """
    layers = ["F.Cu", "B.Cu"]
    segments: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    if len(path) < 2:
        return segments, vias

    run_start = path[0]
    prev = path[0]
    prev_dir: tuple[int, int] | None = None

    for curr in path[1:]:
        if curr[0] != prev[0]:
            if (prev[1], prev[2]) != (run_start[1], run_start[2]):
                segments.append(
                    {
                        "start": [gx_to_x(run_start[1]), gy_to_y(run_start[2])],
                        "end": [gx_to_x(prev[1]), gy_to_y(prev[2])],
                        "width": width,
                        "layer": layers[run_start[0]],
                        "net": net_name,
                    }
                )
            vias.append(
                {
                    "at": [gx_to_x(curr[1]), gy_to_y(curr[2])],
                    "size": via_size,
                    "drill": via_drill,
                    "layers": ["F.Cu", "B.Cu"],
                    "net": net_name,
                }
            )
            run_start = curr
            prev = curr
            prev_dir = None
            continue

        step_dir = (curr[1] - prev[1], curr[2] - prev[2])
        if prev_dir is not None and step_dir != prev_dir:
            segments.append(
                {
                    "start": [gx_to_x(run_start[1]), gy_to_y(run_start[2])],
                    "end": [gx_to_x(prev[1]), gy_to_y(prev[2])],
                    "width": width,
                    "layer": layers[run_start[0]],
                    "net": net_name,
                }
            )
            run_start = prev
        prev_dir = step_dir
        prev = curr

    if (prev[1], prev[2]) != (run_start[1], run_start[2]):
        segments.append(
            {
                "start": [gx_to_x(run_start[1]), gy_to_y(run_start[2])],
                "end": [gx_to_x(prev[1]), gy_to_y(prev[2])],
                "width": width,
                "layer": layers[run_start[0]],
                "net": net_name,
            }
        )
    return segments, vias


def _discover_freerouting_env(
    freerouting_jar: str | None = None,
) -> tuple[Path | None, Path | None, Path | None, str | None]:
    """Locate `java`, `freerouting.jar`, and KiCad's bundled `python` with `pcbnew`."""
    java_bin = shutil.which("java")
    if not java_bin:
        return None, None, None, "java executable not found on PATH"

    jar_candidates: list[Path] = []
    if freerouting_jar:
        jar_candidates.append(Path(freerouting_jar).expanduser().resolve())
    env_jar = os.environ.get("FREEROUTING_JAR")
    if env_jar:
        jar_candidates.append(Path(env_jar).expanduser().resolve())
    jar_candidates.extend(
        [
            Path(__file__).resolve().parent / "tools" / "freerouting.jar",
            Path(__file__).resolve().parent / "freerouting.jar",
            Path.home() / "freerouting.jar",
            Path(r"C:\Program Files\Freerouting\freerouting.jar"),
            Path("/usr/local/share/freerouting/freerouting.jar"),
        ]
    )
    jar_path = next((p for p in jar_candidates if p.is_file()), None)
    if jar_path is None:
        return Path(java_bin), None, None, "freerouting.jar not found (set FREEROUTING_JAR or pass freerouting_jar)"

    kicad_py: Path | None = None
    try:
        from cli_wrapper import discover_kicad_cli

        cli_dir = discover_kicad_cli().parent
        for name in ("python.exe", "python3", "python"):
            cand = cli_dir / name
            if cand.is_file():
                kicad_py = cand
                break
    except Exception:
        kicad_py = None

    if kicad_py is None:
        return Path(java_bin), jar_path, None, "KiCad bundled Python (pcbnew) not found for DSN/SES bridge"

    return Path(java_bin), jar_path, kicad_py, None


def _prepare_staged_kicad_pro(
    source_pcb: Path,
    staged_pcb: Path,
    trace_width_mm: float,
    clearance_mm: float,
    via_size_mm: float,
    via_drill_mm: float,
    net_widths: dict[str, float] | None,
) -> None:
    """Write a staged `.kicad_pro` with Net Classes and clearances so `ExportSpecctraDSN` passes `net_widths` to Freerouting."""
    src_pro = source_pcb.with_suffix(".kicad_pro")
    dst_pro = staged_pcb.with_suffix(".kicad_pro")
    data: dict[str, Any] = {}
    if src_pro.is_file():
        try:
            data = json.loads(src_pro.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    net_settings = data.setdefault("net_settings", {})
    classes = list(net_settings.get("classes", []))
    default_cls = next((c for c in classes if isinstance(c, dict) and c.get("name") == "Default"), None)
    if default_cls is None:
        default_cls = {"name": "Default"}
        classes.insert(0, default_cls)
    default_cls["clearance"] = float(clearance_mm)
    default_cls["track_width"] = float(trace_width_mm)
    default_cls["via_diameter"] = float(via_size_mm)
    default_cls["via_drill"] = float(via_drill_mm)

    assignments = dict(net_settings.get("netclass_assignments") or {})
    for idx, (net_name, width_val) in enumerate(sorted((net_widths or {}).items())):
        cls_name = f"MCP_W_{idx}"
        classes = [c for c in classes if not (isinstance(c, dict) and c.get("name") == cls_name)]
        classes.append(
            {
                "name": cls_name,
                "clearance": float(clearance_mm),
                "track_width": float(width_val),
                "via_diameter": max(float(via_size_mm), float(width_val) * 0.8),
                "via_drill": float(via_drill_mm),
            }
        )
        assignments[str(net_name)] = cls_name

    net_settings["classes"] = classes
    net_settings["netclass_assignments"] = assignments
    dst_pro.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_freerouting(
    source_pcb: Path,
    dest_pcb: Path,
    java_bin: Path,
    jar_path: Path,
    kicad_py: Path,
    timeout_s: int = 120,
    trace_width_mm: float = 0.3,
    clearance_mm: float = 0.25,
    via_size_mm: float = 0.6,
    via_drill_mm: float = 0.3,
    net_widths: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Export DSN via KiCad `pcbnew`, autoroute via Freerouting CLI, and import SES into `dest_pcb`."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tdir = Path(tmp_dir)
        staged_pcb = tdir / source_pcb.name
        staged_pcb.write_text(source_pcb.read_text(encoding="utf-8"), encoding="utf-8")
        _prepare_staged_kicad_pro(
            source_pcb,
            staged_pcb,
            trace_width_mm=trace_width_mm,
            clearance_mm=clearance_mm,
            via_size_mm=via_size_mm,
            via_drill_mm=via_drill_mm,
            net_widths=net_widths,
        )
        dsn_path = tdir / f"{source_pcb.stem}.dsn"
        ses_path = tdir / f"{source_pcb.stem}.ses"

        export_script = (
            "import pcbnew, sys; "
            "b = pcbnew.LoadBoard(sys.argv[1]); "
            "ok = pcbnew.ExportSpecctraDSN(b, sys.argv[2]); "
            "sys.exit(0 if ok else 2)"
        )
        res_exp = subprocess.run(
            [str(kicad_py), "-c", export_script, str(staged_pcb), str(dsn_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res_exp.returncode != 0 or not dsn_path.is_file():
            raise RuntimeError(f"Specctra DSN export failed: {res_exp.stderr or res_exp.stdout}")

        res_fr = subprocess.run(
            [str(java_bin), "-jar", str(jar_path), "-de", str(dsn_path), "-do", str(ses_path), "-mp", "20"],
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout_s)),
        )
        if res_fr.returncode != 0 or not ses_path.is_file():
            raise RuntimeError(f"Freerouting failed: {res_fr.stderr or res_fr.stdout}")

        import_script = (
            "import pcbnew, sys; "
            "b = pcbnew.LoadBoard(sys.argv[1]); "
            "ok = pcbnew.ImportSpecctraSES(b, sys.argv[2]); "
            "b.Save(sys.argv[3]); "
            "sys.exit(0 if ok else 3)"
        )
        res_imp = subprocess.run(
            [str(kicad_py), "-c", import_script, str(staged_pcb), str(ses_path), str(dest_pcb)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res_imp.returncode != 0 or not dest_pcb.is_file():
            raise RuntimeError(f"Specctra SES import failed: {res_imp.stderr or res_imp.stdout}")

    return {"status": "ok", "saved_path": str(dest_pcb)}


def autoroute_pcb_nets(
    pcb_path: str,
    nets: list[str] | None = None,
    grid_step_mm: float = 0.5,
    trace_width_mm: float = 0.3,
    clearance_mm: float = 0.25,
    allow_vias: bool = True,
    via_size_mm: float = 0.6,
    via_drill_mm: float = 0.3,
    output_path: str | None = None,
    net_widths: dict[str, float] | None = None,
    auto_neckdown: bool = True,
    router: str = "auto",
    freerouting_jar: str | None = None,
    freerouting_timeout_s: int = 120,
) -> dict[str, Any]:
    """Autoroute unrouted connections using Freerouting (when available) with automatic Python A* cleanup/fallback."""
    source = Path(pcb_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PCB file does not exist: {source}")

    router_mode = str(router).strip().lower()
    fallback_reason: str | None = None
    router_used = "astar"

    if router_mode in ("auto", "freerouting"):
        java_bin, jar_path, kicad_py, env_reason = _discover_freerouting_env(freerouting_jar)
        if java_bin and jar_path and kicad_py:
            _, dest_pcb, lock_info = _resolve_target_and_output(pcb_path, output_path, suffix="_routed")
            try:
                fr_res = _run_freerouting(
                    source,
                    dest_pcb,
                    java_bin,
                    jar_path,
                    kicad_py,
                    timeout_s=freerouting_timeout_s,
                    trace_width_mm=trace_width_mm,
                    clearance_mm=clearance_mm,
                    via_size_mm=via_size_mm,
                    via_drill_mm=via_drill_mm,
                    net_widths=net_widths,
                )
                post_unrouted = pcb_inspector.find_unrouted_pads(str(dest_pcb))
                remaining_nets = sorted(
                    {
                        p["net"]
                        for p in (
                            post_unrouted.get("completely_unrouted_pads", [])
                            + post_unrouted.get("zone_only_pads", [])
                        )
                        if p.get("net") and (not nets or p["net"] in set(nets))
                    }
                )
                if remaining_nets and router_mode == "auto":
                    cleanup_res = autoroute_pcb_nets(
                        pcb_path=str(dest_pcb),
                        nets=remaining_nets,
                        grid_step_mm=grid_step_mm,
                        trace_width_mm=trace_width_mm,
                        clearance_mm=clearance_mm,
                        allow_vias=allow_vias,
                        via_size_mm=via_size_mm,
                        via_drill_mm=via_drill_mm,
                        output_path=str(dest_pcb),
                        net_widths=net_widths,
                        auto_neckdown=auto_neckdown,
                        router="astar",
                    )
                    return {
                        **cleanup_res,
                        "status": "ok" if dest_pcb == source else "ok_staged",
                        "router_used": "freerouting+astar_cleanup",
                        "cleanup_nets": remaining_nets,
                        "source_pcb": str(source),
                        "saved_path": str(dest_pcb),
                        "lock_files": lock_info.get("lock_files", []),
                    }
                return {
                    "status": "ok" if dest_pcb == source else "ok_staged",
                    "router_used": "freerouting",
                    "source_pcb": str(source),
                    "saved_path": fr_res["saved_path"],
                    "remaining_unrouted_nets": remaining_nets,
                    "lock_files": lock_info.get("lock_files", []),
                }
            except Exception as exc:
                if router_mode == "freerouting":
                    return {"status": "error", "router_used": "freerouting", "error": str(exc)}
                fallback_reason = str(exc)
                router_used = "astar_fallback"
        else:
            if router_mode == "freerouting":
                return {"status": "error", "router_used": "freerouting", "error": env_reason}
            fallback_reason = env_reason
            router_used = "astar_fallback"

    resolved, tree = pcb_inspector._parse_pcb_file(str(source))
    net_map = pcb_inspector._extract_net_map(tree)
    _, all_pads = pcb_inspector._parse_footprints_and_pads(tree, net_map)
    segments = pcb_inspector._extract_segments(tree, net_map)
    bbox = pcb_inspector._extract_board_bbox(tree) or {
        "min_x": min((float(p["x"]) for p in all_pads), default=0.0) - 5.0,
        "min_y": min((float(p["y"]) for p in all_pads), default=0.0) - 5.0,
        "max_x": max((float(p["x"]) for p in all_pads), default=100.0) + 5.0,
        "max_y": max((float(p["y"]) for p in all_pads), default=100.0) + 5.0,
    }
    unrouted_info = pcb_inspector.find_unrouted_pads(str(source))

    min_x = float(bbox["min_x"]) - 1.0
    min_y = float(bbox["min_y"]) - 1.0
    max_x = float(bbox["max_x"]) + 1.0
    max_y = float(bbox["max_y"]) + 1.0

    step = max(0.2, float(grid_step_mm))
    nx = int(math.ceil((max_x - min_x) / step)) + 1
    ny = int(math.ceil((max_y - min_y) / step)) + 1

    def x_to_gx(x: float) -> int:
        """Convert board X coordinate (mm) to clamped grid X index."""
        return max(0, min(nx - 1, int(round((x - min_x) / step))))

    def y_to_gy(y: float) -> int:
        """Convert board Y coordinate (mm) to clamped grid Y index."""
        return max(0, min(ny - 1, int(round((y - min_y) / step))))

    def gx_to_x(gx: int) -> float:
        """Convert grid X index to board X coordinate (mm)."""
        return round(min_x + gx * step, 4)

    def gy_to_y(gy: int) -> float:
        """Convert grid Y index to board Y coordinate (mm)."""
        return round(min_y + gy * step, 4)

    blocked: list[dict[tuple[int, int], set[str]]] = [{}, {}]
    keepout = clearance_mm + trace_width_mm * 0.5 + step * 0.5

    for pad in all_pads:
        px, py = float(pad["x"]), float(pad["y"])
        rx = max(float(pad.get("size_x", 1.0)) * 0.5, 0.4) + keepout
        ry = max(float(pad.get("size_y", 1.0)) * 0.5, 0.4) + keepout
        pnet = str(pad.get("net", ""))
        pad_layers = pad.get("layers", [])
        l_indices: list[int] = []
        if any(l in pad_layers for l in ("*.Cu", "F.Cu")):
            l_indices.append(0)
        if any(l in pad_layers for l in ("*.Cu", "B.Cu")):
            l_indices.append(1)
        gx0, gx1 = x_to_gx(px - rx), x_to_gx(px + rx)
        gy0, gy1 = y_to_gy(py - ry), y_to_gy(py + ry)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for li in l_indices:
                    blocked[li].setdefault((gx, gy), set()).add(pnet)

    for seg in segments:
        layer = str(seg.get("layer", ""))
        if layer not in ("F.Cu", "B.Cu"):
            continue
        li = 0 if layer == "F.Cu" else 1
        snet = str(seg.get("net", ""))
        sx, sy = float(seg["x1"]), float(seg["y1"])
        ex, ey = float(seg["x2"]), float(seg["y2"])
        rad = float(seg.get("width", 0.3)) * 0.5 + keepout
        gx0, gx1 = x_to_gx(min(sx, ex) - rad), x_to_gx(max(sx, ex) + rad)
        gy0, gy1 = y_to_gy(min(sy, ey) - rad), y_to_gy(max(sy, ey) + rad)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                if _point_to_seg_dist(gx_to_x(gx), gy_to_y(gy), sx, sy, ex, ey) <= rad:
                    blocked[li].setdefault((gx, gy), set()).add(snet)

    target_net_set = set(nets) if nets else None
    pads_by_net: dict[str, list[dict[str, Any]]] = {}
    for pad in all_pads:
        nname = str(pad.get("net", ""))
        if not nname or (target_net_set is not None and nname not in target_net_set):
            continue
        pads_by_net.setdefault(nname, []).append(pad)

    unrouted_keys = {
        (p["reference"], str(p["pad_number"]), p["net"])
        for p in unrouted_info.get("completely_unrouted_pads", []) + unrouted_info.get("zone_only_pads", [])
    }

    all_new_segments: list[dict[str, Any]] = []
    all_new_vias: list[dict[str, Any]] = []
    routed_connections: list[dict[str, Any]] = []
    failed_connections: list[dict[str, Any]] = []

    neighbors = [
        (1, 0, 1.0),
        (-1, 0, 1.0),
        (0, 1, 1.0),
        (0, -1, 1.0),
        (1, 1, 1.4142),
        (1, -1, 1.4142),
        (-1, 1, 1.4142),
        (-1, -1, 1.4142),
    ]

    for net_name, net_pads in sorted(pads_by_net.items()):
        if len(net_pads) < 2:
            continue

        parent = list(range(len(net_pads)))

        def _uf_find(idx: int) -> int:
            """Find Disjoint-Set representative for pad index."""
            while parent[idx] != idx:
                parent[idx] = parent[parent[idx]]
                idx = parent[idx]
            return idx

        def _uf_union(a: int, b: int) -> None:
            """Merge two pad clusters in Disjoint-Set."""
            ra, rb = _uf_find(a), _uf_find(b)
            if ra != rb:
                parent[rb] = ra

        net_segs = [s for s in segments if str(s.get("net", "")) == net_name]
        if net_segs:
            seg_adj: dict[tuple[str, float, float], list[tuple[str, float, float]]] = {}
            for s in net_segs:
                lyr = str(s.get("layer", ""))
                p1 = (lyr, round(float(s["x1"]), 3), round(float(s["y1"]), 3))
                p2 = (lyr, round(float(s["x2"]), 3), round(float(s["y2"]), 3))
                seg_adj.setdefault(p1, []).append(p2)
                seg_adj.setdefault(p2, []).append(p1)

            pad_clusters: dict[int, set[tuple[str, float, float]]] = {}
            for idx, pad in enumerate(net_pads):
                px, py = float(pad["x"]), float(pad["y"])
                conn_r = max(min(float(pad.get("size_x", 1.0)), float(pad.get("size_y", 1.0))) * 0.5, 0.25) + 0.05
                plyrs = pad.get("layers", [])
                seeds = [
                    pt
                    for pt in seg_adj
                    if any(
                        pt[0] == pl or (pl == "*.Cu" and pt[0] in ("F.Cu", "B.Cu"))
                        for pl in plyrs
                    )
                    and math.hypot(pt[1] - px, pt[2] - py) <= conn_r
                ]
                visited: set[tuple[str, float, float]] = set(seeds)
                stack = list(seeds)
                while stack:
                    cur = stack.pop()
                    for nxt in seg_adj.get(cur, ()):
                        if nxt not in visited:
                            visited.add(nxt)
                            stack.append(nxt)
                pad_clusters[idx] = visited
            for i in range(len(net_pads)):
                if not pad_clusters[i]:
                    continue
                for j in range(i + 1, len(net_pads)):
                    if pad_clusters[i] & pad_clusters[j]:
                        _uf_union(i, j)

        while len({_uf_find(i) for i in range(len(net_pads))}) > 1:
            best_pair: tuple[int, int] | None = None
            best_dist = 1e18
            for i in range(len(net_pads)):
                for j in range(i + 1, len(net_pads)):
                    if _uf_find(i) == _uf_find(j):
                        continue
                    d = math.hypot(
                        float(net_pads[i]["x"]) - float(net_pads[j]["x"]),
                        float(net_pads[i]["y"]) - float(net_pads[j]["y"]),
                    )
                    if d < best_dist:
                        best_dist = d
                        best_pair = (i, j)
            if best_pair is None:
                break
            src_idx, dst_idx = best_pair
            src_pad, dst_pad = net_pads[src_idx], net_pads[dst_idx]
            _uf_union(src_idx, dst_idx)

            sgx, sgy = x_to_gx(float(src_pad["x"])), y_to_gy(float(src_pad["y"]))
            dgx, dgy = x_to_gx(float(dst_pad["x"])), y_to_gy(float(dst_pad["y"]))

            src_layers = [0] if "F.Cu" in src_pad.get("layers", []) else ([1] if "B.Cu" in src_pad.get("layers", []) else [0, 1])
            dst_layers = {0} if "F.Cu" in dst_pad.get("layers", []) else ({1} if "B.Cu" in dst_pad.get("layers", []) else {0, 1})

            open_heap: list[tuple[float, float, int, int, int]] = []
            g_score: dict[tuple[int, int, int], float] = {}
            came_from: dict[tuple[int, int, int], tuple[int, int, int]] = {}

            for sli in src_layers:
                state = (sli, sgx, sgy)
                g_score[state] = 0.0
                h = math.hypot(dgx - sgx, dgy - sgy)
                heapq.heappush(open_heap, (h, 0.0, sli, sgx, sgy))

            found_goal: tuple[int, int, int] | None = None
            max_iters = 60000
            iters = 0

            net_trace_w = float((net_widths or {}).get(net_name, trace_width_mm))
            extra_cells = int(math.ceil(max(0.0, (net_trace_w - trace_width_mm) * 0.5) / step))
            via_r_cells = int(
                math.ceil(max(0.0, (via_size_mm * 0.5 + clearance_mm - trace_width_mm * 0.5)) / step)
            )

            def _cell_blocked(layer_idx: int, cx: int, cy: int, radius: int = extra_cells) -> bool:
                """Check whether routing at `(cx, cy)` with `radius` clearance hits another net."""
                if radius <= 0:
                    occ = blocked[layer_idx].get((cx, cy))
                    return bool(occ and (occ - {net_name}) and (cx, cy) not in {(sgx, sgy), (dgx, dgy)})
                for ox in range(-radius, radius + 1):
                    for oy in range(-radius, radius + 1):
                        if ox * ox + oy * oy > radius * radius + 1:
                            continue
                        tx, ty = cx + ox, cy + oy
                        occ = blocked[layer_idx].get((tx, ty))
                        if occ and (occ - {net_name}) and (tx, ty) not in {(sgx, sgy), (dgx, dgy)}:
                            return True
                return False

            while open_heap and iters < max_iters:
                iters += 1
                _, g_curr, li, gx, gy = heapq.heappop(open_heap)
                if (gx, gy) == (dgx, dgy) and li in dst_layers:
                    found_goal = (li, gx, gy)
                    break
                if g_curr > g_score.get((li, gx, gy), 1e18):
                    continue

                for dx, dy, step_cost in neighbors:
                    ngx, ngy = gx + dx, gy + dy
                    if not (0 <= ngx < nx and 0 <= ngy < ny):
                        continue
                    if _cell_blocked(li, ngx, ngy):
                        continue
                    dir_bias = 0.05 if (li == 0 and dx == 0) or (li == 1 and dy == 0) else 0.0
                    ng = g_curr + step_cost + dir_bias
                    nstate = (li, ngx, ngy)
                    if ng < g_score.get(nstate, 1e18):
                        g_score[nstate] = ng
                        came_from[nstate] = (li, gx, gy)
                        nh = math.hypot(dgx - ngx, dgy - ngy)
                        heapq.heappush(open_heap, (ng + nh, ng, li, ngx, ngy))

                if allow_vias:
                    oli = 1 - li
                    if not _cell_blocked(0, gx, gy, via_r_cells) and not _cell_blocked(1, gx, gy, via_r_cells):
                        ng = g_curr + 6.0
                        vstate = (oli, gx, gy)
                        if ng < g_score.get(vstate, 1e18):
                            g_score[vstate] = ng
                            came_from[vstate] = (li, gx, gy)
                            nh = math.hypot(dgx - gx, dgy - gy)
                            heapq.heappush(open_heap, (ng + nh, ng, oli, gx, gy))

            if found_goal is None:
                failed_connections.append(
                    {
                        "net": net_name,
                        "from": f"{src_pad['reference']}.{src_pad['pad_number']}",
                        "to": f"{dst_pad['reference']}.{dst_pad['pad_number']}",
                    }
                )
                continue

            grid_path: list[tuple[int, int, int]] = []
            curr_state: tuple[int, int, int] | None = found_goal
            while curr_state is not None:
                grid_path.append(curr_state)
                curr_state = came_from.get(curr_state)
            grid_path.reverse()

            mark_r = max(0, extra_cells)
            for idx_gp, (pli, pgx, pgy) in enumerate(grid_path):
                for ox in range(-mark_r, mark_r + 1):
                    for oy in range(-mark_r, mark_r + 1):
                        if ox * ox + oy * oy <= mark_r * mark_r + 1:
                            blocked[pli].setdefault((pgx + ox, pgy + oy), set()).add(net_name)
                if idx_gp > 0 and grid_path[idx_gp - 1][0] != pli:
                    vr = max(1, via_r_cells + 1)
                    for ox in range(-vr, vr + 1):
                        for oy in range(-vr, vr + 1):
                            if ox * ox + oy * oy <= vr * vr + 1:
                                blocked[0].setdefault((pgx + ox, pgy + oy), set()).add(net_name)
                                blocked[1].setdefault((pgx + ox, pgy + oy), set()).add(net_name)

            segs, vias = _simplify_grid_path(
                grid_path,
                gx_to_x,
                gy_to_y,
                net_name,
                net_trace_w,
                via_size_mm,
                via_drill_mm,
            )
            src_xy = [round(float(src_pad["x"]), 4), round(float(src_pad["y"]), 4)]
            dst_xy = [round(float(dst_pad["x"]), 4), round(float(dst_pad["y"]), 4)]
            s0_xy = [gx_to_x(grid_path[0][1]), gy_to_y(grid_path[0][2])]
            d0_xy = [gx_to_x(grid_path[-1][1]), gy_to_y(grid_path[-1][2])]
            src_lyr_name = "F.Cu" if grid_path[0][0] == 0 else "B.Cu"
            dst_lyr_name = "F.Cu" if grid_path[-1][0] == 0 else "B.Cu"
            src_pad_w = min(float(src_pad.get("size_x", net_trace_w)), float(src_pad.get("size_y", net_trace_w)))
            dst_pad_w = min(float(dst_pad.get("size_x", net_trace_w)), float(dst_pad.get("size_y", net_trace_w)))
            src_neck_w = round(max(0.2, min(net_trace_w, src_pad_w * 0.85)), 3) if (auto_neckdown and net_trace_w > src_pad_w * 0.9) else net_trace_w
            dst_neck_w = round(max(0.2, min(net_trace_w, dst_pad_w * 0.85)), 3) if (auto_neckdown and net_trace_w > dst_pad_w * 0.9) else net_trace_w
            if math.hypot(src_xy[0] - s0_xy[0], src_xy[1] - s0_xy[1]) > 0.01:
                segs.insert(0, {"start": src_xy, "end": s0_xy, "width": src_neck_w, "layer": src_lyr_name, "net": net_name})
            elif segs and auto_neckdown and net_trace_w > src_pad_w * 0.9:
                segs[0]["width"] = src_neck_w
            if math.hypot(dst_xy[0] - d0_xy[0], dst_xy[1] - d0_xy[1]) > 0.01:
                segs.append({"start": d0_xy, "end": dst_xy, "width": dst_neck_w, "layer": dst_lyr_name, "net": net_name})
            elif segs and auto_neckdown and net_trace_w > dst_pad_w * 0.9:
                segs[-1]["width"] = dst_neck_w
            all_new_segments.extend(segs)
            all_new_vias.extend(vias)
            routed_connections.append(
                {
                    "net": net_name,
                    "from": f"{src_pad['reference']}.{src_pad['pad_number']}",
                    "to": f"{dst_pad['reference']}.{dst_pad['pad_number']}",
                    "segments": len(segs),
                    "vias": len(vias),
                }
            )

    write_res = edit_pcb_traces(
        pcb_path=str(source),
        add_segments=all_new_segments,
        add_vias=all_new_vias,
        output_path=output_path,
    )
    return {
        "status": write_res["status"],
        "router_used": router_used,
        **({"fallback_reason": fallback_reason} if fallback_reason else {}),
        "source_pcb": str(source),
        "saved_path": write_res["saved_path"],
        "routed_count": len(routed_connections),
        "failed_count": len(failed_connections),
        "routed_connections": routed_connections,
        "failed_connections": failed_connections,
        "added_segments": write_res["added_segments"],
        "added_vias": write_res["added_vias"],
        "lock_files": write_res["lock_files"],
    }
