from __future__ import annotations

import math
import re
from typing import Optional, Tuple

from book_engine.core.schemas import ParsedValue

_SPACE_RE = re.compile(r"\s+")
_OCR_DECIMAL_SPACE_RE = re.compile(r"(?<=\d)\s*[.．]\s+(?=\d)")
_NUMBER = r"[-+−]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?"
_RANGE_RE = re.compile(rf"(?P<a>{_NUMBER})\s*(?:~|～|—|–|至|到|\.\.)\s*(?P<b>{_NUMBER})")
_PLUS_MINUS_RE = re.compile(rf"(?P<center>{_NUMBER})\s*±\s*(?P<delta>{_NUMBER})")
_COMPARATOR_RE = re.compile(r"^\s*(?P<cmp>>=|<=|>|<|≥|≤|≈|约|不小于|不大于|大于|小于|至少|至多)\s*")
_RATIO_RE = re.compile(rf"(?P<a>{_NUMBER})\s*[:：/]\s*(?P<b>{_NUMBER})")
_FIRST_NUMBER_RE = re.compile(_NUMBER)
_UNIT_AFTER_RE = re.compile(
    rf"{_NUMBER}(?:\s*(?:~|～|—|–|至|到|\.\.|±)\s*{_NUMBER})?\s*"
    r"(?P<unit>°C/min|℃/min|K/min|N(?:·|\.|•)?s/kg|N(?:·|\.|•)?s/g|"
    r"g/cm(?:3|³)|kg/m(?:3|³)|kJ/cm3|kJ/mol|kJ/kg|mol/L|mm/s|cm/s|km/s|m/s|"
    r"Pa·s|Pa\.s|r/min|kHz|MHz|GPa|MPa|kPa|mbar|bar|Pa|"
    r"J/g|cal/g|mg/L|g/L|rpm|Hz|ppm|ppb|vol%|wt%|μg|ug|mg|kg|nm|μm|um|mm|cm|"
    r"ns|μs|us|ms|min|℃|°C|‰|%|目|K|V|mV|kV|A|mA|g|m|s|h|d)?"
    r"(?=$|[^A-Za-z/])",
    re.IGNORECASE,
)


def _to_float(text: str) -> Optional[float]:
    try:
        value = text.replace("−", "-").replace(",", "")
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_value_text(text: str) -> str:
    value = (text or "").strip()
    value = value.replace("−", "-").replace("⁻", "-").replace("—", "–")
    value = value.replace("•", "·").replace("×", "*")
    value = re.sub(r"kg\s*(?:·|\*)?\s*m\s*\^?\s*-\s*3\b", "kg/m3", value, flags=re.I)
    value = re.sub(r"g\s*(?:·|\*)?\s*cm\s*\^?\s*-\s*3\b", "g/cm3", value, flags=re.I)
    value = re.sub(r"kJ\s*(?:·|\*)?\s*cm\s*\^?\s*-\s*3\b", "kJ/cm3", value, flags=re.I)
    value = re.sub(r"kJ\s*(?:·|\*)?\s*mol\s*\^?\s*-\s*1\b", "kJ/mol", value, flags=re.I)
    value = re.sub(r"J\s*(?:·|\*)?\s*g\s*\^?\s*-\s*1\b", "J/g", value, flags=re.I)
    value = re.sub(r"(?P<u>km|cm|mm|m)\s*(?:·|\*)\s*s\s*\^?\s*-\s*1\b", r"\g<u>/s", value, flags=re.I)
    value = re.sub(r"μ\s*m\b", "μm", value, flags=re.I)
    value = re.sub(r"u\s*m\b", "um", value, flags=re.I)
    value = re.sub(r"(?<=\d),(?=\d{1,2}(?:\D|$))", ".", value)
    # OCR often inserts whitespace around a decimal point (for example
    # ``1. 32`` or ``1 . 32``). Repair only digit-dot-digit surfaces;
    # malformed multi-number strings remain detectable by the downstream
    # concatenated-series guard.
    value = _OCR_DECIMAL_SPACE_RE.sub(".", value)
    value = _SPACE_RE.sub(" ", value)
    return value


def normalize_unit(unit: str) -> str:
    value = (unit or "").strip()
    if not value:
        return ""
    value = value.replace("−", "-").replace("⁻", "-")
    value = value.replace("° C", "°C").replace("degC", "℃")
    value = re.sub(r"\s+", "", value)
    value = value.replace("•", "·").replace("×", "*")
    value = re.sub(r"^kg(?:·|\*)?m\^?-?3$", "kg/m3", value, flags=re.I)
    value = re.sub(r"^g(?:·|\*)?cm\^?-?3$", "g/cm3", value, flags=re.I)
    value = re.sub(r"^kJ(?:·|\*)?cm\^?-?3$", "kJ/cm3", value, flags=re.I)
    value = re.sub(r"^kJ(?:·|\*)?mol\^?-?1$", "kJ/mol", value, flags=re.I)
    value = re.sub(r"^J(?:·|\*)?g\^?-?1$", "J/g", value, flags=re.I)
    value = re.sub(r"^(km|cm|mm|m)(?:·|\*)s\^?-?1$", r"\1/s", value, flags=re.I)
    value = re.sub(r"^N(?:·|\*)s(?:/|·|\*)kg\^?-?1$", "N·s/kg", value, flags=re.I)
    value = value.replace("μ", "u")
    return value


def _extract_unit(text: str, header_unit: str = "") -> str:
    if header_unit:
        return normalize_unit(header_unit)
    matches = list(_UNIT_AFTER_RE.finditer(text))
    if not matches:
        return ""
    unit = matches[-1].group("unit") or ""
    return normalize_unit(unit)


def parse_value(text: str, header_unit: str = "") -> ParsedValue:
    raw = text or ""
    normalized = normalize_value_text(raw)
    result = ParsedValue(raw_text=raw, normalized_text=normalized)
    if not normalized:
        return result

    comparator = ""
    comparator_match = _COMPARATOR_RE.match(normalized)
    body = normalized
    if comparator_match:
        comparator = comparator_match.group("cmp")
        body = normalized[comparator_match.end():].strip()
    comparator_map = {
        ">=": ">=", "≥": ">=", "不小于": ">=", "至少": ">=",
        "<=": "<=", "≤": "<=", "不大于": "<=", "至多": "<=",
        ">": ">", "大于": ">", "<": "<", "小于": "<",
        "≈": "≈", "约": "≈",
    }
    result.comparator = comparator_map.get(comparator, comparator)

    ratio_match = _RATIO_RE.search(body)
    if ratio_match and not re.search(r"(?:https?|[A-Za-z]):/", body):
        result.is_ratio = True
        result.lower_bound = _to_float(ratio_match.group("a"))
        result.upper_bound = _to_float(ratio_match.group("b"))
        result.unit = _extract_unit(body, header_unit)
        result.parse_confidence = 0.9
        return result

    plus_minus = _PLUS_MINUS_RE.search(body)
    if plus_minus:
        center = _to_float(plus_minus.group("center"))
        delta = _to_float(plus_minus.group("delta"))
        result.value_num = center
        if center is not None and delta is not None:
            result.lower_bound = center - delta
            result.upper_bound = center + delta
        result.is_range = True
        result.unit = _extract_unit(body, header_unit)
        result.parse_confidence = 0.95
        return result

    range_match = _RANGE_RE.search(body)
    if range_match:
        result.lower_bound = _to_float(range_match.group("a"))
        result.upper_bound = _to_float(range_match.group("b"))
        result.is_range = True
        result.unit = _extract_unit(body, header_unit)
        result.parse_confidence = 0.95
        return result

    number_match = _FIRST_NUMBER_RE.search(body)
    if number_match:
        result.value_num = _to_float(number_match.group(0))
        result.unit = _extract_unit(body, header_unit)
        result.parse_confidence = 0.88
        return result

    result.unit = normalize_unit(header_unit)
    result.parse_confidence = 0.25
    return result


__all__ = ["parse_value", "normalize_value_text", "normalize_unit"]
