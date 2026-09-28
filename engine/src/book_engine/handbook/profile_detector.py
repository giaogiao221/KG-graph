from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.handbook.entry_parser import parse_entries

_FIELD_RE = re.compile(
    r"(?:化学式|分子式|结构式|相对分子质量|相对分子量|CAS\s*(?:号|登记号)?|"
    r"\[(?:理化性质|用途|毒性|储存运输条件|感度性能|燃烧爆炸性能)\]|标准生成热)\s*[：:]?",
    re.I,
)
_CODE_RE = re.compile(r"(?<!\d)([123]\d{5})(?!\d)")
_SPEC_ENTRY_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*\d+(?:\s*[.．]\s*\d+){1,4}\s*[^#\n]+$",
    re.M,
)
_SPEC_TABLE_RE = re.compile(r"(?:理化指标和检验方法|技术指标和检验方法|质量指标和检验方法)", re.I)
_SPEC_IDENTITY_RE = re.compile(r"(?:中文名称|英文名称|分子式|化学式|分子量|CAS\s*(?:登记号|号)?)\s*[：:]", re.I)
_SPEC_SUBSECTION_RE = re.compile(r"^\s*#{1,6}\s*(?:1[.．、]?\s*)?(?:物理性质|化学性质)|^\s*#{1,6}\s*(?:3[.．、]?\s*)?理化指标", re.M)


@dataclass(frozen=True)
class DocumentProfileDecision:
    profile: str
    confidence: float
    entry_count: int
    unique_entry_codes: int
    duplicate_entry_codes: int
    field_label_hits: int
    code_hits: int
    reasons: tuple[str, ...]
    specification_entry_heading_hits: int = 0
    specification_table_hits: int = 0
    specification_identity_hits: int = 0

    @property
    def is_repeated_entry_handbook(self) -> bool:
        return self.profile == "repeated_entry_handbook"

    @property
    def is_specification_handbook(self) -> bool:
        return self.profile == "specification_handbook"


def _count_spec_entry_headings(text: str) -> int:
    lines = text.splitlines()
    count = 0
    for index, line in enumerate(lines):
        if not _SPEC_ENTRY_HEADING_RE.match(line.replace("$", "")):
            continue
        following = "\n".join(lines[index + 1 : min(len(lines), index + 35)])
        if len(_SPEC_IDENTITY_RE.findall(following)) >= 2:
            count += 1
    return count


def detect_document_profile(document: MarkdownDocument) -> DocumentProfileDecision:
    text = document.text
    entries = parse_entries(text)
    codes = [entry.entry_id for entry in entries]
    unique_codes = len(set(codes))
    duplicates = max(0, len(codes) - unique_codes)
    field_hits = len(_FIELD_RE.findall(text))
    code_hits = len(_CODE_RE.findall(text))
    spec_entry_hits = _count_spec_entry_headings(text)
    spec_table_hits = len(_SPEC_TABLE_RE.findall(text))
    spec_identity_hits = len(_SPEC_IDENTITY_RE.findall(text))
    spec_subsection_hits = len(_SPEC_SUBSECTION_RE.findall(text))

    repeated_reasons: list[str] = []
    repeated_score = 0.0
    if len(entries) >= 30:
        repeated_score += 0.45
        repeated_reasons.append("many_repeated_six_digit_entry_headers")
    if unique_codes >= 30 and unique_codes / max(1, len(entries)) >= 0.95:
        repeated_score += 0.20
        repeated_reasons.append("entry_codes_are_nearly_unique")
    if field_hits >= max(50, len(entries) * 2):
        repeated_score += 0.25
        repeated_reasons.append("repeated_identity_and_property_field_template")
    if code_hits >= len(entries) and entries:
        repeated_score += 0.05
        repeated_reasons.append("document_contains_dense_entry_code_inventory")
    if len(entries) >= 200:
        repeated_score += 0.05
        repeated_reasons.append("large_reference_handbook_scale")

    spec_reasons: list[str] = []
    spec_score = 0.0
    if spec_entry_hits >= 20:
        spec_score += 0.45
        spec_reasons.append("many_numbered_material_sections_with_identity_fields")
    if spec_table_hits >= 10:
        spec_score += 0.25
        spec_reasons.append("repeated_project_indicator_method_tables")
    if spec_identity_hits >= max(30, spec_entry_hits * 2):
        spec_score += 0.20
        spec_reasons.append("repeated_material_identity_preamble")
    if spec_subsection_hits >= 20:
        spec_score += 0.10
        spec_reasons.append("repeated_property_preparation_application_subsections")

    if repeated_score >= 0.75:
        profile = "repeated_entry_handbook"
        confidence = min(0.99, repeated_score)
        reasons = repeated_reasons
    elif spec_score >= 0.75:
        profile = "specification_handbook"
        confidence = min(0.99, spec_score)
        reasons = spec_reasons
    else:
        profile = "narrative_monograph"
        confidence = max(0.55, 1.0 - max(repeated_score, spec_score))
        reasons = []
        if code_hits:
            reasons.append("document_contains_entry_like_codes_but_no_stable_template")
        if field_hits:
            reasons.append("document_contains_property_fields_but_no_profile_threshold")
        if not reasons:
            reasons.append("no_repeated_entry_or_specification_template_detected")

    return DocumentProfileDecision(
        profile=profile,
        confidence=confidence,
        entry_count=len(entries),
        unique_entry_codes=unique_codes,
        duplicate_entry_codes=duplicates,
        field_label_hits=field_hits,
        code_hits=code_hits,
        reasons=tuple(reasons),
        specification_entry_heading_hits=spec_entry_hits,
        specification_table_hits=spec_table_hits,
        specification_identity_hits=spec_identity_hits,
    )


def write_document_profile_report(output_dir: Path, decision: DocumentProfileDecision) -> dict[str, object]:
    stage_dir = output_dir / "step_document_profile"
    stage_dir.mkdir(parents=True, exist_ok=True)
    payload = {"ok": True, "stage": "document_profile_detection_v2_phase102", **asdict(decision)}
    (stage_dir / "document_profile_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


__all__ = ["DocumentProfileDecision", "detect_document_profile", "write_document_profile_report"]
