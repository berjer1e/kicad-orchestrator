"""Pure-Python KiCad `.kicad_pcb` S-expression inspector, trace analyzer, and safety auditor.

Provides lock-file awareness, layout/geometry extraction, IPC-2221 trace width
and voltage drop analysis, unrouted/zone-only pad verification, mechanical fit
and stacked mezzanine auditing, and interactive design decision management
across KiCad 8, 9, and 10 boards for the `kicad_mcp` server and CLI toolchain.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import heapq
import json
import math
import os
from pathlib import Path
import re
from typing import Any


class _QuotedStr(str):
    """String subclass distinguishing quoted S-expression literals from bare atoms."""


def _require_pcb_file(pcb_path: str) -> Path:
    """Resolve `pcb_path` and verify that it points to an existing file.

    Args:
        pcb_path: Path to a `.kicad_pcb` file.

    Returns:
        Resolved `Path` object for the existing PCB file.
    """
    resolved = Path(pcb_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"PCB file does not exist: {resolved}")
    return resolved


def _read_lock_holder(lock_path: Path) -> Any:
    """Safely read and parse a KiCad `.lck` file as JSON or plain text.

    Args:
        lock_path: Path to a `.lck` file on disk.

    Returns:
        Parsed JSON object, plain-text string, `None` if empty, or an error dict.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return {"error": str(exc)}

    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def check_kicad_lock_files(target_path: str) -> dict[str, Any]:
    """Inspect a KiCad file's directory or a directory for active `.lck` lock files.

    Args:
        target_path: Path to a KiCad project/board/schematic file or directory.

    Returns:
        Dictionary summarizing discovered `.lck` files, holder metadata, lock
        status flags (`locked`, `target_locked`), and a conflict warning if any.
    """
    resolved = Path(target_path).expanduser().resolve()
    is_dir_target = resolved.is_dir()
    scan_dir = resolved if is_dir_target else resolved.parent

    target_lock_names: set[str] = set()
    if not is_dir_target:
        target_lock_names = {
            f"~{resolved.name}.lck",
            f"{resolved.name}.lck",
            f"~{resolved.stem}.kicad_pro.lck",
        }
        if os.name == "nt":
            target_lock_names = {name.lower() for name in target_lock_names}

    lock_entries: list[dict[str, Any]] = []
    target_locked = False

    if scan_dir.is_dir():
        candidates = sorted(
            (
                entry
                for entry in scan_dir.iterdir()
                if entry.is_file() and entry.name.lower().endswith(".lck")
            ),
            key=lambda p: p.name.lower(),
        )
        for entry in candidates:
            lock_entries.append(
                {
                    "path": str(entry),
                    "name": entry.name,
                    "holder": _read_lock_holder(entry),
                }
            )
            if not is_dir_target:
                cmp_name = entry.name.lower() if os.name == "nt" else entry.name
                if cmp_name in target_lock_names:
                    target_locked = True

    if is_dir_target:
        target_locked = len(lock_entries) > 0

    warning = (
        f"KiCad GUI lock detected ({len(lock_entries)} .lck file(s)): concurrent edits on disk may conflict with an open KiCad window."
        if lock_entries
        else None
    )

    return {
        "status": "ok",
        "target_path": str(resolved),
        "locked": len(lock_entries) > 0,
        "target_locked": target_locked,
        "lock_count": len(lock_entries),
        "lock_files": lock_entries,
        "warning": warning,
    }


def _parse_sexpr(text: str) -> list[Any]:
    """Parse a KiCad S-expression document into nested Python lists using balanced parentheses.

    Args:
        text: Raw UTF-8 text contents of a `.kicad_pcb` file.

    Returns:
        Top-level S-expression list node.
    """
    stack: list[list[Any]] = [[]]
    idx = 0
    length = len(text)
    escape_map = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}

    while idx < length:
        ch = text[idx]
        if ch.isspace():
            idx += 1
        elif ch == "(":
            node: list[Any] = []
            stack[-1].append(node)
            stack.append(node)
            idx += 1
        elif ch == ")":
            if len(stack) <= 1:
                raise ValueError(f"Unexpected ')' at offset {idx} in S-expression.")
            stack.pop()
            idx += 1
        elif ch == '"':
            idx += 1
            buf: list[str] = []
            terminated = False
            while idx < length:
                c = text[idx]
                if c == "\\":
                    idx += 1
                    if idx >= length:
                        raise ValueError("Unterminated escape sequence inside string literal.")
                    esc = text[idx]
                    buf.append(escape_map.get(esc, esc))
                    idx += 1
                elif c == '"':
                    idx += 1
                    terminated = True
                    break
                else:
                    buf.append(c)
                    idx += 1
            if not terminated:
                raise ValueError("Unterminated quoted string in S-expression.")
            stack[-1].append(_QuotedStr("".join(buf)))
        else:
            end = idx
            while end < length and not text[end].isspace() and text[end] not in '()"':
                end += 1
            stack[-1].append(text[idx:end])
            idx = end

    if len(stack) != 1:
        raise ValueError("Unclosed '(' in S-expression.")
    if not stack[0] or not isinstance(stack[0][0], list):
        raise ValueError("Empty or invalid KiCad S-expression document.")
    return stack[0][0]


def _parse_pcb_file(pcb_path: str) -> tuple[Path, list[Any]]:
    """Read and parse a `.kicad_pcb` file into an S-expression AST.

    Args:
        pcb_path: Path to the `.kicad_pcb` file.

    Returns:
        Tuple of `(resolved_path, root_sexpr_list)`.
    """
    resolved = _require_pcb_file(pcb_path)
    text = resolved.read_text(encoding="utf-8", errors="replace")
    tree = _parse_sexpr(text)
    if not tree or tree[0] != "kicad_pcb":
        raise ValueError(f"File is not a valid kicad_pcb S-expression: {resolved}")
    return resolved, tree


def _first_child(node: list[Any], tag: str) -> list[Any] | None:
    """Return the first child list of `node` whose head symbol matches `tag`.

    Args:
        node: Parent S-expression list node.
        tag: Tag name to look up.

    Returns:
        First matching child list, or `None` if absent.
    """
    for item in node[1:]:
        if isinstance(item, list) and item and item[0] == tag:
            return item
    return None


def _child_nodes(node: list[Any], tag: str) -> list[list[Any]]:
    """Return all child lists of `node` whose head symbol matches `tag`.

    Args:
        node: Parent S-expression list node.
        tag: Tag name to match.

    Returns:
        List of matching child S-expression lists.
    """
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == tag]


def _atom_str(node: list[Any] | None, index: int, default: str = "") -> str:
    """Extract an atom from `node[index]` as a plain `str`.

    Args:
        node: S-expression list node, or `None`.
        index: Positional index inside `node`.
        default: Fallback string when missing or non-atomic.

    Returns:
        Extracted string value or `default`.
    """
    if node is None or index >= len(node):
        return default
    val = node[index]
    return str(val) if isinstance(val, str) else default


def _atom_float(node: list[Any] | None, index: int, default: float = 0.0) -> float:
    """Extract a numeric atom from `node[index]` as a `float`.

    Args:
        node: S-expression list node, or `None`.
        index: Positional index inside `node`.
        default: Fallback float when missing or non-numeric.

    Returns:
        Parsed floating-point value or `default`.
    """
    if node is None or index >= len(node):
        return default
    val = node[index]
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return default
    return default


def _extract_net_map(tree: list[Any]) -> dict[str, str]:
    """Collect top-level legacy `(net <id> "<name>")` declarations into a lookup table.

    Args:
        tree: Root `kicad_pcb` S-expression list.

    Returns:
        Mapping from stringified net index to net name.
    """
    net_map: dict[str, str] = {}
    for item in _child_nodes(tree, "net"):
        if len(item) >= 3 and isinstance(item[1], str) and isinstance(item[2], str):
            net_map[str(item[1])] = str(item[2])
    return net_map


def _resolve_net_name(
    net_node: list[Any] | None,
    net_map: dict[str, str],
    net_name_node: list[Any] | None = None,
) -> str:
    """Resolve a net name supporting KiCad 10 `(net "GND")` and legacy `(net 1 "GND")` / `(net 1)`.

    Args:
        net_node: S-expression `(net ...)` child node.
        net_map: Top-level legacy net ID-to-name lookup map.
        net_name_node: Optional sibling `(net_name "...")` node found in legacy zones.

    Returns:
        Resolved net name, or `""` for unconnected/unnamed nets.
    """
    override = _atom_str(net_name_node, 1)
    if override:
        return override

    if net_node is None or len(net_node) < 2:
        return ""

    if len(net_node) >= 3:
        return _atom_str(net_node, 2)

    token = net_node[1]
    if isinstance(token, _QuotedStr):
        return str(token)

    key = str(token)
    if key in net_map:
        return net_map[key]
    if key == "0":
        return ""
    return key


def _local_to_global(
    px: float,
    py: float,
    fx: float,
    fy: float,
    frot_deg: float,
) -> tuple[float, float]:
    """Transform footprint-local `(px, py)` coordinates into global PCB coordinates.

    Args:
        px: Footprint-local X coordinate in mm.
        py: Footprint-local Y coordinate in mm.
        fx: Footprint origin X coordinate on PCB in mm.
        fy: Footprint origin Y coordinate on PCB in mm.
        frot_deg: Footprint rotation in degrees.

    Returns:
        Global `(gx, gy)` coordinate tuple in mm.
    """
    ang = -math.radians(frot_deg)
    cos_a = math.cos(ang)
    sin_a = math.sin(ang)
    gx = fx + px * cos_a - py * sin_a
    gy = fy + px * sin_a + py * cos_a
    return gx, gy


def _arc_bbox_points(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Compute extreme bounding points for a 3-point circular arc.

    Args:
        start: Global arc start `(x, y)`.
        mid: Global arc midpoint `(x, y)`.
        end: Global arc end `(x, y)`.

    Returns:
        List of `(x, y)` points whose min/max encloses the entire circular arc.
    """
    x1, y1 = start
    x2, y2 = mid
    x3, y3 = end
    points = [start, mid, end]

    denom = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(denom) < 1e-9:
        return points

    s1 = x1 * x1 + y1 * y1
    s2 = x2 * x2 + y2 * y2
    s3 = x3 * x3 + y3 * y3
    cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / denom
    cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / denom
    radius = math.hypot(x1 - cx, y1 - cy)

    a_start = math.atan2(y1 - cy, x1 - cx)
    a_mid = math.atan2(y2 - cy, x2 - cx)
    a_end = math.atan2(y3 - cy, x3 - cx)

    two_pi = 2.0 * math.pi
    d_mid = (a_mid - a_start) % two_pi
    d_end = (a_end - a_start) % two_pi
    ccw = d_mid <= d_end

    for cardinal in (0.0, math.pi * 0.5, math.pi, -math.pi * 0.5):
        rel = (cardinal - a_start) % two_pi
        on_arc = (rel <= d_end) if ccw else (rel >= d_end or rel == 0.0)
        if on_arc:
            points.append((cx + radius * math.cos(cardinal), cy + radius * math.sin(cardinal)))

    return points


def _legacy_arc_bbox_points(
    center: tuple[float, float],
    start: tuple[float, float],
    angle_deg: float,
) -> list[tuple[float, float]]:
    """Compute extreme bounding points for a legacy `(start cx cy) (end x y) (angle deg)` arc.

    Args:
        center: Global arc center `(cx, cy)`.
        start: Global arc start `(x, y)`.
        angle_deg: Signed sweep angle in degrees.

    Returns:
        List of `(x, y)` points bounding the arc.
    """
    cx, cy = center
    sx, sy = start
    radius = math.hypot(sx - cx, sy - cy)
    if radius < 1e-9:
        return [center, start]

    a_start = math.atan2(sy - cy, sx - cx)
    sweep = math.radians(angle_deg)
    a_mid = a_start + sweep * 0.5
    a_end = a_start + sweep
    mid_pt = (cx + radius * math.cos(a_mid), cy + radius * math.sin(a_mid))
    end_pt = (cx + radius * math.cos(a_end), cy + radius * math.sin(a_end))
    return _arc_bbox_points(start, mid_pt, end_pt)


def _point_to_segment_distance(
    px: float,
    py: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Calculate minimum Euclidean distance between a point and a 2D line segment.

    Args:
        px: Point X coordinate in mm.
        py: Point Y coordinate in mm.
        x1: Segment start X coordinate in mm.
        y1: Segment start Y coordinate in mm.
        x2: Segment end X coordinate in mm.
        y2: Segment end Y coordinate in mm.

    Returns:
        Shortest distance in mm from `(px, py)` to segment `(x1, y1)-(x2, y2)`.
    """
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq == 0.0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _layers_match(pad_layers: list[str], copper_layer: str) -> bool:
    """Check whether a pad's layer specification connects to a given copper layer.

    Args:
        pad_layers: Layer strings defined on a pad (e.g. `["*.Cu", "*.Mask"]` or `["F.Cu"]`).
        copper_layer: Target copper layer name (e.g. `"F.Cu"`, `"B.Cu"`).

    Returns:
        `True` if the pad exposes copper on `copper_layer`.
    """
    if not copper_layer.endswith(".Cu"):
        return False
    if "*.Cu" in pad_layers:
        return True
    if "F&B.Cu" in pad_layers and copper_layer in ("F.Cu", "B.Cu"):
        return True
    return copper_layer in pad_layers


def _extract_node_layers(node: list[Any]) -> list[str]:
    """Extract copper layer names from a `(layer ...)` or `(layers ...)` child node.

    Args:
        node: Parent S-expression list node (e.g. `zone`, `via`, or `pad`).

    Returns:
        List of layer name strings.
    """
    layers_node = _first_child(node, "layers")
    if layers_node is not None:
        return [str(tok) for tok in layers_node[1:] if isinstance(tok, str)]
    layer_node = _first_child(node, "layer")
    if layer_node is not None and len(layer_node) >= 2:
        return [_atom_str(layer_node, 1)]
    return []


def _extract_board_bbox(tree: list[Any]) -> dict[str, float] | None:
    """Compute the outer board bounding box from all `Edge.Cuts` graphical items.

    Args:
        tree: Root `kicad_pcb` S-expression list.

    Returns:
        Dictionary with `min_x`, `min_y`, `max_x`, `max_y`, `width_mm`, `height_mm`,
        and `area_mm2`, or `None` when no `Edge.Cuts` geometry is present.
    """
    xs: list[float] = []
    ys: list[float] = []

    for tag in ("gr_rect", "gr_line", "gr_poly", "gr_arc", "gr_circle"):
        for item in _child_nodes(tree, tag):
            layer_val = _atom_str(_first_child(item, "layer"), 1)
            if layer_val != "Edge.Cuts":
                continue

            if tag in ("gr_rect", "gr_line"):
                start = _first_child(item, "start")
                end = _first_child(item, "end")
                if start and end:
                    xs.extend([_atom_float(start, 1), _atom_float(end, 1)])
                    ys.extend([_atom_float(start, 2), _atom_float(end, 2)])
            elif tag == "gr_poly":
                pts_node = _first_child(item, "pts")
                if pts_node:
                    for xy in _child_nodes(pts_node, "xy"):
                        xs.append(_atom_float(xy, 1))
                        ys.append(_atom_float(xy, 2))
            elif tag == "gr_circle":
                center = _first_child(item, "center")
                end = _first_child(item, "end")
                if center and end:
                    cx, cy = _atom_float(center, 1), _atom_float(center, 2)
                    ex, ey = _atom_float(end, 1), _atom_float(end, 2)
                    radius = math.hypot(ex - cx, ey - cy)
                    xs.extend([cx - radius, cx + radius])
                    ys.extend([cy - radius, cy + radius])
            elif tag == "gr_arc":
                start = _first_child(item, "start")
                mid = _first_child(item, "mid")
                end = _first_child(item, "end")
                angle = _first_child(item, "angle")
                if start and mid and end:
                    pts = _arc_bbox_points(
                        (_atom_float(start, 1), _atom_float(start, 2)),
                        (_atom_float(mid, 1), _atom_float(mid, 2)),
                        (_atom_float(end, 1), _atom_float(end, 2)),
                    )
                    for px, py in pts:
                        xs.append(px)
                        ys.append(py)
                elif start and end and angle:
                    pts = _legacy_arc_bbox_points(
                        (_atom_float(start, 1), _atom_float(start, 2)),
                        (_atom_float(end, 1), _atom_float(end, 2)),
                        _atom_float(angle, 1),
                    )
                    for px, py in pts:
                        xs.append(px)
                        ys.append(py)

    if not xs or not ys:
        return None

    min_x = min(xs)
    min_y = min(ys)
    max_x = max(xs)
    max_y = max(ys)
    width = max(0.0, max_x - min_x)
    height = max(0.0, max_y - min_y)
    return {
        "min_x": round(min_x, 4),
        "min_y": round(min_y, 4),
        "max_x": round(max_x, 4),
        "max_y": round(max_y, 4),
        "width_mm": round(width, 4),
        "height_mm": round(height, 4),
        "area_mm2": round(width * height, 4),
    }


def _extract_footprint_metadata(fp_node: list[Any]) -> tuple[str, str, str]:
    """Extract reference, value, and description from a footprint node across KiCad versions.

    Args:
        fp_node: S-expression `(footprint ...)` list node.

    Returns:
        Tuple `(reference, value, description)`.
    """
    reference = ""
    value = ""
    description = _atom_str(_first_child(fp_node, "descr"), 1)

    for prop in _child_nodes(fp_node, "property"):
        key = _atom_str(prop, 1)
        val = _atom_str(prop, 2)
        if key == "Reference" and not reference:
            reference = val
        elif key == "Value" and not value:
            value = val
        elif key == "Description" and val and not description:
            description = val

    if not reference or not value:
        for fp_text in _child_nodes(fp_node, "fp_text"):
            kind = _atom_str(fp_text, 1)
            val = _atom_str(fp_text, 2)
            if kind == "reference" and not reference:
                reference = val
            elif kind == "value" and not value:
                value = val

    return reference, value, description


def _estimate_component_height_mm(
    reference: str,
    value: str,
    library_link: str,
    description: str,
    pad_count: int,
) -> float:
    """Estimate 3D component height in millimeters based on library, value, and reference tokens.

    Args:
        reference: Reference designator string.
        value: Footprint value string.
        library_link: KiCad library and footprint identifier.
        description: Footprint description string.
        pad_count: Number of pads on the footprint.

    Returns:
        Estimated height in millimeters above the PCB surface.
    """
    combined = f"{reference} {value} {library_link} {description}".lower()

    if "mountinghole" in combined or "mounting_hole" in combined or re.match(r"^H\d*$", reference, re.IGNORECASE):
        return 0.0

    if "crystal_hc49" in combined or "hc-49" in combined:
        if "vertical" in combined or "-4h" in combined:
            return 11.0
        return 4.0

    if "dip" in combined and "socket" in combined:
        return 8.5
    if "package_dip" in combined or re.search(r"\bdip-\d+\b", combined):
        return 5.0

    if "mt3608" in combined:
        return 7.5

    if "jst_xh" in combined or "b4b-xh" in combined or "b2b-xh" in combined:
        if "vertical" in combined:
            return 7.0
        return 5.5

    m_btn_h = re.search(r"\bh(\d+(?:\.\d+)?)mm\b", combined)
    if m_btn_h:
        return float(m_btn_h.group(1))
    if "sw_push" in combined or "tact" in combined:
        return 5.0

    if "c_disc" in combined:
        return 6.0
    if "cp_radial" in combined or "electrolytic" in combined:
        return 11.0

    if "r_axial" in combined:
        if "vertical" in combined:
            return 7.5
        return 3.0

    if "d_do-35" in combined or "sod27" in combined:
        if "vertical" in combined:
            return 7.0
        return 3.0

    if "tp4056" in combined or "hw373" in combined:
        return 4.5

    if "pinheader" in combined or "femaleheader" in combined or "pin_header" in combined or "female_header" in combined:
        return 8.5

    if "smd" in combined or re.search(r"\b(?:0805|0603|1206|0402|sot-23|sot-223|soic-\d+|tssop-\d+|qfp-\d+)\b", combined):
        return 1.5

    if re.search(r"\b(?:tht|through_hole)\b", combined) or pad_count > 0:
        return 6.0

    return 2.5


def _pad_bbox_points(
    gx: float,
    gy: float,
    sx: float,
    sy: float,
    pad_shape: str,
    pad_rot_deg: float,
) -> list[tuple[float, float]]:
    """Compute global extreme points enclosing a pad centered at `(gx, gy)`.

    Args:
        gx: Global pad center X in mm.
        gy: Global pad center Y in mm.
        sx: Pad width in mm.
        sy: Pad height in mm.
        pad_shape: KiCad pad shape name (`circle`, `rect`, `roundrect`, `oval`, etc.).
        pad_rot_deg: Pad board-space rotation in degrees.

    Returns:
        List of global `(x, y)` corner/extent points.
    """
    hx = sx * 0.5
    hy = sy * 0.5
    if pad_shape == "circle":
        r = max(hx, hy)
        return [(gx - r, gy - r), (gx + r, gy + r)]

    ang = -math.radians(pad_rot_deg)
    cos_a = math.cos(ang)
    sin_a = math.sin(ang)
    pts: list[tuple[float, float]] = []
    for dx in (-hx, hx):
        for dy in (-hy, hy):
            pts.append((gx + dx * cos_a - dy * sin_a, gy + dx * sin_a + dy * cos_a))
    return pts


def _parse_footprints_and_pads(
    tree: list[Any],
    net_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse all footprints, their global bounding boxes, and their extracted pads.

    Args:
        tree: Root `kicad_pcb` S-expression list.
        net_map: Legacy net ID-to-name lookup map.

    Returns:
        Tuple `(footprints_list, pads_list)` with global coordinates resolved.
    """
    footprints: list[dict[str, Any]] = []
    all_pads: list[dict[str, Any]] = []

    for fp_node in _child_nodes(tree, "footprint"):
        library_link = _atom_str(fp_node, 1)
        fp_layer = _atom_str(_first_child(fp_node, "layer"), 1, "F.Cu")
        at_node = _first_child(fp_node, "at")
        fx = _atom_float(at_node, 1)
        fy = _atom_float(at_node, 2)
        frot = _atom_float(at_node, 3, 0.0)
        reference, value, description = _extract_footprint_metadata(fp_node)

        bbox_xs: list[float] = []
        bbox_ys: list[float] = []
        fp_nets: set[str] = set()
        pad_nodes = _child_nodes(fp_node, "pad")

        for pad_node in pad_nodes:
            pad_number = _atom_str(pad_node, 1)
            pad_type = _atom_str(pad_node, 2)
            pad_shape = _atom_str(pad_node, 3)
            pad_at = _first_child(pad_node, "at")
            px = _atom_float(pad_at, 1)
            py = _atom_float(pad_at, 2)
            prot = _atom_float(pad_at, 3, frot)
            size_node = _first_child(pad_node, "size")
            sx = _atom_float(size_node, 1)
            sy = _atom_float(size_node, 2, sx)
            layers = _extract_node_layers(pad_node)
            net_name = _resolve_net_name(_first_child(pad_node, "net"), net_map)

            gx, gy = _local_to_global(px, py, fx, fy, frot)
            pad_pts = _pad_bbox_points(gx, gy, sx, sy, pad_shape, prot)
            pad_w_rot = max(pt[0] for pt in pad_pts) - min(pt[0] for pt in pad_pts)
            pad_h_rot = max(pt[1] for pt in pad_pts) - min(pt[1] for pt in pad_pts)
            for bx, by in pad_pts:
                bbox_xs.append(bx)
                bbox_ys.append(by)

            if net_name:
                fp_nets.add(net_name)

            all_pads.append(
                {
                    "reference": reference,
                    "pad_number": pad_number,
                    "pad_type": pad_type,
                    "x": round(gx, 4),
                    "y": round(gy, 4),
                    "size_x": round(pad_w_rot, 4),
                    "size_y": round(pad_h_rot, 4),
                    "raw_size_x": round(sx, 4),
                    "raw_size_y": round(sy, 4),
                    "rotation": round(prot, 4),
                    "layers": layers,
                    "net": net_name,
                }
            )

        for gfx_tag in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            for gfx in _child_nodes(fp_node, gfx_tag):
                if gfx_tag == "fp_line":
                    start = _first_child(gfx, "start")
                    end = _first_child(gfx, "end")
                    if start and end:
                        for pt in (start, end):
                            gx, gy = _local_to_global(_atom_float(pt, 1), _atom_float(pt, 2), fx, fy, frot)
                            bbox_xs.append(gx)
                            bbox_ys.append(gy)
                elif gfx_tag == "fp_rect":
                    start = _first_child(gfx, "start")
                    end = _first_child(gfx, "end")
                    if start and end:
                        x1, y1 = _atom_float(start, 1), _atom_float(start, 2)
                        x2, y2 = _atom_float(end, 1), _atom_float(end, 2)
                        for cx, cy in ((x1, y1), (x1, y2), (x2, y1), (x2, y2)):
                            gx, gy = _local_to_global(cx, cy, fx, fy, frot)
                            bbox_xs.append(gx)
                            bbox_ys.append(gy)
                elif gfx_tag == "fp_circle":
                    center = _first_child(gfx, "center")
                    end = _first_child(gfx, "end")
                    if center and end:
                        cx, cy = _atom_float(center, 1), _atom_float(center, 2)
                        ex, ey = _atom_float(end, 1), _atom_float(end, 2)
                        radius = math.hypot(ex - cx, ey - cy)
                        gcx, gcy = _local_to_global(cx, cy, fx, fy, frot)
                        bbox_xs.extend([gcx - radius, gcx + radius])
                        bbox_ys.extend([gcy - radius, gcy + radius])
                elif gfx_tag == "fp_arc":
                    start = _first_child(gfx, "start")
                    mid = _first_child(gfx, "mid")
                    end = _first_child(gfx, "end")
                    angle = _first_child(gfx, "angle")
                    if start and mid and end:
                        g_start = _local_to_global(_atom_float(start, 1), _atom_float(start, 2), fx, fy, frot)
                        g_mid = _local_to_global(_atom_float(mid, 1), _atom_float(mid, 2), fx, fy, frot)
                        g_end = _local_to_global(_atom_float(end, 1), _atom_float(end, 2), fx, fy, frot)
                        for ax, ay in _arc_bbox_points(g_start, g_mid, g_end):
                            bbox_xs.append(ax)
                            bbox_ys.append(ay)
                    elif start and end and angle:
                        g_center = _local_to_global(_atom_float(start, 1), _atom_float(start, 2), fx, fy, frot)
                        g_start = _local_to_global(_atom_float(end, 1), _atom_float(end, 2), fx, fy, frot)
                        for ax, ay in _legacy_arc_bbox_points(g_center, g_start, _atom_float(angle, 1)):
                            bbox_xs.append(ax)
                            bbox_ys.append(ay)
                elif gfx_tag == "fp_poly":
                    pts_node = _first_child(gfx, "pts")
                    if pts_node:
                        for xy in _child_nodes(pts_node, "xy"):
                            gx, gy = _local_to_global(_atom_float(xy, 1), _atom_float(xy, 2), fx, fy, frot)
                            bbox_xs.append(gx)
                            bbox_ys.append(gy)

        if bbox_xs and bbox_ys:
            min_x = min(bbox_xs)
            min_y = min(bbox_ys)
            max_x = max(bbox_xs)
            max_y = max(bbox_ys)
        else:
            min_x = max_x = fx
            min_y = max_y = fy

        est_h = _estimate_component_height_mm(
            reference,
            value,
            library_link,
            description,
            len(pad_nodes),
        )

        footprints.append(
            {
                "reference": reference,
                "value": value,
                "library_link": library_link,
                "description": description,
                "layer": fp_layer,
                "position": {
                    "x": round(fx, 4),
                    "y": round(fy, 4),
                    "rotation": round(frot, 4),
                },
                "pad_count": len(pad_nodes),
                "nets": sorted(fp_nets),
                "estimated_height_mm": round(est_h, 2),
                "bbox": {
                    "min_x": round(min_x, 4),
                    "min_y": round(min_y, 4),
                    "max_x": round(max_x, 4),
                    "max_y": round(max_y, 4),
                    "width_mm": round(max(0.0, max_x - min_x), 4),
                    "height_mm": round(max(0.0, max_y - min_y), 4),
                },
            }
        )

    return footprints, all_pads


def _extract_segments(tree: list[Any], net_map: dict[str, str]) -> list[dict[str, Any]]:
    """Extract all copper track `(segment ...)` records from the board AST.

    Args:
        tree: Root `kicad_pcb` S-expression list.
        net_map: Legacy net ID-to-name lookup map.

    Returns:
        List of normalized segment dictionaries.
    """
    segments: list[dict[str, Any]] = []
    for seg in _child_nodes(tree, "segment"):
        start = _first_child(seg, "start")
        end = _first_child(seg, "end")
        if start is None or end is None:
            continue
        x1, y1 = _atom_float(start, 1), _atom_float(start, 2)
        x2, y2 = _atom_float(end, 1), _atom_float(end, 2)
        width = _atom_float(_first_child(seg, "width"), 1)
        layer = _atom_str(_first_child(seg, "layer"), 1)
        net_name = _resolve_net_name(_first_child(seg, "net"), net_map)
        segments.append(
            {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "length_mm": math.hypot(x2 - x1, y2 - y1),
                "width": width,
                "layer": layer,
                "net": net_name,
            }
        )
    return segments


def _extract_vias(tree: list[Any], net_map: dict[str, str]) -> list[dict[str, Any]]:
    """Extract all `(via ...)` records from the board AST.

    Args:
        tree: Root `kicad_pcb` S-expression list.
        net_map: Legacy net ID-to-name lookup map.

    Returns:
        List of normalized via dictionaries.
    """
    vias: list[dict[str, Any]] = []
    for via in _child_nodes(tree, "via"):
        at_node = _first_child(via, "at")
        if at_node is None:
            continue
        vias.append(
            {
                "x": _atom_float(at_node, 1),
                "y": _atom_float(at_node, 2),
                "size": _atom_float(_first_child(via, "size"), 1),
                "drill": _atom_float(_first_child(via, "drill"), 1),
                "layers": _extract_node_layers(via),
                "net": _resolve_net_name(_first_child(via, "net"), net_map),
            }
        )
    return vias


def _extract_zones(tree: list[Any], net_map: dict[str, str]) -> list[dict[str, Any]]:
    """Extract all copper `(zone ...)` records and fill polygon statistics.

    Args:
        tree: Root `kicad_pcb` S-expression list.
        net_map: Legacy net ID-to-name lookup map.

    Returns:
        List of zone summary dictionaries.
    """
    zones: list[dict[str, Any]] = []
    for zone in _child_nodes(tree, "zone"):
        if _first_child(zone, "keepout") is not None:
            continue
        net_name = _resolve_net_name(
            _first_child(zone, "net"),
            net_map,
            _first_child(zone, "net_name"),
        )
        layers = _extract_node_layers(zone)
        filled_polygons = _child_nodes(zone, "filled_polygon")
        filled_point_count = 0
        for poly in filled_polygons:
            pts_node = _first_child(poly, "pts")
            if pts_node is not None:
                filled_point_count += len(_child_nodes(pts_node, "xy"))
        zones.append(
            {
                "net": net_name,
                "layer": ", ".join(layers) if len(layers) > 1 else (layers[0] if layers else ""),
                "layers": layers,
                "has_filled_polygon": len(filled_polygons) > 0,
                "filled_polygon_count": len(filled_polygons),
                "filled_point_count": filled_point_count,
            }
        )
    return zones


def _is_mounting_hole(fp: dict[str, Any]) -> bool:
    """Return whether a footprint represents a PCB mounting hole.

    Args:
        fp: Parsed footprint dictionary.

    Returns:
        `True` for mounting hole footprints (`MountingHole*` or `H<digits>`).
    """
    lib = fp.get("library_link", "").lower()
    val = fp.get("value", "").lower()
    ref = fp.get("reference", "")
    if "mountinghole" in lib or "mounting_hole" in lib or "mountinghole" in val:
        return True
    if re.match(r"^H\d*$", ref, re.IGNORECASE) and fp.get("pad_count", 0) <= 1:
        return True
    return False


def _extract_board_stackup(
    tree: list[Any],
    board_bbox: dict[str, float] | None,
    footprints: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract layer count, substrate thickness, outline shape, and max 3D height from the board.

    Args:
        tree: Root `kicad_pcb` S-expression AST.
        board_bbox: Computed outer board bounding box.
        footprints: Parsed footprints list with estimated 3D heights.

    Returns:
        Board stackup dictionary containing layer names, thickness, and profile metrics.
    """
    general_node = _first_child(tree, "general")
    thickness_node = _first_child(general_node, "thickness") if general_node else None
    board_thickness_mm = _atom_float(thickness_node, 1, 1.6)

    layers_node = _first_child(tree, "layers")
    copper_layers: list[str] = []
    if layers_node:
        for child in layers_node[1:]:
            if isinstance(child, list) and len(child) >= 3:
                layer_name = _atom_str(child, 2)
                if layer_name.endswith(".Cu"):
                    copper_layers.append(layer_name)

    edge_items = []
    for tag in ("gr_rect", "gr_line", "gr_poly", "gr_arc", "gr_circle"):
        for item in _child_nodes(tree, tag):
            if _atom_str(_first_child(item, "layer"), 1) == "Edge.Cuts":
                edge_items.append((tag, item))

    has_arcs = any(tag == "gr_arc" for tag, _ in edge_items)
    poly_or_rect_count = sum(1 for tag, _ in edge_items if tag in ("gr_rect", "gr_poly"))

    if has_arcs:
        outline_shape = "rounded_rectangular"
    elif poly_or_rect_count > 1 or len(edge_items) > 6:
        outline_shape = "custom_polygon_or_cutouts"
    else:
        outline_shape = "rectangular"

    internal_cutouts = max(0, poly_or_rect_count - 1)

    max_h = max((fp.get("estimated_height_mm", 0.0) for fp in footprints), default=0.0)

    return {
        "layer_count": len(copper_layers) if copper_layers else 2,
        "copper_layers": copper_layers if copper_layers else ["F.Cu", "B.Cu"],
        "board_thickness_mm": round(board_thickness_mm, 3),
        "outline_shape": outline_shape,
        "internal_cutouts_count": internal_cutouts,
        "max_estimated_component_height_mm": round(max_h, 2),
    }


def _audit_pcb_silkscreen(
    tree: list[Any],
    footprints: list[dict[str, Any]],
    all_pads: list[dict[str, Any]],
    vias: list[dict[str, Any]],
) -> dict[str, Any]:
    """Audit silkscreen text elements for font size, copper pad overlap, and connector pin labels.

    Args:
        tree: Root `kicad_pcb` S-expression AST.
        footprints: Parsed footprints list.
        all_pads: Parsed pads list with global coordinates.
        vias: Parsed vias list.

    Returns:
        Silkscreen audit summary dictionary.
    """
    func_count = 0
    std_count = 0
    for fp in footprints:
        ref = fp["reference"]
        if "_" in ref or any(ref.startswith(p) for p in ("U_", "J_", "SW_", "BTN_", "D_")):
            func_count += 1
        elif ref:
            std_count += 1

    if func_count > 0 and std_count > 0:
        if func_count / (func_count + std_count) >= 0.8:
            designator_style = "functional"
        elif std_count / (func_count + std_count) >= 0.99:
            designator_style = "standard"
        else:
            designator_style = "mixed"
    elif func_count > 0:
        designator_style = "functional"
    elif std_count > 0:
        designator_style = "standard"
    else:
        designator_style = "standard"

    silkscreen_texts: list[dict[str, Any]] = []

    for fp_node in _child_nodes(tree, "footprint"):
        at_node = _first_child(fp_node, "at")
        fx = _atom_float(at_node, 1)
        fy = _atom_float(at_node, 2)
        frot = _atom_float(at_node, 3, 0.0)

        for prop in _child_nodes(fp_node, "property"):
            hide = _atom_str(_first_child(prop, "hide"), 1)
            if hide == "yes":
                continue
            layer = _atom_str(_first_child(prop, "layer"), 1)
            if not ("SilkS" in layer):
                continue
            text_val = _atom_str(prop, 2)
            at_child = _first_child(prop, "at")
            px = _atom_float(at_child, 1)
            py = _atom_float(at_child, 2)
            gx, gy = _local_to_global(px, py, fx, fy, frot)
            eff = _first_child(prop, "effects")
            font = _first_child(eff, "font") if eff else None
            size_node = _first_child(font, "size") if font else None
            th_node = _first_child(font, "thickness") if font else None
            sx = _atom_float(size_node, 1, 1.0)
            sy = _atom_float(size_node, 2, 1.0)
            th = _atom_float(th_node, 1, 0.15)
            silkscreen_texts.append(
                {
                    "text": text_val,
                    "layer": layer,
                    "x": round(gx, 3),
                    "y": round(gy, 3),
                    "size_x": round(sx, 3),
                    "size_y": round(sy, 3),
                    "thickness": round(th, 3),
                }
            )

        for fp_text in _child_nodes(fp_node, "fp_text"):
            hide = _atom_str(_first_child(fp_text, "hide"), 1)
            if hide == "yes":
                continue
            layer = _atom_str(_first_child(fp_text, "layer"), 1)
            if not ("SilkS" in layer):
                continue
            text_val = _atom_str(fp_text, 2)
            at_child = _first_child(fp_text, "at")
            px = _atom_float(at_child, 1)
            py = _atom_float(at_child, 2)
            gx, gy = _local_to_global(px, py, fx, fy, frot)
            eff = _first_child(fp_text, "effects")
            font = _first_child(eff, "font") if eff else None
            size_node = _first_child(font, "size") if font else None
            th_node = _first_child(font, "thickness") if font else None
            sx = _atom_float(size_node, 1, 1.0)
            sy = _atom_float(size_node, 2, 1.0)
            th = _atom_float(th_node, 1, 0.15)
            silkscreen_texts.append(
                {
                    "text": text_val,
                    "layer": layer,
                    "x": round(gx, 3),
                    "y": round(gy, 3),
                    "size_x": round(sx, 3),
                    "size_y": round(sy, 3),
                    "thickness": round(th, 3),
                }
            )

    for gr_text in _child_nodes(tree, "gr_text"):
        layer = _atom_str(_first_child(gr_text, "layer"), 1)
        if not ("SilkS" in layer):
            continue
        text_val = _atom_str(gr_text, 1)
        at_child = _first_child(gr_text, "at")
        gx = _atom_float(at_child, 1)
        gy = _atom_float(at_child, 2)
        eff = _first_child(gr_text, "effects")
        font = _first_child(eff, "font") if eff else None
        size_node = _first_child(font, "size") if font else None
        th_node = _first_child(font, "thickness") if font else None
        sx = _atom_float(size_node, 1, 1.0)
        sy = _atom_float(size_node, 2, 1.0)
        th = _atom_float(th_node, 1, 0.15)
        silkscreen_texts.append(
            {
                "text": text_val,
                "layer": layer,
                "x": round(gx, 3),
                "y": round(gy, 3),
                "size_x": round(sx, 3),
                "size_y": round(sy, 3),
                "thickness": round(th, 3),
            }
        )

    min_h = min((t["size_y"] for t in silkscreen_texts), default=0.0)
    min_th = min((t["thickness"] for t in silkscreen_texts), default=0.0)

    undersized_texts = [
        t
        for t in silkscreen_texts
        if (t["size_y"] < 1.0 - 1e-4 or t["thickness"] < 0.15 - 1e-4)
    ]

    pad_overlap_warnings: list[dict[str, Any]] = []
    for txt in silkscreen_texts:
        tx, ty = txt["x"], txt["y"]
        is_front = "F.SilkS" in txt["layer"]
        copper_match = "F.Cu" if is_front else "B.Cu"

        for p in all_pads:
            if _layers_match(p["layers"], copper_match):
                hw = p["size_x"] * 0.5
                hh = p["size_y"] * 0.5
                if (p["x"] - hw) <= tx <= (p["x"] + hw) and (p["y"] - hh) <= ty <= (p["y"] + hh):
                    pad_overlap_warnings.append(
                        {
                            "text": txt["text"],
                            "layer": txt["layer"],
                            "position": {"x": tx, "y": ty},
                            "overlapping_pad": f"{p['reference']}.{p['pad_number']}",
                        }
                    )
                    break
        if len(pad_overlap_warnings) >= 50:
            break

    connector_pin_label_audit: list[dict[str, Any]] = []
    for fp in footprints:
        if not _is_connector(fp):
            continue
        cx, cy = fp["position"]["x"], fp["position"]["y"]
        labels_found: list[str] = []
        for txt in silkscreen_texts:
            d = math.hypot(txt["x"] - cx, txt["y"] - cy)
            if d <= 8.0:
                m_lbl = re.search(
                    r"^[+-]$|\b(?:GND|VCC|\+?5V|\+?3V3|\+?12V|BAT[T]?|SDA|SCL|TX|RX|LCD|KEY|IN\+?|OUT\+?|1)\b",
                    txt["text"],
                    re.IGNORECASE,
                )
                if m_lbl:
                    labels_found.append(txt["text"])

        connector_pin_label_audit.append(
            {
                "reference": fp["reference"],
                "value": fp["value"],
                "has_pin_labels": len(labels_found) > 0,
                "labels_found": sorted(set(labels_found)),
            }
        )

    return {
        "designator_style": designator_style,
        "total_silkscreen_texts": len(silkscreen_texts),
        "min_font_height_mm_found": round(min_h, 3),
        "min_font_thickness_mm_found": round(min_th, 3),
        "undersized_text_count": len(undersized_texts),
        "undersized_texts": undersized_texts[:10],
        "pad_overlap_warnings_count": len(pad_overlap_warnings),
        "pad_overlap_warnings": pad_overlap_warnings[:10],
        "missing_connector_pin_labels": [
            c["reference"] for c in connector_pin_label_audit if not c["has_pin_labels"]
        ],
    }


def inspect_pcb_layout(pcb_path: str, compact: bool = True) -> dict[str, Any]:
    """Inspect a `.kicad_pcb` layout for board bounds, stackup, footprints, traces, vias, zones, silkscreen, and locks.

    Args:
        pcb_path: Path to the target `.kicad_pcb` file.
        compact: When True (default), compress each footprint entry to essential placement/dimension keys.

    Returns:
        Structured dictionary containing `board_bbox`, `board_stackup`, `silkscreen_audit`,
        `footprints`, traces, vias, zones, and KiCad GUI locks.
    """
    resolved, tree = _parse_pcb_file(pcb_path)
    lock_info = check_kicad_lock_files(str(resolved))
    net_map = _extract_net_map(tree)

    board_bbox = _extract_board_bbox(tree)
    footprints, all_pads = _parse_footprints_and_pads(tree, net_map)
    segments = _extract_segments(tree, net_map)
    vias = _extract_vias(tree, net_map)
    zones = _extract_zones(tree, net_map)

    board_stackup = _extract_board_stackup(tree, board_bbox, footprints)
    silkscreen_audit = _audit_pcb_silkscreen(tree, footprints, all_pads, vias)

    non_mh_fps = [fp for fp in footprints if not _is_mounting_hole(fp)]
    occupied_area = sum(
        fp["bbox"]["width_mm"] * fp["bbox"]["height_mm"] for fp in non_mh_fps
    )
    board_area = board_bbox["area_mm2"] if board_bbox else 0.0
    occupancy_ratio = round(occupied_area / board_area, 4) if board_area > 0.0 else 0.0
    free_area = round(max(0.0, board_area - occupied_area), 4) if board_area > 0.0 else 0.0

    output_footprints = (
        [
            {
                "ref": fp["reference"],
                "val": fp["value"],
                "layer": fp["layer"],
                "pos": [
                    fp["position"]["x"],
                    fp["position"]["y"],
                    fp["position"]["rotation"],
                ],
                "size": [fp["bbox"]["width_mm"], fp["bbox"]["height_mm"]],
                "h_mm": fp["estimated_height_mm"],
            }
            for fp in footprints
        ]
        if compact
        else footprints
    )

    return {
        "status": "ok",
        "compact": compact,
        "pcb_path": str(resolved),
        "version": _atom_str(_first_child(tree, "version"), 1),
        "generator": _atom_str(_first_child(tree, "generator"), 1),
        "generator_version": _atom_str(_first_child(tree, "generator_version"), 1),
        "board_bbox": board_bbox,
        "board_stackup": board_stackup,
        "silkscreen_audit": silkscreen_audit,
        "footprint_count": len(footprints),
        "footprints": output_footprints,
        "segments": {
            "count": len(segments),
            "total_length_mm": round(sum(s["length_mm"] for s in segments), 4),
            "widths": sorted({round(s["width"], 4) for s in segments}),
            "layers_used": sorted({s["layer"] for s in segments if s["layer"]}),
        },
        "vias": {
            "count": len(vias),
            "drills": sorted({round(v["drill"], 4) for v in vias}),
            "sizes": sorted({round(v["size"], 4) for v in vias}),
        },
        "zone_count": len(zones),
        "zones": zones,
        "occupancy": {
            "non_mounting_hole_footprint_count": len(non_mh_fps),
            "occupied_area_mm2": round(occupied_area, 4),
            "board_area_mm2": round(board_area, 4),
            "rough_occupancy_ratio": occupancy_ratio,
            "free_area_estimate_mm2": free_area,
        },
        "locked": lock_info["locked"],
        "target_locked": lock_info["target_locked"],
        "lock_files": lock_info["lock_files"],
        "lock_warning": lock_info["warning"],
    }


def _via_connects_to_segment(
    via: dict[str, Any],
    segments_by_net: dict[str, list[dict[str, Any]]],
    tolerance_mm: float,
) -> bool:
    """Determine whether a via touches at least one copper segment on the same net.

    Args:
        via: Parsed via dictionary.
        segments_by_net: Mapping of net names to lists of segment dictionaries.
        tolerance_mm: Extra geometric tolerance in mm.

    Returns:
        `True` when a same-net segment on a matching layer intersects the via pad.
    """
    via_radius = via["size"] * 0.5 + tolerance_mm
    via_layers = via["layers"]
    for seg in segments_by_net.get(via["net"], ()):
        if _layers_match(via_layers, seg["layer"]):
            dist = _point_to_segment_distance(
                via["x"], via["y"], seg["x1"], seg["y1"], seg["x2"], seg["y2"]
            )
            if dist <= via_radius:
                return True
    return False


def _is_gnd_net(net_name: str) -> bool:
    """Return whether a net name represents a ground reference net.

    Args:
        net_name: Net name string.

    Returns:
        `True` for `GND`, `*GND`, or `VSS` nets.
    """
    upper = net_name.strip().upper()
    return upper == "GND" or upper.endswith("GND") or upper == "VSS"


def _is_critical_gnd_component(reference: str) -> bool:
    """Return whether a component designator is critical for explicit GND routing.

    Args:
        reference: Component reference designator (e.g. `C4`, `R1`, `Y1`, `U3`).

    Returns:
        `True` for capacitors (`C*`), resistors (`R*`), crystals (`Y*`), and ICs (`U*`).
    """
    return bool(re.match(r"^[CRYU]", reference.strip(), re.IGNORECASE))


def find_unrouted_pads(
    pcb_path: str,
    net_filter: str | None = None,
    tolerance_mm: float = 0.05,
) -> dict[str, Any]:
    """Identify multi-pad net pads lacking explicit copper track connections.

    Classifies un-tracked pads into `zone_only_pads` (covered only by a copper
    zone pour on a matching layer) and `completely_unrouted_pads` (no segment or
    zone), highlighting critical `C*`/`R*`/`Y*`/`U*` GND pads missing explicit tracks.

    Args:
        pcb_path: Path to the target `.kicad_pcb` file.
        net_filter: Optional case-insensitive exact or substring net filter.
        tolerance_mm: Geometric distance tolerance in mm added to pad radii.

    Returns:
        Audit dictionary with `passed`, `counts`, `zone_only_pads`,
        `completely_unrouted_pads`, `critical_gnd_pads`, and lock metadata.
    """
    resolved, tree = _parse_pcb_file(pcb_path)
    lock_info = check_kicad_lock_files(str(resolved))
    net_map = _extract_net_map(tree)

    _, all_pads = _parse_footprints_and_pads(tree, net_map)
    segments = _extract_segments(tree, net_map)
    vias = _extract_vias(tree, net_map)
    zones = _extract_zones(tree, net_map)

    net_pad_counts = Counter(
        pad["net"]
        for pad in all_pads
        if pad["net"] and not pad["net"].startswith("unconnected-")
    )

    segments_by_net: dict[str, list[dict[str, Any]]] = {}
    for seg in segments:
        if seg["net"]:
            segments_by_net.setdefault(seg["net"], []).append(seg)

    vias_by_net: dict[str, list[dict[str, Any]]] = {}
    for via in vias:
        if via["net"]:
            vias_by_net.setdefault(via["net"], []).append(via)

    zones_by_net: dict[str, list[dict[str, Any]]] = {}
    for zone in zones:
        if zone["net"]:
            zones_by_net.setdefault(zone["net"], []).append(zone)

    filter_needle = net_filter.strip().lower() if net_filter and net_filter.strip() else None

    checked_pads = 0
    ignored_pads = 0
    routed_by_segment = 0
    checked_nets: set[str] = set()
    zone_only_pads: list[dict[str, Any]] = []
    completely_unrouted_pads: list[dict[str, Any]] = []
    critical_gnd_pads: list[dict[str, Any]] = []

    for pad in all_pads:
        net = pad["net"]
        if not net or net.startswith("unconnected-") or net_pad_counts[net] <= 1:
            ignored_pads += 1
            continue

        if filter_needle is not None and filter_needle not in net.lower():
            continue

        checked_pads += 1
        checked_nets.add(net)
        px, py = pad["x"], pad["y"]
        pad_layers = pad["layers"]
        conn_radius = max(min(pad["size_x"], pad["size_y"]) * 0.5, 0.25) + tolerance_mm

        has_segment = False
        for seg in segments_by_net.get(net, ()):
            if _layers_match(pad_layers, seg["layer"]):
                if _point_to_segment_distance(px, py, seg["x1"], seg["y1"], seg["x2"], seg["y2"]) <= conn_radius:
                    has_segment = True
                    break

        if not has_segment:
            for via in vias_by_net.get(net, ()):
                via_touches_pad_layer = any(
                    _layers_match(pad_layers, v_layer)
                    or (v_layer == "*.Cu" and any(_layers_match(pad_layers, c) for c in ("F.Cu", "B.Cu")))
                    for v_layer in via["layers"]
                )
                if via_touches_pad_layer and math.hypot(px - via["x"], py - via["y"]) <= conn_radius:
                    if _via_connects_to_segment(via, segments_by_net, tolerance_mm):
                        has_segment = True
                        break

        if has_segment:
            routed_by_segment += 1
            continue

        matching_zones = [
            zone
            for zone in zones_by_net.get(net, ())
            if any(_layers_match(pad_layers, z_layer) for z_layer in zone["layers"])
        ]

        entry = {
            "reference": pad["reference"],
            "pad_number": pad["pad_number"],
            "pad_type": pad["pad_type"],
            "net": net,
            "x": pad["x"],
            "y": pad["y"],
            "size_x": pad["size_x"],
            "size_y": pad["size_y"],
            "layers": pad_layers,
            "conn_radius_mm": round(conn_radius, 4),
        }

        if matching_zones:
            entry["zone_layers"] = sorted({lyr for z in matching_zones for lyr in z["layers"]})
            entry["zone_filled"] = any(z["has_filled_polygon"] for z in matching_zones)
            entry["classification"] = "zone_only"
            zone_only_pads.append(entry)
        else:
            entry["classification"] = "completely_unrouted"
            completely_unrouted_pads.append(entry)

        if _is_gnd_net(net) and _is_critical_gnd_component(pad["reference"]):
            critical_gnd_pads.append(
                {
                    **entry,
                    "reason": "Critical component GND pad lacks an explicit copper track/segment connection.",
                }
            )

    unrouted_summary_by_net: dict[str, dict[str, int]] = defaultdict(
        lambda: {"zone_only": 0, "completely_unrouted": 0}
    )
    for item in zone_only_pads:
        unrouted_summary_by_net[item["net"]]["zone_only"] += 1
    for item in completely_unrouted_pads:
        unrouted_summary_by_net[item["net"]]["completely_unrouted"] += 1

    critical_gnd_warning = (
        f"{len(critical_gnd_pads)} critical GND pad(s) on C*/R*/Y*/U* lack explicit copper tracks."
        if critical_gnd_pads
        else None
    )

    return {
        "status": "ok",
        "pcb_path": str(resolved),
        "net_filter": net_filter,
        "tolerance_mm": tolerance_mm,
        "passed": len(zone_only_pads) == 0 and len(completely_unrouted_pads) == 0,
        "counts": {
            "total_pads": len(all_pads),
            "checked_pads": checked_pads,
            "ignored_pads": ignored_pads,
            "checked_multi_pad_nets": len(checked_nets),
            "routed_by_segment_count": routed_by_segment,
            "zone_only_count": len(zone_only_pads),
            "completely_unrouted_count": len(completely_unrouted_pads),
            "critical_gnd_missing_tracks_count": len(critical_gnd_pads),
        },
        "unrouted_summary_by_net": {
            net: counts for net, counts in sorted(unrouted_summary_by_net.items())
        },
        "zone_only_pads": zone_only_pads[:20],
        "completely_unrouted_pads": completely_unrouted_pads[:25],
        "critical_gnd_pads": critical_gnd_pads[:20],
        "critical_gnd_warning": critical_gnd_warning,
        "locked": lock_info["locked"],
        "target_locked": lock_info["target_locked"],
        "lock_files": lock_info["lock_files"],
        "lock_warning": lock_info["warning"],
    }


def _bbox_clearance(bbox_a: dict[str, float], bbox_b: dict[str, float]) -> tuple[bool, float]:
    """Compute signed clearance between two axis-aligned bounding boxes.

    Args:
        bbox_a: First bounding box dict with `min_x`, `min_y`, `max_x`, `max_y`.
        bbox_b: Second bounding box dict with `min_x`, `min_y`, `max_x`, `max_y`.

    Returns:
        Tuple `(overlapping, clearance_mm)` where negative clearance indicates
        overlap penetration depth.
    """
    dx = max(0.0, max(bbox_a["min_x"] - bbox_b["max_x"], bbox_b["min_x"] - bbox_a["max_x"]))
    dy = max(0.0, max(bbox_a["min_y"] - bbox_b["max_y"], bbox_b["min_y"] - bbox_a["max_y"]))
    if dx > 0.0 or dy > 0.0:
        return False, math.hypot(dx, dy)

    overlap_x = min(bbox_a["max_x"], bbox_b["max_x"]) - max(bbox_a["min_x"], bbox_b["min_x"])
    overlap_y = min(bbox_a["max_y"], bbox_b["max_y"]) - max(bbox_a["min_y"], bbox_b["min_y"])
    return True, -min(overlap_x, overlap_y)


def _is_module_footprint(fp: dict[str, Any]) -> bool:
    """Identify daughter-board or multi-part hardware module footprints.

    Args:
        fp: Parsed footprint dictionary.

    Returns:
        `True` if the footprint appears to be an integrated hardware module.
    """
    combined = " ".join(
        [
            fp.get("reference", ""),
            fp.get("value", ""),
            fp.get("library_link", ""),
            fp.get("description", ""),
        ]
    ).lower()
    if any(tok in combined for tok in ("module", "tp4056", "mt3608", "mini360", "mini560", "esp", "buck", "boost", "charger")):
        return True
    bbox = fp.get("bbox", {})
    return bbox.get("width_mm", 0.0) >= 20.0 or bbox.get("height_mm", 0.0) >= 20.0


def _is_expected_overhang_module(fp: dict[str, Any]) -> tuple[bool, str]:
    """Determine whether a board-edge overhang is intentional (e.g. USB Type-C / TP4056).

    Args:
        fp: Parsed footprint dictionary.

    Returns:
        Tuple `(is_expected, reason)` describing why the overhang is expected or not.
    """
    combined = " ".join(
        [
            fp.get("reference", ""),
            fp.get("value", ""),
            fp.get("library_link", ""),
            fp.get("description", ""),
        ]
    ).lower()
    expected_keywords = (
        ("tp4056", "Charger module with edge-mounted USB connector"),
        ("usb", "Edge-accessible USB receptacle overhang"),
        ("type-c", "Edge-accessible USB Type-C overhang"),
        ("type_c", "Edge-accessible USB Type-C overhang"),
        ("overhang", "Footprint metadata explicitly notes an edge overhang keepout"),
        ("barrel_jack", "Edge-mounted power jack overhang"),
        ("dc_jack", "Edge-mounted DC jack overhang"),
        ("sdcard", "Edge-accessible SD card slot overhang"),
        ("microsd", "Edge-accessible microSD card slot overhang"),
        ("rj45", "Edge-mounted Ethernet jack overhang"),
        ("hdmi", "Edge-mounted HDMI connector overhang"),
    )
    for keyword, reason in expected_keywords:
        if keyword in combined:
            return True, reason
    return False, "Footprint extends beyond Edge.Cuts without an expected edge-connector/module profile"


def _is_connector(fp: dict[str, Any]) -> bool:
    """Identify connector footprints by reference prefix or library link.

    Args:
        fp: Parsed footprint dictionary.

    Returns:
        `True` if the footprint is a connector (`J*` or `Connector`/`JST` library).
    """
    ref = fp.get("reference", "").strip().upper()
    lib = fp.get("library_link", "").lower()
    return ref.startswith("J") or "connector" in lib or "jst" in lib


def _is_display_module(fp: dict[str, Any]) -> bool:
    """Identify external or on-board display headers and display modules.

    Args:
        fp: Parsed footprint dictionary.

    Returns:
        `True` for `J_LCD*`, `LCD*`, `1602`, `2004`, `OLED`, `TFT`, or `DISPLAY` footprints.
    """
    combined = " ".join(
        [
            fp.get("reference", ""),
            fp.get("value", ""),
            fp.get("library_link", ""),
            fp.get("description", ""),
        ]
    ).lower()
    return any(tok in combined for tok in ("j_lcd", "lcd", "1602", "2004", "oled", "tft", "display"))


def check_mechanical_fit(
    pcb_path: str,
    min_clearance_mm: float = 0.5,
    connector_clearance_mm: float = 2.0,
    standoff_keepout_dia_mm: float = 6.5,
    standoff_height_mm: float = 10.0,
    mezzanine_modules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit PCB mechanical fit: courtyard collisions, edge overhangs, connector mating, and stacked mezzanine clearances.

    Args:
        pcb_path: Path to the target `.kicad_pcb` file.
        min_clearance_mm: Minimum required courtyard/body gap in mm between parts.
        connector_clearance_mm: Minimum clearance in mm around connectors for mating/wiring.
        standoff_keepout_dia_mm: Standoff screw-head keepout diameter around mounting holes (e.g. 6.5 mm for M3).
        standoff_height_mm: Default vertical standoff height in mm for mezzanine modules.
        mezzanine_modules: Optional list of mezzanine module definitions (`name`, `min_x`, `min_y`, `max_x`, `max_y`).

    Returns:
        Dictionary containing results for Check A (`courtyard_collisions`), Check B
        (`board_edge_overhangs`), Check C (`connector_clearances`), Check D
        (`display_and_mounting_audit`), Check E (`mezzanine_and_standoff_audit`),
        and KiCad lock metadata.
    """
    resolved, tree = _parse_pcb_file(pcb_path)
    layout = inspect_pcb_layout(str(resolved), compact=False)
    board_bbox = layout["board_bbox"]
    footprints = layout["footprints"]
    net_map = _extract_net_map(tree)
    segments = _extract_segments(tree, net_map)

    eps = 1e-6
    courtyard_collisions: list[dict[str, Any]] = []
    for i in range(len(footprints)):
        fp_a = footprints[i]
        mh_a = _is_mounting_hole(fp_a)
        for j in range(i + 1, len(footprints)):
            fp_b = footprints[j]
            if fp_a["layer"] != fp_b["layer"]:
                continue
            if mh_a and _is_mounting_hole(fp_b):
                continue

            overlapping, clearance = _bbox_clearance(fp_a["bbox"], fp_b["bbox"])
            if clearance < min_clearance_mm - eps:
                module_involved = _is_module_footprint(fp_a) or _is_module_footprint(fp_b)
                if overlapping:
                    note = (
                        "Module-level courtyard/body overlap detected; verify physical component height and outline."
                        if module_involved
                        else "Courtyard/body bounding boxes overlap on the same copper side."
                    )
                else:
                    note = (
                        "Module-level proximity is tighter than min_clearance_mm."
                        if module_involved
                        else "Component clearance is tighter than min_clearance_mm."
                    )
                courtyard_collisions.append(
                    {
                        "ref_a": fp_a["reference"],
                        "ref_b": fp_b["reference"],
                        "value_a": fp_a["value"],
                        "value_b": fp_b["value"],
                        "layer": fp_a["layer"],
                        "overlapping": overlapping,
                        "clearance_mm": round(clearance, 4),
                        "severity": "collision" if overlapping else "low_clearance",
                        "module_involved": module_involved,
                        "note": note,
                    }
                )

    courtyard_collisions.sort(key=lambda item: item["clearance_mm"])
    physical_collision_count = sum(1 for item in courtyard_collisions if item["overlapping"])
    low_clearance_count = len(courtyard_collisions) - physical_collision_count

    expected_overhangs: list[dict[str, Any]] = []
    unintended_overhangs: list[dict[str, Any]] = []
    if board_bbox is not None:
        for fp in footprints:
            fp_box = fp["bbox"]
            over_left = max(0.0, board_bbox["min_x"] - fp_box["min_x"])
            over_right = max(0.0, fp_box["max_x"] - board_bbox["max_x"])
            over_top = max(0.0, board_bbox["min_y"] - fp_box["min_y"])
            over_bottom = max(0.0, fp_box["max_y"] - board_bbox["max_y"])
            max_overhang = max(over_left, over_right, over_top, over_bottom)
            if max_overhang > 1e-4:
                is_expected, reason = _is_expected_overhang_module(fp)
                entry = {
                    "reference": fp["reference"],
                    "value": fp["value"],
                    "library_link": fp["library_link"],
                    "layer": fp["layer"],
                    "expected": is_expected,
                    "classification": "expected_edge_module" if is_expected else "unintended_overhang",
                    "reason": reason,
                    "max_overhang_mm": round(max_overhang, 4),
                    "overhang_mm": {
                        "left": round(over_left, 4),
                        "right": round(over_right, 4),
                        "top": round(over_top, 4),
                        "bottom": round(over_bottom, 4),
                    },
                    "bbox": fp_box,
                }
                if is_expected:
                    expected_overhangs.append(entry)
                else:
                    unintended_overhangs.append(entry)

    connector_clearances: list[dict[str, Any]] = []
    for idx, fp in enumerate(footprints):
        if not _is_connector(fp):
            continue
        obstructing_parts: list[dict[str, Any]] = []
        min_found: float | None = None
        for other_idx, other in enumerate(footprints):
            if idx == other_idx or other["layer"] != fp["layer"]:
                continue
            overlapping, clearance = _bbox_clearance(fp["bbox"], other["bbox"])
            if min_found is None or clearance < min_found:
                min_found = clearance
            if clearance < connector_clearance_mm - eps:
                obstructing_parts.append(
                    {
                        "reference": other["reference"],
                        "value": other["value"],
                        "clearance_mm": round(clearance, 4),
                        "overlapping": overlapping,
                    }
                )
        obstructing_parts.sort(key=lambda item: item["clearance_mm"])
        connector_clearances.append(
            {
                "reference": fp["reference"],
                "value": fp["value"],
                "library_link": fp["library_link"],
                "layer": fp["layer"],
                "bbox": fp["bbox"],
                "min_found_clearance_mm": round(min_found, 4) if min_found is not None else None,
                "obstructing_parts": obstructing_parts,
                "passed": len(obstructing_parts) == 0,
            }
        )

    mounting_holes = [
        {
            "reference": fp["reference"],
            "value": fp["value"],
            "library_link": fp["library_link"],
            "position": fp["position"],
            "bbox": fp["bbox"],
        }
        for fp in footprints
        if _is_mounting_hole(fp)
    ]

    display_modules = [
        {
            "reference": fp["reference"],
            "value": fp["value"],
            "library_link": fp["library_link"],
            "position": fp["position"],
            "bbox": fp["bbox"],
            "is_cable_header": _is_connector(fp),
        }
        for fp in footprints
        if _is_display_module(fp)
    ]

    module_footprints = [
        fp["reference"] for fp in footprints if _is_module_footprint(fp)
    ]

    standoff_keepouts: list[dict[str, Any]] = []
    for mh in mounting_holes:
        hx = mh["position"]["x"]
        hy = mh["position"]["y"]
        keepout_rad = max(standoff_keepout_dia_mm * 0.5, mh["bbox"]["width_mm"] * 0.5)

        encroaching_fps: list[dict[str, Any]] = []
        for fp in footprints:
            if fp["reference"] == mh["reference"] or _is_mounting_hole(fp):
                continue
            box = fp["bbox"]
            dx = max(0.0, max(box["min_x"] - hx, hx - box["max_x"]))
            dy = max(0.0, max(box["min_y"] - hy, hy - box["max_y"]))
            dist = math.hypot(dx, dy)
            if dist < keepout_rad - eps:
                encroaching_fps.append(
                    {
                        "reference": fp["reference"],
                        "value": fp["value"],
                        "clearance_to_hole_center_mm": round(dist, 3),
                    }
                )

        encroaching_traces: list[dict[str, Any]] = []
        for seg in segments:
            if _is_gnd_net(seg["net"]):
                continue
            dist = _point_to_segment_distance(hx, hy, seg["x1"], seg["y1"], seg["x2"], seg["y2"])
            if dist < keepout_rad - eps:
                encroaching_traces.append(
                    {
                        "net": seg["net"],
                        "layer": seg["layer"],
                        "distance_mm": round(dist, 3),
                    }
                )

        standoff_keepouts.append(
            {
                "hole_reference": mh["reference"],
                "position": {"x": hx, "y": hy},
                "keepout_radius_mm": round(keepout_rad, 3),
                "keepout_diameter_mm": round(keepout_rad * 2.0, 3),
                "encroaching_components": encroaching_fps,
                "encroaching_non_gnd_traces": encroaching_traces,
                "passed": len(encroaching_fps) == 0 and len(encroaching_traces) == 0,
            }
        )

    mezzanine_eval_list: list[dict[str, Any]] = []
    if mezzanine_modules:
        for m in mezzanine_modules:
            bbox = m.get("bbox", m) if isinstance(m.get("bbox"), dict) else m
            mezzanine_eval_list.append(
                {
                    "name": m.get("name", "Custom_Mezzanine"),
                    "min_x": float(bbox.get("min_x", m.get("min_x", 0.0))),
                    "min_y": float(bbox.get("min_y", m.get("min_y", 0.0))),
                    "max_x": float(bbox.get("max_x", m.get("max_x", 0.0))),
                    "max_y": float(bbox.get("max_y", m.get("max_y", 0.0))),
                    "standoff_height_mm": float(m.get("standoff_height_mm", standoff_height_mm)),
                    "is_candidate_projection": False,
                }
            )
    else:
        for d in display_modules:
            dx = d["position"]["x"]
            dy = d["position"]["y"]
            if board_bbox:
                box_w = 80.0
                box_h = 36.0
                min_x = max(board_bbox["min_x"], dx - 10.0)
                max_x = min(board_bbox["max_x"], min_x + box_w)
                min_y = max(board_bbox["min_y"], dy - 5.0)
                max_y = min(board_bbox["max_y"], min_y + box_h)
                mezzanine_eval_list.append(
                    {
                        "name": f"Candidate_Mezzanine_For_{d['reference']}",
                        "min_x": round(min_x, 2),
                        "min_y": round(min_y, 2),
                        "max_x": round(max_x, 2),
                        "max_y": round(max_y, 2),
                        "standoff_height_mm": float(standoff_height_mm),
                        "is_candidate_projection": True,
                    }
                )

    mezzanine_z_clearance: list[dict[str, Any]] = []
    for mez in mezzanine_eval_list:
        m_box = {
            "min_x": mez["min_x"],
            "min_y": mez["min_y"],
            "max_x": mez["max_x"],
            "max_y": mez["max_y"],
        }
        z_limit = mez["standoff_height_mm"] - 1.5

        colliding_parts: list[dict[str, Any]] = []
        tactile_switch_colliding: list[str] = []
        max_h_found = 0.0

        for fp in footprints:
            overlapping, _ = _bbox_clearance(m_box, fp["bbox"])
            if overlapping:
                h = fp.get("estimated_height_mm", 0.0)
                if h > max_h_found:
                    max_h_found = h
                if h > z_limit:
                    colliding_parts.append(
                        {
                            "reference": fp["reference"],
                            "value": fp["value"],
                            "estimated_height_mm": h,
                            "z_clearance_deficit_mm": round(h - z_limit, 2),
                        }
                    )
                if fp["reference"].startswith("SW"):
                    tactile_switch_colliding.append(fp["reference"])

        mezzanine_z_clearance.append(
            {
                "module_name": mez["name"],
                "projection_box": m_box,
                "standoff_height_mm": mez["standoff_height_mm"],
                "is_candidate_projection": mez["is_candidate_projection"],
                "max_underlying_height_mm": round(max_h_found, 2),
                "minimum_required_standoff_height_mm": round(max_h_found + 1.5, 2),
                "z_conflicts": colliding_parts,
                "tactile_switches_under_mezzanine": tactile_switch_colliding,
                "passed": len(colliding_parts) == 0 and len(tactile_switch_colliding) == 0,
            }
        )

    rigid_headers: list[dict[str, Any]] = []
    for fp in footprints:
        lib = fp["library_link"].lower()
        val = fp["value"].lower()
        if any(tok in lib or tok in val for tok in ("pinheader", "femaleheader", "socket_strip")):
            offsets: list[dict[str, Any]] = []
            fx, fy = fp["position"]["x"], fp["position"]["y"]
            for mh in mounting_holes:
                hx, hy = mh["position"]["x"], mh["position"]["y"]
                offsets.append(
                    {
                        "hole_reference": mh["reference"],
                        "dx_mm": round(hx - fx, 3),
                        "dy_mm": round(hy - fy, 3),
                        "distance_mm": round(math.hypot(hx - fx, hy - fy), 3),
                    }
                )
            rigid_headers.append(
                {
                    "reference": fp["reference"],
                    "value": fp["value"],
                    "position": fp["position"],
                    "offsets_to_mounting_holes": offsets,
                }
            )

    external_module_mounting_notes: list[str] = []
    if not mounting_holes:
        external_module_mounting_notes.append(
            "No PCB mounting holes (H* / MountingHole*) found on the board; verify how the PCB attaches to the enclosure."
        )
    else:
        hole_refs = ", ".join(h["reference"] for h in mounting_holes)
        external_module_mounting_notes.append(
            f"PCB mounting holes present ({len(mounting_holes)}: {hole_refs}); verify enclosure standoff spacing and screw head clearance."
        )

    if display_modules:
        disp_desc = ", ".join(f"{d['reference']} ({d['value']})" for d in display_modules)
        has_dedicated_display_holes = any(not d["is_cable_header"] for d in display_modules) and len(mounting_holes) > 4
        if all(d["is_cable_header"] for d in display_modules):
            external_module_mounting_notes.append(
                f"External display header(s) detected ({disp_desc}) with no dedicated display PCB mounting holes: confirm whether the display mounts to the front panel/enclosure via cable harness and verify connector + wire-bend Z-height clearance."
            )
        elif not has_dedicated_display_holes:
            external_module_mounting_notes.append(
                f"Display module(s) detected ({disp_desc}): verify dedicated PCB mounting holes vs enclosure/panel mounting and 3D height above adjacent components."
            )
        else:
            external_module_mounting_notes.append(
                f"Display module(s) detected ({disp_desc}): verify 3D Z-height and standoff height relative to adjacent tall components."
            )

    if module_footprints:
        external_module_mounting_notes.append(
            f"Daughter/external hardware modules detected ({', '.join(module_footprints)}): verify 3D Z-height (e.g. boost inductor/potentiometer, USB-C plug clearance, pin header stack height) inside enclosure."
        )

    standoff_failed = any(not s["passed"] for s in standoff_keepouts)
    explicit_mezzanine_failed = any(
        not m["passed"] for m in mezzanine_z_clearance if not m["is_candidate_projection"]
    )

    edge_interface_audit: list[dict[str, Any]] = []
    if board_bbox is not None:
        _, all_pads = _parse_footprints_and_pads(tree, net_map)
        pads_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for pad in all_pads:
            pads_by_ref[pad["reference"]].append(pad)

        edge_iface_pattern = re.compile(
            r"(?:rj11|rj45|usb|dc-?005|barrel|wh148|potentiometer|pot_|kcd|audio|hdmi|microsd|sd_card)",
            re.IGNORECASE,
        )
        for fp in footprints:
            ref = fp["reference"]
            meta_blob = f"{ref} {fp['value']} {fp['library_link']}"
            is_edge_iface = bool(edge_iface_pattern.search(meta_blob)) or bool(
                re.match(r"^RV\d+", ref, re.IGNORECASE)
            )
            if not is_edge_iface:
                continue

            box = fp["bbox"]
            edge_dists = {
                "left": box["min_x"] - board_bbox["min_x"],
                "right": board_bbox["max_x"] - box["max_x"],
                "top": box["min_y"] - board_bbox["min_y"],
                "bottom": board_bbox["max_y"] - box["max_y"],
            }
            nearest_edge, dist_to_edge = min(edge_dists.items(), key=lambda kv: kv[1])

            if nearest_edge == "left":
                corridor = {
                    "min_x": board_bbox["min_x"],
                    "max_x": box["min_x"],
                    "min_y": box["min_y"],
                    "max_y": box["max_y"],
                }
                outward_vec = (-1.0, 0.0)
            elif nearest_edge == "right":
                corridor = {
                    "min_x": box["max_x"],
                    "max_x": board_bbox["max_x"],
                    "min_y": box["min_y"],
                    "max_y": box["max_y"],
                }
                outward_vec = (1.0, 0.0)
            elif nearest_edge == "top":
                corridor = {
                    "min_x": box["min_x"],
                    "max_x": box["max_x"],
                    "min_y": board_bbox["min_y"],
                    "max_y": box["min_y"],
                }
                outward_vec = (0.0, -1.0)
            else:
                corridor = {
                    "min_x": box["min_x"],
                    "max_x": box["max_x"],
                    "min_y": box["max_y"],
                    "max_y": board_bbox["max_y"],
                }
                outward_vec = (0.0, 1.0)

            blocking_parts: list[str] = []
            if dist_to_edge > 0.2:
                for other in footprints:
                    if other["reference"] == ref or other["layer"] != fp["layer"] or _is_mounting_hole(other):
                        continue
                    overlapping, _ = _bbox_clearance(corridor, other["bbox"])
                    if overlapping:
                        blocking_parts.append(other["reference"])

            facing_inward = False
            face_vec_norm: tuple[float, float] | None = None
            fp_pads = pads_by_ref.get(ref, [])
            if fp_pads:
                px_avg = sum(p["x"] for p in fp_pads) / len(fp_pads)
                py_avg = sum(p["y"] for p in fp_pads) / len(fp_pads)
                bx_mid = (box["min_x"] + box["max_x"]) * 0.5
                by_mid = (box["min_y"] + box["max_y"]) * 0.5
                vx, vy = bx_mid - px_avg, by_mid - py_avg
                vlen = math.hypot(vx, vy)
                if vlen >= 0.5:
                    nx, ny = vx / vlen, vy / vlen
                    face_vec_norm = (round(nx, 3), round(ny, 3))
                    dot = nx * outward_vec[0] + ny * outward_vec[1]
                    if dot < -0.25:
                        facing_inward = True

            too_far = dist_to_edge > 5.0
            passed_item = (len(blocking_parts) == 0) and (not facing_inward) and (not too_far)
            edge_interface_audit.append(
                {
                    "reference": ref,
                    "value": fp["value"],
                    "nearest_edge": nearest_edge,
                    "distance_to_edge_mm": round(dist_to_edge, 3),
                    "too_far_from_board_edge": too_far,
                    "edge_access_blocked_by": blocking_parts,
                    "facing_inward": facing_inward,
                    "face_vector": face_vec_norm,
                    "passed": passed_item,
                }
            )

    edge_interface_failed = any(not item["passed"] for item in edge_interface_audit)

    return {
        "status": "ok",
        "pcb_path": layout["pcb_path"],
        "min_clearance_mm": min_clearance_mm,
        "connector_clearance_mm": connector_clearance_mm,
        "standoff_keepout_dia_mm": standoff_keepout_dia_mm,
        "standoff_height_mm": standoff_height_mm,
        "passed": (
            physical_collision_count == 0
            and len(unintended_overhangs) == 0
            and not standoff_failed
            and not explicit_mezzanine_failed
            and not edge_interface_failed
        ),
        "summary": {
            "physical_collision_count": physical_collision_count,
            "low_clearance_count": low_clearance_count,
            "expected_overhang_count": len(expected_overhangs),
            "unintended_overhang_count": len(unintended_overhangs),
            "connector_count": len(connector_clearances),
            "connectors_with_tight_clearance_count": sum(
                1 for item in connector_clearances if not item["passed"]
            ),
            "mounting_hole_count": len(mounting_holes),
            "display_module_count": len(display_modules),
            "standoff_keepouts_with_encroachment_count": sum(
                1 for item in standoff_keepouts if not item["passed"]
            ),
            "mezzanine_modules_with_z_conflicts_count": sum(
                1 for item in mezzanine_z_clearance if not item["passed"]
            ),
            "edge_interfaces_checked": len(edge_interface_audit),
            "edge_interfaces_failed": sum(1 for item in edge_interface_audit if not item["passed"]),
        },
        "courtyard_collisions": courtyard_collisions[:20],
        "board_edge_overhangs": {
            "expected_overhangs": expected_overhangs,
            "unintended_overhangs": unintended_overhangs,
        },
        "connector_clearances": [c for c in connector_clearances if not c["passed"]],
        "edge_interface_orientation_audit": edge_interface_audit,
        "display_and_mounting_audit": {
            "mounting_holes": mounting_holes,
            "display_modules": display_modules,
            "has_pcb_mounting_holes": len(mounting_holes) > 0,
            "external_module_mounting_notes": external_module_mounting_notes,
        },
        "mezzanine_and_standoff_audit": {
            "standoff_keepouts": standoff_keepouts,
            "mezzanine_z_clearance": mezzanine_z_clearance,
            "rigid_header_alignment": rigid_headers,
        },
        "locked": layout["locked"],
        "target_locked": layout["target_locked"],
        "lock_files": layout["lock_files"],
        "lock_warning": layout["lock_warning"],
    }


def _ipc_min_width(current_a: float, copper_oz: float, max_temp_rise_c: float) -> float:
    """Calculate minimum external copper trace width in mm according to IPC-2221.

    Args:
        current_a: Conductor current in amperes.
        copper_oz: Copper foil weight in ounces per square foot (1.0 oz = 35 um).
        max_temp_rise_c: Allowable conductor temperature rise above ambient in degrees Celsius.

    Returns:
        Minimum trace width in millimeters.
    """
    t_mils = 1.378 * copper_oz
    area_mils2 = (current_a / (0.048 * (max_temp_rise_c ** 0.44))) ** (1.0 / 0.725)
    return (area_mils2 / t_mils) * 0.0254


def analyze_pcb_traces(
    pcb_path: str,
    net_currents_a: dict[str, float] | None = None,
    copper_oz: float = 1.0,
    max_temp_rise_c: float = 10.0,
    max_voltage_drop_v: float = 0.1,
    neckdown_max_length_mm: float = 3.0,
) -> dict[str, Any]:
    """Analyze copper trace widths against IPC-2221, neck-down rules, voltage drop, and critical net parasitics.

    Args:
        pcb_path: Path to the target `.kicad_pcb` file.
        net_currents_a: Optional dictionary mapping net names to target currents in amperes.
        copper_oz: Copper foil thickness in ounces (default 1.0 oz = 0.035 mm).
        max_temp_rise_c: Allowable temperature rise in degrees Celsius for IPC-2221 width sizing.
        max_voltage_drop_v: Maximum acceptable path voltage drop in volts along power rails.
        neckdown_max_length_mm: Maximum allowable segment length in mm for local pad neck-down.

    Returns:
        Structured trace analysis dictionary with power rail audits, segment classifications,
        voltage drop calculations, crystal/decoupling/I2C evaluations, and KiCad GUI locks.
    """
    resolved, tree = _parse_pcb_file(pcb_path)
    lock_info = check_kicad_lock_files(str(resolved))
    net_map = _extract_net_map(tree)

    footprints, all_pads = _parse_footprints_and_pads(tree, net_map)
    segments = _extract_segments(tree, net_map)
    zones = _extract_zones(tree, net_map)

    t_mm = 0.035 * copper_oz
    rho = 0.01724

    segs_by_net: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for seg in segments:
        if seg["net"]:
            segs_by_net[seg["net"]].append(seg)

    pads_by_net: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pad in all_pads:
        if pad["net"]:
            pads_by_net[pad["net"]].append(pad)

    all_nets = set(segs_by_net.keys()) | set(pads_by_net.keys())

    auto_currents: dict[str, float] = {}
    for net in all_nets:
        upper = net.strip().upper()
        if _is_gnd_net(upper):
            auto_currents[net] = 1.0
        elif any(tok in upper for tok in ("+5V", "5V", "VCC", "VDD")):
            auto_currents[net] = 1.0
        elif any(tok in upper for tok in ("BAT", "VIN", "VOUT", "VBUS", "PWR", "12V", "3V3")):
            auto_currents[net] = 1.5
        else:
            net_fps = {p["reference"] for p in pads_by_net.get(net, ())}
            if any(
                ref.startswith(("U_BOOST", "U_CHG", "J_BATT", "J_PWR", "J_SW_PWR"))
                for ref in net_fps
            ):
                auto_currents[net] = 1.5

    if net_currents_a:
        for k, v in net_currents_a.items():
            for net in all_nets:
                if net.lower() == k.lower():
                    auto_currents[net] = float(v)

    power_net_analysis: list[dict[str, Any]] = []
    total_undersized_count = 0
    total_voltage_drop_violations = 0

    for net, current_a in sorted(auto_currents.items()):
        net_segs = segs_by_net.get(net, [])
        net_pads = pads_by_net.get(net, [])

        ipc_min_w = _ipc_min_width(current_a, copper_oz, max_temp_rise_c)

        if net_pads:
            min_pad_dim = min(min(p["size_x"], p["size_y"]) for p in net_pads)
        else:
            min_pad_dim = ipc_min_w

        allowed_neckdown_w = min(ipc_min_w, max(0.2, min_pad_dim * 0.85))
        small_pads = [p for p in net_pads if min(p["size_x"], p["size_y"]) < ipc_min_w - 1e-4]

        trunk_ok_count = 0
        valid_neckdown_count = 0
        undersized_count = 0
        undersized_segments: list[dict[str, Any]] = []

        for seg in net_segs:
            w = seg["width"]
            l_mm = seg["length_mm"]
            if w >= ipc_min_w - 1e-4:
                trunk_ok_count += 1
            else:
                near_small_pad = False
                if l_mm <= neckdown_max_length_mm and w >= allowed_neckdown_w - 1e-4:
                    for sp in small_pads:
                        d1 = math.hypot(seg["x1"] - sp["x"], seg["y1"] - sp["y"])
                        d2 = math.hypot(seg["x2"] - sp["x"], seg["y2"] - sp["y"])
                        if min(d1, d2) <= 1.5:
                            near_small_pad = True
                            break
                if near_small_pad:
                    valid_neckdown_count += 1
                else:
                    undersized_count += 1
                    total_undersized_count += 1
                    undersized_segments.append(
                        {
                            "start": {"x": seg["x1"], "y": seg["y1"]},
                            "end": {"x": seg["x2"], "y": seg["y2"]},
                            "width_mm": seg["width"],
                            "required_min_width_mm": round(ipc_min_w, 3),
                            "length_mm": round(l_mm, 3),
                            "layer": seg["layer"],
                        }
                    )

        total_length_mm = sum(s["length_mm"] for s in net_segs)
        total_segments_r = sum(
            rho * (s["length_mm"] / 1000.0) / (max(s["width"], 0.05) * t_mm)
            for s in net_segs
        )

        adj: dict[tuple[float, float], list[tuple[tuple[float, float], float]]] = defaultdict(list)
        for s in net_segs:
            u = (round(s["x1"], 2), round(s["y1"], 2))
            v = (round(s["x2"], 2), round(s["y2"], 2))
            r = rho * (s["length_mm"] / 1000.0) / (max(s["width"], 0.05) * t_mm)
            adj[u].append((v, r))
            adj[v].append((u, r))

        pad_nodes = []
        for p in net_pads:
            best_k = None
            best_d = 999.0
            for k in adj:
                d = math.hypot(p["x"] - k[0], p["y"] - k[1])
                if d < best_d:
                    best_d = d
                    best_k = k
            if best_k and best_d <= 2.5:
                pad_nodes.append((p["reference"], p["pad_number"], best_k))

        max_path_r = 0.0
        for i in range(len(pad_nodes)):
            src = pad_nodes[i][2]
            dist: dict[tuple[float, float], float] = {src: 0.0}
            pq = [(0.0, src)]
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist.get(u, 999.0):
                    continue
                for v, weight in adj[u]:
                    if d + weight < dist.get(v, 999.0):
                        dist[v] = d + weight
                        heapq.heappush(pq, (d + weight, v))
            for j in range(i + 1, len(pad_nodes)):
                dst = pad_nodes[j][2]
                if dst in dist:
                    if dist[dst] > max_path_r:
                        max_path_r = dist[dst]

        has_copper_zone = any(z["net"] == net for z in zones)
        estimated_path_r = max_path_r if max_path_r > 0.0 else total_segments_r
        voltage_drop_v = round(current_a * estimated_path_r, 4)
        power_loss_mw = round((current_a ** 2) * estimated_path_r * 1000.0, 3)

        voltage_drop_exceeded = bool(voltage_drop_v > max_voltage_drop_v and not has_copper_zone)
        if voltage_drop_exceeded:
            total_voltage_drop_violations += 1

        power_net_analysis.append(
            {
                "net": net,
                "current_a": current_a,
                "ipc_min_width_mm": round(ipc_min_w, 3),
                "allowed_neckdown_width_mm": round(allowed_neckdown_w, 3),
                "total_length_mm": round(total_length_mm, 2),
                "total_segments_resistance_ohm": round(total_segments_r, 4),
                "estimated_path_resistance_ohm": round(estimated_path_r, 4),
                "voltage_drop_v": voltage_drop_v,
                "power_loss_mw": power_loss_mw,
                "has_copper_zone": has_copper_zone,
                "voltage_drop_exceeded": voltage_drop_exceeded,
                "segment_classification": {
                    "total_segments": len(net_segs),
                    "trunk_ok_count": trunk_ok_count,
                    "valid_neckdown_count": valid_neckdown_count,
                    "undersized_count": undersized_count,
                    "undersized_segments": undersized_segments[:10],
                },
            }
        )

    critical_violations = 0

    crystal_nets: list[dict[str, Any]] = []
    crystal_fps = [fp for fp in footprints if fp["reference"].startswith("Y")]
    crystal_net_names = {net for fp in crystal_fps for net in fp["nets"] if net}
    if not crystal_net_names:
        for net in all_nets:
            if "XTAL" in net.upper():
                crystal_net_names.add(net)

    for c_net in sorted(crystal_net_names):
        c_segs = segs_by_net.get(c_net, [])
        c_len = sum(s["length_mm"] for s in c_segs)
        passed = c_len <= 15.0
        if not passed:
            critical_violations += 1
        crystal_nets.append(
            {
                "net": c_net,
                "total_length_mm": round(c_len, 2),
                "max_allowed_length_mm": 15.0,
                "passed": passed,
                "warning": (
                    f"Crystal trace length ({round(c_len, 2)} mm) exceeds recommended 15.0 mm maximum."
                    if not passed
                    else None
                ),
            }
        )

    decoupling_caps: list[dict[str, Any]] = []
    u_power_pads = [
        p
        for p in all_pads
        if p["reference"].startswith("U")
        and not p["reference"].startswith(("U_BOOST", "U_CHG"))
        and any(tok in p["net"].upper() for tok in ("+5V", "5V", "VCC", "VDD", "GND"))
    ]

    for fp in footprints:
        if fp["reference"].startswith("C"):
            val_clean = fp["value"].lower()
            if any(tok in val_clean for tok in ("100n", "0.1u", "104")) or (
                "+5V" in fp["nets"] and "GND" in fp["nets"]
            ):
                min_dist = 999.0
                nearest_u = None
                c_pads = [p for p in all_pads if p["reference"] == fp["reference"]]
                for cp in c_pads:
                    for up in u_power_pads:
                        if cp["net"] == up["net"]:
                            d = math.hypot(cp["x"] - up["x"], cp["y"] - up["y"])
                            if d < min_dist:
                                min_dist = d
                                nearest_u = f"{up['reference']}.{up['pad_number']} ({up['net']})"

                passed = min_dist <= 8.0
                if not passed:
                    critical_violations += 1
                decoupling_caps.append(
                    {
                        "capacitor": fp["reference"],
                        "value": fp["value"],
                        "nearest_ic_pad": nearest_u,
                        "distance_to_nearest_ic_mm": round(min_dist, 2),
                        "max_allowed_distance_mm": 8.0,
                        "passed": passed,
                        "warning": (
                            f"Decoupling capacitor {fp['reference']} is {round(min_dist, 2)} mm from IC power pin (> 8.0 mm limit)."
                            if not passed
                            else None
                        ),
                    }
                )

    i2c_and_clock_nets: list[dict[str, Any]] = []
    for net in sorted(all_nets):
        upper = net.upper()
        if any(tok in upper for tok in ("SDA", "SCL", "I2C", "SCK", "CLK")):
            i_segs = segs_by_net.get(net, [])
            i_len = sum(s["length_mm"] for s in i_segs)
            est_cap_pf = round(i_len * 0.1, 2)
            passed = i_len <= 150.0
            if not passed:
                critical_violations += 1
            i2c_and_clock_nets.append(
                {
                    "net": net,
                    "total_length_mm": round(i_len, 2),
                    "estimated_capacitance_pf": est_cap_pf,
                    "max_allowed_length_mm": 150.0,
                    "passed": passed,
                }
            )

    return {
        "status": "ok",
        "pcb_path": str(resolved),
        "copper_oz": copper_oz,
        "max_temp_rise_c": max_temp_rise_c,
        "max_voltage_drop_v": max_voltage_drop_v,
        "neckdown_max_length_mm": neckdown_max_length_mm,
        "passed": total_undersized_count == 0 and total_voltage_drop_violations == 0,
        "summary": {
            "power_nets_checked": len(power_net_analysis),
            "undersized_segment_count": total_undersized_count,
            "voltage_drop_violations": total_voltage_drop_violations,
            "critical_net_violations": critical_violations,
        },
        "power_net_analysis": power_net_analysis,
        "critical_nets": {
            "crystal_nets": [c for c in crystal_nets if not c["passed"]],
            "decoupling_capacitors": [c for c in decoupling_caps if not c["passed"]],
            "i2c_and_clock_nets": i2c_and_clock_nets,
        },
        "locked": lock_info["locked"],
        "target_locked": lock_info["target_locked"],
        "lock_files": lock_info["lock_files"],
        "lock_warning": lock_info["warning"],
    }


def _compute_project_signature(
    resolved_dir: Path,
    bom_or_pcb_path: str | None,
) -> dict[str, Any]:
    """Build a lightweight project signature from the target PCB/BOM name and detected hardware families."""
    target_pcb: Path | None = None
    target_bom: Path | None = None
    if bom_or_pcb_path:
        cand = Path(bom_or_pcb_path).expanduser().resolve()
        if cand.is_file():
            if cand.suffix.lower() == ".kicad_pcb":
                target_pcb = cand
            else:
                target_bom = cand
    if target_pcb is None and target_bom is None:
        pcb_candidates = sorted(resolved_dir.glob("*.kicad_pcb"))
        if pcb_candidates:
            target_pcb = pcb_candidates[0]
        else:
            txt_candidates = sorted(resolved_dir.glob("*.txt"))
            if txt_candidates:
                target_bom = txt_candidates[0]

    sig_tokens: set[str] = set()
    token_pat = re.compile(
        r"(tp4056|hw-?373|mt3608|mini-?360|mini-?560|wh148|rj11|esp32|atmega|lcd|oled|1602|2004)",
        re.IGNORECASE,
    )
    if target_bom is not None:
        try:
            raw = target_bom.read_text(encoding="utf-8", errors="replace")
            sig_tokens.update(m.group(1).lower().replace("-", "") for m in token_pat.finditer(raw))
        except OSError:
            pass
    if target_pcb is not None:
        try:
            raw = target_pcb.read_text(encoding="utf-8", errors="replace")
            sig_tokens.update(m.group(1).lower().replace("-", "") for m in token_pat.finditer(raw))
        except OSError:
            pass

    return {
        "project_stem": (target_pcb.stem if target_pcb else (target_bom.stem if target_bom else None)),
        "detected_tokens": sorted(sig_tokens),
    }


def manage_design_decisions(
    project_dir: str,
    action: str = "check",
    decisions: dict[str, Any] | None = None,
    bom_or_pcb_path: str | None = None,
) -> dict[str, Any]:
    """Inspect, validate, and record project design decisions into `design_decisions.json`.

    Args:
        project_dir: Root directory of the project.
        action: Operation to perform (`"check"`, `"load"`, `"save"`).
        decisions: Optional dictionary of decisions to save or merge.
        bom_or_pcb_path: Optional path to a `.kicad_pcb` or BOM specification file to analyze.

    Returns:
        Dictionary reporting decision state, resolved decisions, and unresolved questions.
    """
    resolved_dir = Path(project_dir).expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    dec_file = resolved_dir / "design_decisions.json"
    current_sig = _compute_project_signature(resolved_dir, bom_or_pcb_path)

    saved_data: dict[str, Any] = {}
    if dec_file.is_file():
        try:
            saved_data = json.loads(dec_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved_data = {}

    if action == "load":
        return {
            "status": "ok",
            "action": "load",
            "project_dir": str(resolved_dir),
            "decisions_file": str(dec_file),
            "decisions": saved_data,
        }

    if action == "save":
        merged = dict(saved_data)
        if decisions:
            merged.update(decisions)
        merged["project_signature"] = current_sig
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        dec_file.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "status": "ok",
            "action": "save",
            "project_dir": str(resolved_dir),
            "decisions_file": str(dec_file),
            "decisions": merged,
        }

    stale_decisions = False
    stale_reason: str | None = None
    saved_sig = saved_data.get("project_signature")
    if saved_data:
        if isinstance(saved_sig, dict):
            prev_stem = saved_sig.get("project_stem")
            curr_stem = current_sig.get("project_stem")
            prev_tokens = set(saved_sig.get("detected_tokens", []))
            curr_tokens = set(current_sig.get("detected_tokens", []))
            if prev_stem and curr_stem and prev_stem != curr_stem:
                stale_decisions = True
                stale_reason = f"Cached decisions belong to '{prev_stem}', but current project is '{curr_stem}'."
            elif prev_tokens and curr_tokens and prev_tokens != curr_tokens:
                stale_decisions = True
                stale_reason = (
                    f"Detected hardware modules changed ({sorted(prev_tokens)} -> {sorted(curr_tokens)})."
                )
        elif current_sig.get("detected_tokens"):
            saved_blob = json.dumps(saved_data, ensure_ascii=False).lower()
            curr_tokens = current_sig["detected_tokens"]
            if not any(tok in saved_blob for tok in curr_tokens):
                stale_decisions = True
                stale_reason = (
                    f"Existing design_decisions.json has no project signature and does not match detected modules {curr_tokens}."
                )

    resolved_decisions = {} if stale_decisions else dict(saved_data)
    if decisions:
        resolved_decisions.update(decisions)

    target_pcb: Path | None = None
    target_bom: Path | None = None

    if bom_or_pcb_path:
        cand = Path(bom_or_pcb_path).expanduser().resolve()
        if cand.is_file():
            if cand.suffix.lower() == ".kicad_pcb":
                target_pcb = cand
            else:
                target_bom = cand

    if target_pcb is None and target_bom is None:
        pcb_candidates = list(resolved_dir.glob("*.kicad_pcb"))
        if pcb_candidates:
            target_pcb = pcb_candidates[0]
        else:
            txt_candidates = list(resolved_dir.glob("*.txt"))
            if txt_candidates:
                target_bom = txt_candidates[0]

    detected_display = False
    detected_power_modules = False
    if target_pcb is not None:
        try:
            layout = inspect_pcb_layout(str(target_pcb), compact=False)
            for fp in layout.get("footprints", []):
                if _is_display_module(fp):
                    detected_display = True
                if _is_module_footprint(fp):
                    detected_power_modules = True
        except Exception:
            pass

    if target_bom is not None:
        try:
            raw_text = target_bom.read_text(encoding="utf-8", errors="replace")
            if re.search(r"lcd|1602|2004|oled|display|екран|дисплей", raw_text, re.IGNORECASE):
                detected_display = True
            if re.search(r"mini-?360|mini-?560|mt-?3608|tp4056|hw-?373", raw_text, re.IGNORECASE):
                detected_power_modules = True
        except Exception:
            pass

    all_questions = [
        {
            "key": "display_mounting_mode",
            "category": "display_mounting_mode",
            "question": "How should the LCD display module be mounted relative to the main board?",
            "options": [
                "(Recommended) Front panel / enclosure mounting via JST-XH cable harness",
                "Second-floor mezzanine mounted on PCB standoffs directly above the main board",
                "Direct solder / right-angle pin header on PCB edge",
            ],
            "recommended": "(Recommended) Front panel / enclosure mounting via JST-XH cable harness",
        },
        {
            "key": "module_variants",
            "category": "module_variants",
            "question": "Which DC-DC power converter and battery charging module variants should be used?",
            "options": [
                "(Recommended) THT module headers with edge-accessible USB connector overhang (e.g. TP4056_HW373, MT3608)",
                "Direct surface-mount (SMD) power converter sub-boards (e.g. Mini560 SMD 15x18 mm)",
                "Integrated discrete on-board power regulator ICs with thermal vias",
            ],
            "recommended": "(Recommended) THT module headers with edge-accessible USB connector overhang (e.g. TP4056_HW373, MT3608)",
        },
        {
            "key": "connector_orientation_and_keying",
            "category": "connector_orientation_and_keying",
            "question": "What connector orientation, pitch, and keying conventions apply to external headers?",
            "options": [
                "(Recommended) Keyed vertical JST-XH connectors (2.50 mm pitch) for power, battery, and I2C display",
                "Right-angle JST-XH connectors facing outer board edges for thin enclosure profile",
                "Standard 2.54 mm unshrouded pin headers (PLS / PBS)",
                "Screw terminal blocks (KF301, 5.08 mm pitch) for robust power wiring",
            ],
            "recommended": "(Recommended) Keyed vertical JST-XH connectors (2.50 mm pitch) for power, battery, and I2C display",
        },
        {
            "key": "button_interface",
            "category": "button_interface",
            "question": "What user input button and switch interface configuration should be implemented?",
            "options": [
                "(Recommended) On-board tactile push button matrix (e.g. 6x6 mm tact switches)",
                "Off-board panel switches wired via dedicated pin header / screw terminals",
                "Membrane keypad / capacitive touch interface",
            ],
            "recommended": "(Recommended) On-board tactile push button matrix (e.g. 6x6 mm tact switches)",
        },
        {
            "key": "pcb_stackup_and_limits",
            "category": "pcb_stackup_and_limits",
            "question": "What copper layer count, copper weight, board thickness, and 3D component height limits apply?",
            "options": [
                "(Recommended) 2-layer PCB (F.Cu signal + B.Cu ground pour), 1 oz (35 µm) copper, 1.6 mm FR-4",
                "4-layer PCB (Signal - GND Plane - Power Plane - Signal), 1 oz copper, 1.6 mm FR-4",
                "2-layer PCB, 2 oz (70 µm) heavy copper for high-current traces (ΔU <= 0.1 V)",
                "1-layer single-sided PCB (DIY home toner-transfer / CNC milling)",
            ],
            "recommended": "(Recommended) 2-layer PCB (F.Cu signal + B.Cu ground pour), 1 oz (35 µm) copper, 1.6 mm FR-4",
        },
        {
            "key": "board_shape_and_mounting",
            "category": "board_shape_and_mounting",
            "question": "What board dimensions, corner radius, and mounting hole configuration should be targeted?",
            "options": [
                "(Recommended) Rectangular board matching target dimensions with R3 rounded corners and 4x M3 mounting holes",
                "Rectangular sharp-corner board matching target enclosure slots",
                "Custom profile with edge cutouts for connectors, mounting ears, or display windows",
            ],
            "recommended": "(Recommended) Rectangular board matching target dimensions with R3 rounded corners and 4x M3 mounting holes",
        },
        {
            "key": "silkscreen_conventions",
            "category": "silkscreen_conventions",
            "question": "What silkscreen naming conventions, font sizes, and pin labels should be placed on the board?",
            "options": [
                "(Recommended) Functional designators (e.g. U_CHG, J_BATT, J_LCD) with explicit polarity (+/-) and pin labels, font >= 1.0 x 0.15 mm",
                "Standard academic designators (e.g. U1, U2, J1, J2) with pin-1 indicators, font >= 1.0 x 0.15 mm",
                "Minimalist silkscreen with component outlines only to reduce visual noise",
            ],
            "recommended": "(Recommended) Functional designators (e.g. U_CHG, J_BATT, J_LCD) with explicit polarity (+/-) and pin labels, font >= 1.0 x 0.15 mm",
        },
        {
            "key": "enclosure_fit_and_overhangs",
            "category": "enclosure_fit_and_overhangs",
            "question": "What enclosure mounting, edge overhang allowances, and wall clearances apply?",
            "options": [
                "(Recommended) Edge overhangs permitted for USB charging / programming receptacles (TP4056 / USB-C)",
                "Strict zero-overhang flush enclosure fit (all connectors recessed within PCB outline)",
                "Enclosure with custom front/rear panel faceplate cutouts",
            ],
            "recommended": "(Recommended) Edge overhangs permitted for USB charging / programming receptacles (TP4056 / USB-C)",
        },
    ]

    alias_map = {
        "display_mounting": "display_mounting_mode",
        "power_modules_mounting": "module_variants",
        "connector_specifications": "connector_orientation_and_keying",
        "layer_count": "pcb_stackup_and_limits",
        "copper_and_substrate": "pcb_stackup_and_limits",
        "pcb_stackup": "pcb_stackup_and_limits",
        "board_outline_and_enclosure": "board_shape_and_mounting",
        "silkscreen_and_designators": "silkscreen_conventions",
        "power_rail_currents": "pcb_stackup_and_limits",
    }

    unresolved_questions = [
        q for q in all_questions
        if q["key"] not in resolved_decisions
        and not any(alias_map.get(k) == q["key"] for k in resolved_decisions)
    ]

    return {
        "status": "ok",
        "action": "check",
        "project_dir": str(resolved_dir),
        "decisions_file": str(dec_file),
        "all_resolved": len(unresolved_questions) == 0,
        "stale_decisions": stale_decisions,
        "stale_reason": stale_reason,
        "project_signature": current_sig,
        "resolved_decisions": resolved_decisions,
        "unresolved_questions": unresolved_questions,
    }
