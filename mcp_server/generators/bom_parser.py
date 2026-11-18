"""Human-readable Ukrainian/English BOM and specification parser.

Parses plain-text hardware design notes (such as `BOM.txt` or prose extracted from `Items.DOCX`) into structured
JSON containing revisions, project metadata, components with quantities and
package hints, connector details, board fabrication properties, custom KiCad
module matches, pinout mappings, and wiring/layout rules.
"""

from pathlib import Path
import re
from typing import Any

_CUSTOM_MODULE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmini[-\s_]?360\b|\bmp2307\b", re.IGNORECASE), "Mini360"),
    (re.compile(r"\bmini[-\s_]?560\b|\bjw5068a?\b", re.IGNORECASE), "Mini560"),
    (re.compile(r"\bmt[-\s_]?3608\b", re.IGNORECASE), "MT3608"),
    (re.compile(r"\btp4056\b|\bhw[-\s_]?373\b", re.IGNORECASE), "TP4056_HW373"),
]

_EXTERNAL_MODULE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\batmega328p(?:-[a-z]+)?\b", re.IGNORECASE), "ATmega328P-PU"),
    (re.compile(r"\b74hc595\b", re.IGNORECASE), "74HC595"),
    (re.compile(r"\b74hc165\b", re.IGNORECASE), "74HC165"),
    (re.compile(r"\bpcf8574\b", re.IGNORECASE), "PCF8574"),
    (re.compile(r"\blcd\s*2004\b|екран\s*2004", re.IGNORECASE), "LCD_2004"),
    (re.compile(r"\bkeypad\s*4x4\b|клавіатур\w*\s*4x4", re.IGNORECASE), "Keypad_4x4"),
    (re.compile(r"\besp32\s*nodemcu\b", re.IGNORECASE), "ESP32_NodeMCU_38pin"),
    (re.compile(r"\bpico(?:\s*w)?\b", re.IGNORECASE), "Raspberry_Pi_Pico"),
    (re.compile(r"\bbme280\b", re.IGNORECASE), "BME280"),
    (re.compile(r"\bams1117(?:-[\d.]+)?\b", re.IGNORECASE), "AMS1117"),
    (re.compile(r"\birlz44n\b", re.IGNORECASE), "IRLZ44N"),
    (re.compile(r"\bbss138\b", re.IGNORECASE), "BSS138"),
]

_PACKAGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bDIP-\d+\b", re.IGNORECASE),
    re.compile(r"\bSOIC-\d+\b", re.IGNORECASE),
    re.compile(r"\bTSSOP-\d+\b", re.IGNORECASE),
    re.compile(r"\bQFP-\d+\b", re.IGNORECASE),
    re.compile(r"\bTO-220(?:-\d+)?\b", re.IGNORECASE),
    re.compile(r"\bTO-92\b", re.IGNORECASE),
    re.compile(r"\bSOT-223(?:-\d+)?\b", re.IGNORECASE),
    re.compile(r"\bSOT-23\b", re.IGNORECASE),
    re.compile(r"\bHC-49S?\b", re.IGNORECASE),
    re.compile(r"\bXH2\.54\s*\d+[-\s]?pin\b", re.IGNORECASE),
    re.compile(r"\bJST[-\s]?XH\s*\d+[-\s]?pin\b", re.IGNORECASE),
    re.compile(r"\b(?:Male|Female)\s+Header\s+\d+x\d+\b", re.IGNORECASE),
    re.compile(r"\b1N4148\b|\b1N4007\b|\bSMAJ[\d.]+A\b", re.IGNORECASE),
    re.compile(r"\bDO-41\b|\bSOD-?\d+\b|\bSMA\b|\bSMB\b", re.IGNORECASE),
    re.compile(r"\bDC-005\b|\bKCD-01\b|\bWH148\b", re.IGNORECASE),
    re.compile(r"\b(?:0402|0603|0805|1206|2920)\b"),
    re.compile(r"\b6x6\s*(?:мм|mm)\b", re.IGNORECASE),
]

_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"кнопка|перемикач|вимикач|switch|dip-?перемикач|kcd", re.IGNORECASE), "switch"),
    (re.compile(r"потенціометр|potentiometer|wh148", re.IGNORECASE), "potentiometer"),
    (re.compile(r"резистор|resistor|\bR_[A-Za-z0-9_]*|\bом\b|\bком\b|ohm", re.IGNORECASE), "resistor"),
    (re.compile(r"конденсатор|capacitor|\bpf\b|\bnf\b|\bмкф\b|\buf\b", re.IGNORECASE), "capacitor"),
    (re.compile(r"діод|diode|супресор|1n4148|1n4007|smaj", re.IGNORECASE), "diode"),
    (re.compile(r"кварц|crystal|hc-49|\bmhz\b", re.IGNORECASE), "crystal"),
    (re.compile(r"запобіжник|fuse", re.IGNORECASE), "fuse"),
    (re.compile(r"контрольн\w*\s*точк|testpoint", re.IGNORECASE), "testpoint"),
    (re.compile(r"іч-приймач|інфрачервоний\s+приймач|датчик|sensor|tsop|bme280|lcd\s*2004|keypad", re.IGNORECASE), "sensor_or_hmi"),
    (re.compile(r"header|гніздо|термінал|клемник|роз'єм|rj11|xh2\.54|dc-005|connector", re.IGNORECASE), "connector"),
    (re.compile(r"mosfet|транзистор|irlz44|bss138", re.IGNORECASE), "transistor"),
    (re.compile(r"mini[-\s]?360|mini[-\s]?560|mt[-\s]?3608|dc-dc|buck|boost|стабілізатор|ams1117", re.IGNORECASE), "power_converter"),
    (re.compile(r"tp4056|hw[-\s]?373|charger|заряд", re.IGNORECASE), "battery_charger"),
    (re.compile(r"74hc\d+|pcf8574|level\s*shifter|ahct125|погоджувач", re.IGNORECASE), "ic"),
    (re.compile(r"atmega|esp32|pico|мікроконтролер|mcu", re.IGNORECASE), "mcu"),
]

_MODULE_OWNING_CATEGORIES = {
    "power_converter",
    "battery_charger",
    "transistor",
    "sensor_or_hmi",
    "ic",
    "mcu",
}

_RULE_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"22\s*pf|xtal|кварц", re.IGNORECASE), "crystal_load_caps"),
    (re.compile(r"100\s*nf|0\.1\s*uf|decoupl|розв'?яз", re.IGNORECASE), "decoupling"),
    (re.compile(r"reset|dtr", re.IGNORECASE), "reset_circuit"),
    (re.compile(r"pull-?up|підтягуюч|4\.7\s*(?:k|ком)", re.IGNORECASE), "i2c_pullup"),
    (re.compile(r"pull-?down|стягуюч", re.IGNORECASE), "gate_pulldown"),
    (re.compile(r"конфлікт|uart|1\s*(?:k|ком)|tx.*rx", re.IGNORECASE), "uart_protection"),
    (re.compile(r"clearance|відступ|товщин\w*|трасуван|net\s*classes|полігон", re.IGNORECASE), "pcb_layout_rule"),
    (re.compile(r"keepout|отвір|отвор|кріплен|3\.2\s*мм|м3|виріз", re.IGNORECASE), "mechanical_constraint"),
    (re.compile(r"захист|запобіжник|переполюсов|5\.1\s*v|супресор", re.IGNORECASE), "power_protection"),
]


def _load_source_text(source: str) -> tuple[str, str | None]:
    """Return raw text and an optional filesystem path from a file path or direct string.

    Args:
        source: File path or raw BOM text.

    Returns:
        Tuple of `(raw_text, source_path)`.
    """
    if not source or not isinstance(source, str):
        raise ValueError("source must be a non-empty file path or specification text.")
    if "\n" not in source and len(source) < 512:
        candidate = Path(source)
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8-sig"), str(candidate.resolve())
        except OSError:
            pass
    return source, None


def _expand_designators(token_str: str) -> list[str]:
    """Expand designator lists and ranges into individual component references.

    Args:
        token_str: String containing references (e.g. `Q1, Q2, Q3`, `RV1-RV3`, `R1..4`).

    Returns:
        List of expanded reference strings.
    """
    results: list[str] = []
    for raw_part in re.split(r"\s*,\s*", token_str.strip()):
        part = raw_part.strip()
        if not part:
            continue
        dot_range = re.match(r"^([A-Za-z_]+)(\d+)\.\.(\d+)$", part)
        if dot_range:
            prefix, start, end = dot_range.group(1), int(dot_range.group(2)), int(dot_range.group(3))
            if start <= end and (end - start) <= 64:
                results.extend(f"{prefix}{i}" for i in range(start, end + 1))
                continue
        dash_range = re.match(r"^([A-Za-z_]+)(\d+)-([A-Za-z_]+)?(\d+)$", part)
        if dash_range:
            prefix = dash_range.group(1)
            prefix2 = dash_range.group(3) or prefix
            start, end = int(dash_range.group(2)), int(dash_range.group(4))
            if prefix == prefix2 and start <= end and (end - start) <= 64:
                results.extend(f"{prefix}{i}" for i in range(start, end + 1))
                continue
        results.append(part)
    return results


def _extract_quantity(text: str, designators: list[str]) -> int:
    """Infer component quantity from explicit count tokens or expanded designators.

    Args:
        text: Component descriptive text.
        designators: List of designators already parsed for the line.

    Returns:
        Integer component quantity (at least 1).
    """
    m_qty = re.search(
        r"(?:\(|:\s*|—\s*|\bпо\s+|\b)(\d+)(?:-\d+)?\s*(?:шт\.?|штук\w*|pcs|комплект\w*)\b",
        text,
        re.IGNORECASE,
    )
    if m_qty:
        return int(m_qty.group(1))
    m_prefix_qty = re.match(r"^\s*(\d+)\s*[xх]\s+", text, re.IGNORECASE)
    if m_prefix_qty:
        return int(m_prefix_qty.group(1))
    return max(1, len(designators))


def _match_module(text: str, category: str, designators: list[str]) -> tuple[str | None, str | None]:
    """Identify custom generator module or external module names for module-owning items.

    Args:
        text: Component descriptive text.
        category: Normalized hardware category.
        designators: Component designators.

    Returns:
        Tuple of `(custom_module_name, external_module_name)`.
    """
    is_passive_or_conn = category in {
        "switch",
        "connector",
        "capacitor",
        "resistor",
        "diode",
        "crystal",
        "fuse",
        "testpoint",
        "potentiometer",
    }
    if is_passive_or_conn or category not in _MODULE_OWNING_CATEGORIES:
        return None, None

    primary_desc = re.split(r"\s*(?:->|—|-)\s*(?:під|для)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    _ = designators

    custom = None
    external = None
    for pat, mod_name in _CUSTOM_MODULE_PATTERNS:
        if pat.search(primary_desc):
            custom = mod_name
            break
    for pat, mod_name in _EXTERNAL_MODULE_PATTERNS:
        if pat.search(primary_desc):
            external = mod_name
            break
    return custom, external


def _extract_packages(text: str) -> list[str]:
    """Extract package and footprint hint tokens from a component description.

    Args:
        text: Component descriptive text.

    Returns:
        List of unique package hint strings.
    """
    found: list[str] = []
    for pat in _PACKAGE_PATTERNS:
        for match in pat.findall(text):
            cleaned = match.strip()
            if cleaned and cleaned not in found:
                found.append(cleaned)
    return found


def _classify_category(text: str) -> str:
    """Classify a component into a normalized hardware category.

    Args:
        text: Component descriptive text.

    Returns:
        Category string identifier.
    """
    primary = re.split(r"\s*(?:->|—|-)\s*(?:під|для)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    for pat, category in _CATEGORY_RULES:
        if pat.search(primary):
            return category
    return "other"


def _classify_rule(text: str) -> str:
    """Classify a wiring or layout rule into a normalized rule tag.

    Args:
        text: Rule description text.

    Returns:
        Rule type tag identifier.
    """
    for pat, tag in _RULE_KEYWORDS:
        if pat.search(text):
            return tag
    return "general_note"


def _is_bom_subheader(line: str) -> bool:
    """Detect uppercase BOM category subheaders.

    Args:
        line: Raw text line.

    Returns:
        `True` if line represents a category banner.
    """
    if line.startswith("[") or line.startswith("!"):
        return False
    cleaned = line.rstrip(":").strip()
    if len(cleaned) < 4 or ":" in cleaned:
        return False
    letters = [ch for ch in cleaned if ch.isalpha()]
    return bool(letters) and all(ch.isupper() for ch in letters)


_PROSE_COMPONENT_HINT = re.compile(
    r"(?:шт\.?|штук|комплект|pcs|pc|пФ|нФ|мкФ|кОм|Ом|МГц|pF|nF|uF|kOhm|Ohm|MHz|"
    r"ATmega|ESP32|Pico|BME280|AMS1117|74HC\d+|PCF8574|LCD\s*\d+|Keypad|1N4148|1N4007|18650|TP4056|HW-?373|"
    r"MT3608|Mini[-\s]?360|Mini[-\s]?560|XH2\.54|JST|KF301|PLS|PBS|PinHeader|FemaleHeader|"
    r"DIP-\d+|SOIC-\d+|TSSOP-\d+|QFP-\d+|SOT-\d+|TO-\d+|0402|0603|0805|1206|HC-49|"
    r"Resistor|Capacitor|Crystal|Diode|Switch|Button|Connector|Header|MOSFET|Transistor|Regulator|Module|"
    r"Тумблер|Панелька|Резистор|Конденсатор|Кварц|Діод|Дисплей|Зарядка|"
    r"Перетворювач|Вимикач|Мікроконтролер|Акумулятор|Конектор|роз['’`]?єм|клемник|Зсувн|"
    r"Перфорована плата|2-pin|4-pin|8-pin)",
    re.IGNORECASE,
)


def _match_all_external_modules(text: str, category: str) -> list[str]:
    """Return all external modules referenced by a component line.

    Args:
        text: Component descriptive text.
        category: Normalized hardware category.

    Returns:
        List of matched external module identifiers.
    """
    is_passive_or_conn = category in {
        "switch",
        "connector",
        "capacitor",
        "resistor",
        "diode",
        "crystal",
        "fuse",
        "testpoint",
        "potentiometer",
    }
    if is_passive_or_conn or category not in _MODULE_OWNING_CATEGORIES:
        return []
    primary_desc = re.split(r"\s*(?:->|—|-)\s*(?:під|для)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    return [mod_name for pat, mod_name in _EXTERNAL_MODULE_PATTERNS if pat.search(primary_desc)]


def _extract_connector_details(
    name: str,
    raw_text: str,
    full_source_text: str,
) -> dict[str, Any] | None:
    """Extract physical orientation, mounting, pitch, keying, and current rating for connectors.

    Args:
        name: Cleaned component name.
        raw_text: Raw line text from which the component was parsed.
        full_source_text: Entire BOM document text for broader context.

    Returns:
        Connector details dictionary or `None` if the component is not a connector.
    """
    combined = f"{name} {raw_text}".lower()
    is_conn = bool(
        re.search(
            r"\b(?:jst[-\s]?xh|xh2\.54|jst[-\s]?ph|header|kf301|dc[-\s]?005|connector|термінал|клемник|роз'?єм|гніздо|гребінк|штир|штыр)\b",
            combined,
            re.IGNORECASE,
        )
    )
    if not is_conn:
        return None

    is_right_angle = bool(
        re.search(r"кутов|right-?angle|horizontal|90°|90\s*deg|-ra\b", combined, re.IGNORECASE)
    )
    orientation = "right_angle" if is_right_angle else "vertical"

    is_smd = bool(re.search(r"\b(?:smd|smt|surface)\b", combined, re.IGNORECASE))
    mounting = "SMD" if is_smd else "THT"

    if re.search(r"kf301|гвинтов|клемник|5\.08", combined, re.IGNORECASE):
        pitch_mm: float | None = 5.08
    elif re.search(r"jst[-\s]?ph|2\.0\s*mm|2\.0\s*мм", combined, re.IGNORECASE):
        pitch_mm = 2.0
    elif re.search(r"jst[-\s]?xh|b\d*b-xh|2\.5\s*mm|2\.5\s*мм", combined, re.IGNORECASE) and not re.search(
        r"xh2\.54", combined, re.IGNORECASE
    ):
        pitch_mm = 2.5
    elif re.search(r"xh2\.54|pls|pbs|pinheader|femaleheader|2\.54", combined, re.IGNORECASE):
        pitch_mm = 2.54
    else:
        m_pitch = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|мм)", combined)
        pitch_mm = float(m_pitch.group(1)) if m_pitch else None

    is_bare = bool(
        re.search(
            r"pinheader|femaleheader|\bpls\b|\bpbs\b|гребінк|штыр|штир",
            combined,
            re.IGNORECASE,
        )
    )
    is_keyed_token = bool(
        re.search(
            r"jst[-\s]?xh|xh2\.54|jst[-\s]?ph|dc[-\s]?005|usb|kf301|j_batt|акб|акумулятор|battery",
            combined,
            re.IGNORECASE,
        )
    )
    keyed = is_keyed_token or (not is_bare)

    if re.search(r"kf301|клемник|terminal\s*block", combined, re.IGNORECASE):
        current_rating_a: float | None = 10.0
    elif re.search(r"dc[-\s]?005|power\s*jack", combined, re.IGNORECASE):
        current_rating_a = 5.0
    elif re.search(r"jst[-\s]?xh|xh2\.54|pinheader|femaleheader|header", combined, re.IGNORECASE):
        current_rating_a = 3.0
    else:
        m_curr = re.search(r"(\d+(?:\.\d+)?)\s*a\b", combined, re.IGNORECASE)
        current_rating_a = float(m_curr.group(1)) if m_curr else None

    return {
        "orientation": orientation,
        "mounting": mounting,
        "pitch_mm": pitch_mm,
        "keyed": keyed,
        "current_rating_a": current_rating_a,
    }


def _extract_board_properties(text: str, components: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract fabrication parameters, dimensions, stackup, and silkscreen rules from BOM text.

    Args:
        text: Entire BOM document text.
        components: Parsed component items.

    Returns:
        Structured dictionary of inferred board properties.
    """
    if re.search(r"1-шаров|одношаров|single-layer|лут", text, re.IGNORECASE):
        layer_count: int | None = 1
    elif re.search(r"4-шаров|чотиришаров|4-layer", text, re.IGNORECASE):
        layer_count = 4
    elif re.search(r"2-шаров|двошаров|2-layer", text, re.IGNORECASE):
        layer_count = 2
    else:
        layer_count = None

    if re.search(r"2\s*oz|70\s*(?:мкм|um|µm)", text, re.IGNORECASE):
        copper_oz: float | None = 2.0
    elif re.search(r"1\s*oz|35\s*(?:мкм|um|µm)", text, re.IGNORECASE):
        copper_oz = 1.0
    else:
        copper_oz = None

    if re.search(r"алюмін|aluminum|mcpcb", text, re.IGNORECASE):
        substrate_material: str | None = "Aluminum"
    elif re.search(r"cem-1\b", text, re.IGNORECASE):
        substrate_material = "CEM-1"
    elif re.search(r"fr-4|fr4|склотекстоліт", text, re.IGNORECASE):
        substrate_material = "FR-4"
    else:
        substrate_material = None

    m_thick = re.search(
        r"(?:товщина\s+плати|board\s+thickness|товщина\s*:)\s*(\d+(?:\.\d+)?)\s*(?:мм|mm)",
        text,
        re.IGNORECASE,
    )
    if m_thick:
        board_thickness_mm: float | None = float(m_thick.group(1))
    else:
        m_std = re.search(r"\b(0\.8|1\.0|1\.2|1\.6|2\.0)\s*(?:мм|mm)\b", text, re.IGNORECASE)
        board_thickness_mm = float(m_std.group(1)) if m_std else None

    m_dim = re.search(
        r"(?:розмір\w*(?:\s+плат\w*)?|габарит\w*(?:\s+плат\w*)?|board\s+(?:size|dimensions)|pcb\s+(?:size|dimensions)|плат[аи]\s*:?)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:x|х|×)\s*(\d+(?:\.\d+)?)\s*(?:мм|mm)",
        text,
        re.IGNORECASE,
    )
    if m_dim and float(m_dim.group(1)) >= 15.0 and float(m_dim.group(2)) >= 15.0:
        dimensions_mm: dict[str, float] | None = {
            "width_mm": float(m_dim.group(1)),
            "height_mm": float(m_dim.group(2)),
        }
    else:
        dimensions_mm = None

    if re.search(
        r"скруглен\w*|rounded\s*(?:corner|edge)?s?|радіус\w*\s*(?:кут\w*\s*)?R\d+|(?:кут\w*|corner\w*)\s*R\d+",
        text,
        re.IGNORECASE,
    ):
        outline_shape = "rounded_corners"
    elif re.search(r"виріз\w*|cutout|internal\s+slot", text, re.IGNORECASE):
        outline_shape = "custom_cutouts"
    else:
        outline_shape = "rectangular"

    m_z = re.search(
        r"(?:макс\w*\s+висот\w*(?:\s+компонент\w*)?|max\w*\s+(?:component\s+)?height|обмеження\s+по\s+висоті)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:мм|mm)",
        text,
        re.IGNORECASE,
    )
    max_component_height_mm = float(m_z.group(1)) if m_z else None

    func_count = 0
    std_count = 0
    for comp in components:
        for d in comp.get("designators", []):
            if "_" in d or any(d.startswith(p) for p in ("BTN_", "SW_", "U_", "J_")):
                func_count += 1
            else:
                std_count += 1
    if func_count > 0 and std_count > 0:
        designator_style: str | None = "functional" if func_count >= std_count else "standard"
    elif func_count > 0:
        designator_style = "functional"
    elif std_count > 0:
        designator_style = "standard"
    else:
        designator_style = None

    silkscreen_conventions = {
        "designator_style": designator_style,
        "pin_labels_required": True,
        "min_font_height_mm": 1.0,
        "min_font_thickness_mm": 0.15,
    }

    return {
        "layer_count": layer_count,
        "copper_oz": copper_oz,
        "substrate_material": substrate_material,
        "board_thickness_mm": board_thickness_mm,
        "dimensions_mm": dimensions_mm,
        "outline_shape": outline_shape,
        "max_component_height_mm": max_component_height_mm,
        "silkscreen_conventions": silkscreen_conventions,
    }


def _parse_component_line(line: str) -> dict[str, Any] | None:
    """Parse a single BOM line item into a component metadata dictionary.

    Args:
        line: Raw text line containing a component declaration.

    Returns:
        Structured component dictionary or `None` if line is not a component item.
    """
    stripped = line.strip()
    if not stripped:
        return None

    designator_part = ""
    desc_part = stripped

    m_box = re.match(
        r"^\[\s*[xX ]?\s*\]\s+([A-Za-z][A-Za-z0-9_]*(?:\s*(?:,|\.\.|-)\s*[A-Za-z0-9_]+)*)\s+(.+)$",
        stripped,
    )
    if m_box:
        designator_part = m_box.group(1)
        desc_part = m_box.group(2).strip()
    else:
        m_colon = re.match(
            r"^([A-Za-z][A-Za-z0-9_]*(?:\s*(?:,|\.\.|-)\s*[A-Za-z0-9_]+)*)\s*:\s+(.+)$",
            stripped,
        )
        if m_colon and not re.match(
            r"^(?:ПРОЕКТ|ОПИС|Footprint|Пін|Pin)\b", m_colon.group(1), re.IGNORECASE
        ):
            designator_part = m_colon.group(1)
            desc_part = m_colon.group(2).strip()
        else:
            m_bullet = re.match(r"^(?:[-*•]\s*)?(\d+\s*[xх]\s+.+)$", stripped, re.IGNORECASE)
            if m_bullet:
                desc_part = m_bullet.group(1).strip()
            else:
                m_num = re.match(
                    r"^(?:\d+\.|[-*•])\s*(?:\[\s*[xX ]?\s*\]\s*)?(?:([A-Za-z][A-Za-z0-9_]*(?:\s*(?:,|\.\.|-)\s*[A-Za-z0-9_]+)*)\s*[:\-]\s*)?(.+)$",
                    stripped,
                )
                if m_num and _PROSE_COMPONENT_HINT.search(m_num.group(2)):
                    designator_part = m_num.group(1) or ""
                    desc_part = m_num.group(2).strip()
                else:
                    m_prose = re.match(r"^([^:]{2,60}):\s+(.+)$", stripped)
                    if (
                        m_prose
                        and not re.match(
                            r"^(?:ПРОЕКТ|ПРОЄКТ|PROJECT|TITLE|ОПИС|DESCRIPTION|OVERVIEW|SUMMARY|Footprint|Пін|Pin|Короткий гайд|Quick guide|Важливе питання|Note)\b",
                            m_prose.group(1).strip(),
                            re.IGNORECASE,
                        )
                        and _PROSE_COMPONENT_HINT.search(stripped)
                    ):
                        desc_part = stripped
                    else:
                        return None

    designators = _expand_designators(designator_part) if designator_part else []
    qty = _extract_quantity(stripped, designators)
    category = _classify_category(f"{designator_part} {desc_part}")
    custom_mod, external_mod = _match_module(desc_part, category, designators)
    all_external = _match_all_external_modules(desc_part, category)
    packages = _extract_packages(desc_part)

    return {
        "designators": designators,
        "name": desc_part.rstrip("."),
        "quantity": qty,
        "category": category,
        "package_hint": packages[0] if packages else None,
        "package_hints": packages,
        "kicad_footprint": None,
        "module_match": custom_mod or external_mod,
        "external_modules": all_external,
        "custom_generator_module": custom_mod,
        "connector_details": None,
        "notes": [],
        "raw_line": stripped,
    }


def _parse_pinout_line(line: str) -> dict[str, str] | None:
    """Parse pinout connection mappings between pins and external nets.

    Args:
        line: Text line declaring a pin connection.

    Returns:
        Pinout mapping dictionary or `None`.
    """
    m = re.match(
        r"^(?:Pin|Пін)\s+([A-Za-z0-9_ ]+?)(?:\s*\(([^)]+)\))?\s*(?:<-+>|->|<-|-)\s*(.+)$",
        line.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "pin": m.group(1).strip(),
        "function": (m.group(2) or "").strip(),
        "connection": m.group(3).strip(),
    }


def parse_bom_spec(source: str) -> dict[str, Any]:
    """Parse a plain-text hardware specification or file path into structured JSON.

    Args:
        source: File path or raw text string containing BOM notes.

    Returns:
        Structured specification payload with components, connector details,
        board properties, modules, pinouts, and engineering rules.
    """
    text, source_path = _load_source_text(source)
    lines = text.splitlines()

    titles: list[str] = []
    sections: list[str] = []
    revisions: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    pinout_mappings: list[dict[str, str]] = []
    wiring_rules: list[dict[str, str]] = []

    current_mode = "general"
    current_revision: dict[str, Any] | None = None
    last_component: dict[str, Any] | None = None
    last_note_kind: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            last_note_kind = None
            continue

        if set(line) <= {"=", "-", "_"}:
            if set(line) == {"="} and current_mode in ("rules", "pinout", "header"):
                current_mode = "general"
            last_note_kind = None
            continue

        m_proj = re.match(
            r"^(?:ПРОЕКТ|ПРОЄКТ|PROJECT|TITLE)\s*:?\s*(.+)$", line, re.IGNORECASE
        )
        if m_proj:
            title = m_proj.group(1).strip()
            titles.append(title)
            current_revision = {"title": title, "description": [], "sections": []}
            revisions.append(current_revision)
            current_mode = "header"
            last_component = None
            last_note_kind = None
            continue

        m_desc = re.match(
            r"^(?:ОПИС|DESCRIPTION|OVERVIEW|SUMMARY)\s*:?\s*(.+)$", line, re.IGNORECASE
        )
        if m_desc:
            current_mode = "header"
            if current_revision is not None:
                current_revision["description"].append(m_desc.group(1).strip())
            last_component = None
            last_note_kind = None
            continue

        if (
            re.match(r"^\[\d+\]\s+", line)
            or any(
                tok in line.upper()
                for tok in ("BOM-ЛИСТ", "BILL OF MATERIALS", "BOM LIST", "COMPONENT LIST")
            )
            or (current_mode in ("general", "bom") and re.match(r"^\d+\.\s+\S", line) and not _PROSE_COMPONENT_HINT.search(line))
            or re.match(
                r"^(?:Короткий гайд|Quick guide|Design guide|Layout guide|Engineering rules)\b",
                line,
                re.IGNORECASE,
            )
        ):
            sections.append(line)
            if current_revision is not None:
                current_revision["sections"].append(line)
            upper = line.upper()
            if any(
                k in upper
                for k in (
                    "ГАЙД",
                    "GUIDE",
                    "НЮАНС",
                    "ТОПОЛОГІЯ",
                    "СХЕМОТЕХНІК",
                    "ПРАВИЛА",
                    "RULES",
                    "LAYOUT",
                    "NOTES",
                )
            ):
                current_mode = "rules"
            elif "PINOUT" in upper or "ПІДКЛЮЧЕННЯ" in upper:
                current_mode = "pinout"
            else:
                current_mode = "bom"
            last_component = None
            last_note_kind = None
            continue

        if current_mode == "header":
            if current_revision is not None:
                current_revision["description"].append(line)
            continue

        if (
            last_note_kind is not None
            and raw_line[:1].isspace()
            and not line.startswith(("!", "[]", "[x]", "[X]"))
            and not re.match(r"^(?:Pin|Пін|Footprint)\b", line, re.IGNORECASE)
        ):
            if last_note_kind == "component" and last_component is not None and last_component["notes"]:
                last_component["notes"][-1] = f"{last_component['notes'][-1]} {line}"
                continue
            if last_note_kind == "rule" and wiring_rules:
                wiring_rules[-1]["text"] = f"{wiring_rules[-1]['text']} {line}"
                wiring_rules[-1]["rule_type"] = _classify_rule(wiring_rules[-1]["text"])
                continue

        m_fp = re.match(r"^Footprint(?:\s+у\s+KiCad)?\s*:\s*(.+)$", line, re.IGNORECASE)
        if m_fp and last_component is not None:
            fp_val = m_fp.group(1).strip().rstrip(".")
            fp_clean = re.sub(r"\s*\([^)]*\)\s*$", "", fp_val).strip()
            last_component["kicad_footprint"] = fp_clean or fp_val
            if fp_val != fp_clean:
                last_component["notes"].append(fp_val)
            last_note_kind = None
            continue

        if line.startswith("!"):
            note_text = line.lstrip("!").strip()
            if current_mode == "bom" and last_component is not None:
                last_component["notes"].append(note_text)
                last_note_kind = "component"
            else:
                wiring_rules.append(
                    {
                        "rule_type": _classify_rule(note_text),
                        "section": current_mode,
                        "text": note_text,
                    }
                )
                last_note_kind = "rule"
            continue

        last_note_kind = None

        if current_mode == "bom" and _is_bom_subheader(line):
            last_component = None
            continue

        pin_map = _parse_pinout_line(line)
        if pin_map is not None:
            pinout_mappings.append(pin_map)
            continue

        if current_mode in ("bom", "general"):
            comp = _parse_component_line(line)
            if comp is not None:
                components.append(comp)
                last_component = comp
                continue

        if current_mode == "rules":
            wiring_rules.append(
                {
                    "rule_type": _classify_rule(line),
                    "section": "rules",
                    "text": line,
                }
            )
        elif current_mode == "general" and re.match(r"^(?:\d+\.|[-•])\s+", line):
            if any(pat.search(line) for pat, _ in _RULE_KEYWORDS):
                wiring_rules.append(
                    {
                        "rule_type": _classify_rule(line),
                        "section": "general",
                        "text": line,
                    }
                )

    for comp in components:
        conn_info = _extract_connector_details(comp["name"], comp["raw_line"], text)
        if conn_info is not None:
            comp["connector_details"] = conn_info
            if comp["category"] == "other":
                comp["category"] = "connector"

    board_properties = _extract_board_properties(text, components)

    custom_modules_detected: list[str] = []
    external_modules_detected: list[str] = []
    for comp in components:
        cm = comp.get("custom_generator_module")
        if cm and cm not in custom_modules_detected:
            custom_modules_detected.append(cm)
        ext_mods = comp.get("external_modules") or (
            [comp["module_match"]] if comp.get("module_match") and not cm else []
        )
        for mod in ext_mods:
            if mod and mod != cm and mod not in external_modules_detected:
                external_modules_detected.append(mod)

    return {
        "status": "ok",
        "source_path": source_path,
        "titles": titles,
        "revisions": revisions,
        "sections": sections,
        "components": components,
        "board_properties": board_properties,
        "custom_modules_detected": custom_modules_detected,
        "external_modules_detected": external_modules_detected,
        "pinout_mappings": pinout_mappings,
        "wiring_rules": wiring_rules,
        "summary": {
            "total_line_items": len(components),
            "total_component_count": sum(c["quantity"] for c in components),
            "custom_modules_count": len(custom_modules_detected),
            "pinout_count": len(pinout_mappings),
            "rules_count": len(wiring_rules),
        },
    }
