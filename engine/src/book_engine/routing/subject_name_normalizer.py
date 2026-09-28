from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import List

_DASHES = str.maketrans({"－": "-", "—": "-", "–": "-", "‑": "-", "﹣": "-"})
_BRACKETS = str.maketrans({"（": "(", "）": ")", "【": "[", "】": "]"})
_TABLE_PREFIX_RE = re.compile(r"^\s*表\s*\d+(?:[-.－—]\d+)*\s*", re.IGNORECASE)
# Only remove list markers that are unambiguously delimiters.  Dotted section
# numbers are stripped by heading_subject_resolver, where chemical locants such
# as ``3,3-`` and ``2,4,6-`` can be protected with full context.
_LIST_PREFIX_RE = re.compile(
    r"^\s*(?:[（(]?\d+[）)、]|[（(]?[A-Za-z][）)、．.]|\d+(?:[.．]\d+){1,6}(?=\s))\s*"
)
_ORPHAN_LEADING_CLOSER_RE = re.compile(r"^[\s)\]}>）】]+")
# Attached numeric parentheses are often grade/molecular-weight/sample identity
# (GAP-PEG(200), 共聚物(1)), not citations.  Strip square citations and only
# whitespace-separated parenthetical references.
_TRAILING_CITATION_RE = re.compile(
    r"(?:(?:\s*[\[【]\s*\d+(?:\s*[-,，、]\s*\d+)*\s*[\]】])|"
    r"(?:\s+[（(]\s*\d+(?:\s*[-,，、]\s*\d+)*\s*[）)]))+\s*$"
)
_SPACE_AROUND_PUNCT_RE = re.compile(r"\s*([/+,:;()\[\]-])\s*")
_MULTI_SPACE_RE = re.compile(r"\s+")

_CJK_SPACED_SURFACE_RE = re.compile(r"^[\u4e00-\u9fff]+(?:\s+[\u4e00-\u9fff]+)+$")

_DESCRIPTIVE_TOPIC_SUFFIX_RE = re.compile(
    r"^(?P<entity>.+?)的(?:官能度分布|分子量分布|粒度分布|粒径分布|性能参数|性能指标|"
    r"物理性能|化学性能|热性能|力学性能|燃烧性能|爆轰性能|实验结果|试验结果|"
    r"组成及性能|组成与性能|结构与性能|合成方法|制备方法)$"
)
_ENTITYISH_PREFIX_RE = re.compile(
    r"(?:火药|推进剂|炸药|药剂|材料|化合物|氧化物|聚合物|预聚物|共聚物|均聚物|"
    r"橡胶|树脂|粘合剂|黏合剂|体系|粉|剂|物)$|"
    r"^(?:[A-Za-z][A-Za-z0-9+._/\-()]{1,40}|某[A-Za-z0-9+._/\-()一-鿿]{2,40})$",
    re.I,
)


@dataclass
class NormalizedSubjectName:
    raw_name: str
    canonical_name: str
    normalized_key: str
    aliases: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _collapse_short_cjk_ocr_spacing(value: str) -> str:
    stripped = value.strip()
    if not _CJK_SPACED_SURFACE_RE.fullmatch(stripped):
        return value
    parts = re.split(r"\s+", stripped)
    compact = "".join(parts)
    # OCR often inserts spaces between the characters of short entity names
    # (电 雷 管、火 帽、点火 具).  Do not collapse long multi-name headers.
    if len(compact) <= 6 and any(len(part) == 1 for part in parts):
        return compact
    return value


def _clean_surface(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.translate(_DASHES).translate(_BRACKETS)
    value = _TABLE_PREFIX_RE.sub("", value)
    value = _LIST_PREFIX_RE.sub("", value)
    value = _ORPHAN_LEADING_CLOSER_RE.sub("", value)
    value = _TRAILING_CITATION_RE.sub("", value)
    value = value.strip(" \t\r\n,，。;；:：、|")
    value = _collapse_short_cjk_ocr_spacing(value)
    value = _MULTI_SPACE_RE.sub(" ", value)
    value = _SPACE_AROUND_PUNCT_RE.sub(r"\1", value)
    return value.strip()


def _extract_parenthetical_aliases(value: str) -> List[str]:
    aliases: List[str] = []
    for inner in re.findall(r"\(([^()]{1,50})\)", value):
        for part in re.split(r"[,，;/；]", inner):
            candidate = _clean_surface(part)
            if not candidate or re.fullmatch(r"\d+(?:[-,，、]\d+)*", candidate):
                continue
            if re.search(r"(?:℃|MPa|GPa|kPa|g/cm|kg/m|mm/s|m/s|km/s|J/g|kJ|%|‰)$", candidate, re.I):
                continue
            if 1 < len(candidate) <= 30 and re.search(r"[A-Za-z0-9\u4e00-\u9fff]", candidate):
                aliases.append(candidate)
    return list(dict.fromkeys(aliases))


def normalize_subject_name(raw_name: str, *, trim_descriptive_topic: bool = True) -> NormalizedSubjectName:
    notes: List[str] = []
    raw_nfkc = unicodedata.normalize("NFKC", raw_name or "").translate(_DASHES).translate(_BRACKETS)
    duplicate_match = re.match(r"^\s*(.+?)\s+/\s+\1\s*$", raw_nfkc)
    if duplicate_match:
        value = _clean_surface(duplicate_match.group(1))
        notes.append("collapsed_duplicate_path")
    else:
        value = _clean_surface(raw_nfkc)
    descriptive = _DESCRIPTIVE_TOPIC_SUFFIX_RE.fullmatch(value) if trim_descriptive_topic else None
    if descriptive:
        entity = _clean_surface(descriptive.group("entity"))
        if entity and _ENTITYISH_PREFIX_RE.search(entity):
            notes.append("trimmed_descriptive_topic_suffix")
            value = entity

    aliases = _extract_parenthetical_aliases(value)

    # Canonicalize common OCR spacing in Latin abbreviations while preserving Chinese names.
    if re.fullmatch(r"[A-Za-z0-9._+\-/ ]{2,40}", value):
        compact = re.sub(r"\s+", "", value)
        if compact != value:
            notes.append("removed_latin_internal_spaces")
        value = compact

    key = unicodedata.normalize("NFKC", value).casefold()
    key = re.sub(r"\s+", "", key)
    key = key.translate(_DASHES)
    key = re.sub(r"[^0-9a-z\u4e00-\u9fffα-ω]+", "", key)

    aliases = [alias for alias in aliases if alias != value]
    return NormalizedSubjectName(
        raw_name=raw_name or "",
        canonical_name=value,
        normalized_key=key,
        aliases=aliases,
        notes=notes,
    )


__all__ = ["NormalizedSubjectName", "normalize_subject_name"]
