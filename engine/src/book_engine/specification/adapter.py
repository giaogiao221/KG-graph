from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Sequence, Tuple

from book_engine.core.schemas import (
    ConditionAtom,
    ConditionalFactRecord,
    SourceLocation,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
)
from book_engine.document.block_segmenter import TextBlock
from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.gates.table_record_gate import RecordGateDecision
from book_engine.handbook.common import normalize_math_text, normalize_subject_name
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.tables.value_parser import normalize_unit, parse_value
from book_engine.text.text_fact_extractor import TextFactRecord, extract_text_facts

from .parser import SpecificationEntry, entry_for_line, parse_specification_entries, split_entry_subsections

_GENERIC_TABLE_HEADERS = {
    "项目", "理化指标", "技术指标", "指标", "要求", "检验方法", "试验方法", "方法", "备注",
}
_GRADE_HEADER_RE = re.compile(r"(?:分析纯|化学纯|优级纯|一级品|合格品|优等品|一等品|等级|级别|A类|B类|I级|II级|Ⅲ级)", re.I)
_PLACEHOLDER_RE = re.compile(r"^(?:[一二三四五六七八九十]+|[-—–~～/\\]|无数据|未测|未检出|N/?A)$", re.I)
_QUALITATIVE_PROPERTIES = {"外观", "颜色", "状态", "气味", "溶解性", "形态", "性状"}
_UNIT_SUFFIX_RE = re.compile(
    r"(?:/|／)\s*(?:[（(]\s*)?(?P<unit>%|‰|℃|°C|K|g/cm(?:3|³)|kg/m(?:3|³)|"
    r"kJ/mol|kJ/kg|J/mol|J/kg|mPa·s|Pa·s|mm/s|cm/s|m/s|MPa|kPa|GPa|"
    r"μm|um|nm|mm|cm|m|s|min|h|d|目)(?:\s*[）)])?\s*$",
    re.I,
)

_ELEMENT_NAME_ZH = {
    "H": "氢", "He": "氦", "Li": "锂", "Be": "铍", "B": "硼", "C": "碳", "N": "氮", "O": "氧",
    "F": "氟", "Ne": "氖", "Na": "钠", "Mg": "镁", "Al": "铝", "Si": "硅", "P": "磷", "S": "硫",
    "Cl": "氯", "Ar": "氩", "K": "钾", "Ca": "钙", "Sc": "钪", "Ti": "钛", "V": "钒", "Cr": "铬",
    "Mn": "锰", "Fe": "铁", "Co": "钴", "Ni": "镍", "Cu": "铜", "Zn": "锌", "Ga": "镓", "Ge": "锗",
    "As": "砷", "Se": "硒", "Br": "溴", "Kr": "氪", "Rb": "铷", "Sr": "锶", "Y": "钇", "Zr": "锆",
    "Nb": "铌", "Mo": "钼", "Tc": "锝", "Ru": "钌", "Rh": "铑", "Pd": "钯", "Ag": "银", "Cd": "镉",
    "In": "铟", "Sn": "锡", "Sb": "锑", "Te": "碲", "I": "碘", "Xe": "氙", "Cs": "铯", "Ba": "钡",
    "La": "镧", "Ce": "铈", "Pr": "镨", "Nd": "钕", "Sm": "钐", "Eu": "铕", "Gd": "钆", "Tb": "铽",
    "Dy": "镝", "Ho": "钬", "Er": "铒", "Tm": "铥", "Yb": "镱", "Lu": "镥", "Hf": "铪", "Ta": "钽",
    "W": "钨", "Re": "铼", "Os": "锇", "Ir": "铱", "Pt": "铂", "Au": "金", "Hg": "汞", "Tl": "铊",
    "Pb": "铅", "Bi": "铋", "Th": "钍", "U": "铀",
}
_CHEMICAL_TOKEN_RE = re.compile(r"^(?:[A-Z][a-z]?\d*)+(?:[·.]\d*(?:[A-Z][a-z]?\d*)+)*$")


def _normalize_spec_property(property_name: str, unit: str) -> str:
    """Preserve chemical-analysis rows as content properties, never identity labels.

    Specification matrices frequently use bare symbols (As, Pb, Fe) or a
    component formula (Sb2O3) in the project column with percent values.  A
    generic ontology alias can otherwise confuse ``As`` with ``CAS``.  The
    table grammar itself is decisive here: formula/symbol + percent means a
    composition/impurity content property.
    """
    compact = re.sub(r"\s+", "", property_name or "")
    if unit != "%" or not compact:
        return property_name
    if compact in _ELEMENT_NAME_ZH:
        return f"{_ELEMENT_NAME_ZH[compact]}含量"
    if _CHEMICAL_TOKEN_RE.fullmatch(compact):
        return f"{compact}含量"
    return property_name

_SECTION_ROLE_MAP = {
    "身份信息": "material_profile",
    "物理性质": "property",
    "化学性质": "property",
    "理化指标和检验方法": "property",
    "制备方法": "process",
    "储存、运输和应用": "application_safety",
    "储存运输和应用": "application_safety",
    "毒性与防护": "application_safety",
    "理化分析谱图": "unknown",
    "参考文献": "unknown",
}


def _stable_id(*parts: object, prefix: str) -> str:
    digest = hashlib.sha1("|".join(str(x) for x in parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _role_for_title(title: str) -> str:
    compact = re.sub(r"\s+", "", title or "")
    for key, role in _SECTION_ROLE_MAP.items():
        if key in compact:
            return role
    if re.search(r"(?:制备|合成|工艺|方法)", compact):
        return "process"
    if re.search(r"(?:应用|储存|运输|毒性|防护|安全)", compact):
        return "application_safety"
    if re.search(r"(?:性质|性能|指标|参数)", compact):
        return "property"
    return "material_profile"


def _make_anchor(entry: SpecificationEntry, block_id: str) -> TextSubjectAnchor:
    return TextSubjectAnchor(
        block_id=block_id,
        subject=entry.canonical_subject,
        subject_type="材料",
        source="specification_entry_header",
        confidence=0.97,
        status="confirmed",
        reasons=["specification_entry_scope"],
    )


def build_specification_text_layer(
    document: MarkdownDocument,
    *,
    output_dir: Path,
) -> Tuple[List[TextBlock], List[TextSubjectAnchor], Dict[str, TextSubjectAnchor], List[TextFactRecord], dict[str, object], List[SpecificationEntry]]:
    entries = parse_specification_entries(document)
    blocks: List[TextBlock] = []
    anchors: List[TextSubjectAnchor] = []
    anchor_by_block: Dict[str, TextSubjectAnchor] = {}

    for entry in entries:
        for ordinal, subsection in enumerate(split_entry_subsections(entry, document), start=1):
            # Spectra and references contain visual/bibliographic text but almost
            # no self-contained material facts. Keeping them out of the direct
            # entry stream avoids captions becoming entities while preserving the
            # original source file for audit.
            if subsection.title in {"理化分析谱图", "参考文献"}:
                continue
            block_id = f"SPEC:{entry.section_id}:{ordinal}"
            block = TextBlock(
                block_id=block_id,
                text=subsection.text,
                line_start=subsection.line_start,
                line_end=subsection.line_end,
                heading_path=[f"{entry.section_id} {entry.canonical_subject}", subsection.title],
                heading_title=subsection.title,
                heading_level=2,
                role=_role_for_title(subsection.title),
                role_confidence=0.96,
                role_reasons=["specification_entry_subsection"],
            )
            anchor = _make_anchor(entry, block_id)
            blocks.append(block)
            anchors.append(anchor)
            anchor_by_block[block_id] = anchor

    records = extract_text_facts(blocks, anchor_by_block)
    # The owner is structurally fixed by the material entry. Preserve any
    # explicit local owner found by the extractor, but replace only generic or
    # empty fallbacks with the entry anchor.
    for record in records:
        anchor = anchor_by_block.get(record.block_id)
        if anchor and (not record.subject or record.subject in {"材料", "体系", "功能材料"}):
            record.subject = anchor.subject
            record.subject_type = "材料"
            record.owner_source = "specification_entry_scope"
            record.anchor_source = "specification_entry_header"

    stage_dir = output_dir / "step_specification_handbook"
    stage_dir.mkdir(parents=True, exist_ok=True)
    registry_fields = [
        "section_id", "canonical_subject", "english_name_raw", "line_start", "line_end", "title_raw",
    ]
    with (stage_dir / "specification_entry_registry.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=registry_fields, delimiter="\t")
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "section_id": entry.section_id,
                "canonical_subject": entry.canonical_subject,
                "english_name_raw": entry.english_name_raw,
                "line_start": entry.start_line,
                "line_end": entry.end_line,
                "title_raw": entry.title_raw,
            })

    report = {
        "ok": True,
        "stage": "specification_handbook_text_scope_v2_phase102",
        "entries_detected": len(entries),
        "entry_blocks": len(blocks),
        "text_fact_candidates": len(records),
        "entries_with_english_name": sum(bool(entry.english_name_raw) for entry in entries),
        "registry_path": str(stage_dir / "specification_entry_registry.tsv"),
        "policy": "Numbered material headings with repeated identity fields define hard local subjects; subsection titles never become material subjects.",
    }
    return blocks, anchors, anchor_by_block, records, report, entries


def _cell_text(grid: TableGrid, row: int, col: int) -> str:
    if row < 0 or row >= grid.row_count or col < 0 or col >= grid.column_count:
        return ""
    cell = grid.cells[row][col]
    return (cell.normalized_text or cell.raw_text or "").strip()


def _header_layout(grid: TableGrid) -> tuple[int, int, List[int], int, Dict[int, str]] | None:
    search_rows = min(3, grid.row_count)
    project_col = -1
    method_col = -1
    header_start = -1
    for row in range(search_rows):
        for col in range(grid.column_count):
            text = re.sub(r"\s+", "", _cell_text(grid, row, col))
            if text == "项目" and project_col < 0:
                project_col, header_start = col, row
            if re.search(r"(?:检验|试验|测试|测定)方法|^方法$", text) and method_col < 0:
                method_col = col
    if project_col < 0:
        return None

    data_start = header_start + 1
    while data_start < grid.row_count:
        project = re.sub(r"\s+", "", _cell_text(grid, data_start, project_col))
        row_values = [re.sub(r"\s+", "", _cell_text(grid, data_start, c)) for c in range(grid.column_count)]
        if project and project not in _GENERIC_TABLE_HEADERS and not all(v in _GENERIC_TABLE_HEADERS or _GRADE_HEADER_RE.fullmatch(v or "") for v in row_values if v):
            break
        data_start += 1
    if data_start >= grid.row_count:
        return None

    indicator_cols = [c for c in range(grid.column_count) if c not in {project_col, method_col}]
    if not indicator_cols:
        return None
    grade_by_col: Dict[int, str] = {}
    for col in indicator_cols:
        labels: List[str] = []
        for row in range(header_start, data_start):
            value = re.sub(r"\s+", "", _cell_text(grid, row, col))
            if not value or value in _GENERIC_TABLE_HEADERS:
                continue
            if value not in labels:
                labels.append(value)
        if labels:
            grade_by_col[col] = "/".join(labels)
    return project_col, method_col, indicator_cols, data_start, grade_by_col


def _property_and_unit(raw: str) -> tuple[str, str]:
    value = normalize_math_text(raw).strip(" ：:，,;；")
    unit = ""
    match = _UNIT_SUFFIX_RE.search(value)
    if match:
        unit = normalize_unit(match.group("unit") or "")
        value = value[: match.start()].strip(" /／（(")
    # A bare trailing percent is a unit, not part of the property name.
    if value.endswith("%"):
        value = value[:-1].rstrip("/／")
        unit = unit or "%"
    value = re.sub(r"\s+", "", value)
    return value, unit


def _safe_spec_value(property_name: str, value: str, parsed: object) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    if not compact or _PLACEHOLDER_RE.fullmatch(compact):
        return False
    has_numeric = any(
        getattr(parsed, key, None) is not None
        for key in ("value_num", "lower_bound", "upper_bound")
    )
    if property_name in _QUALITATIVE_PROPERTIES:
        return len(compact) <= 160
    # Specification tables may contain identity-like formulas or textual
    # requirements. They are retained only when the project label clearly says
    # formula/name; all other indicator values need a scalar/range/comparator.
    if re.search(r"(?:分子式|化学式|名称|牌号)", property_name):
        return len(compact) <= 100
    return has_numeric


def build_specification_table_layer(
    structure_results: Sequence[Tuple[TableBlock, TableGrid, object]],
    entries: Sequence[SpecificationEntry],
    *,
    output_dir: Path,
) -> Tuple[List[Tuple[ConditionalFactRecord, RecordGateDecision, TableSemanticPlan]], List[RecordGateDecision], dict[str, object]]:
    accepted: List[Tuple[ConditionalFactRecord, RecordGateDecision, TableSemanticPlan]] = []
    decisions: List[RecordGateDecision] = []
    audit_rows: List[dict[str, object]] = []
    tables_matched = 0

    for block, grid, _header_tree in structure_results:
        entry = entry_for_line(list(entries), block.line_start)
        if entry is None:
            continue
        context = " ".join([block.heading, block.preceding_text, _cell_text(grid, 0, 0)])
        layout = _header_layout(grid)
        if layout is None or not re.search(r"(?:理化指标|技术指标|质量指标|检验方法|试验方法)", context + " " + " ".join(_cell_text(grid, 0, c) for c in range(grid.column_count))):
            continue
        tables_matched += 1
        project_col, method_col, indicator_cols, data_start, grade_by_col = layout
        plan = TableSemanticPlan(
            table_id=block.table_id,
            topology="specification_matrix",
            orientation="row_property",
            confidence=0.98,
            unresolved_reasons=[],
        )
        for row_index in range(data_start, grid.row_count):
            property_raw = _cell_text(grid, row_index, project_col)
            property_name, header_unit = _property_and_unit(property_raw)
            property_name = _normalize_spec_property(property_name, header_unit)
            if not property_name or property_name in _GENERIC_TABLE_HEADERS or property_name.startswith("注"):
                continue
            method = _cell_text(grid, row_index, method_col) if method_col >= 0 else ""
            for col in indicator_cols:
                value = _cell_text(grid, row_index, col)
                if not value:
                    continue
                parsed = parse_value(value, header_unit)
                safe = _safe_spec_value(property_name, value, parsed)
                grade = grade_by_col.get(col, "")
                conditions: List[ConditionAtom] = []
                if grade:
                    conditions.append(ConditionAtom(
                        name="规格等级",
                        normalized_name="规格等级",
                        condition_type="specification_grade",
                        value_text=grade,
                        scope="column",
                        confidence=0.98,
                        source_kind="column_header",
                        source_text=grade,
                        target_row=row_index,
                        target_column=col,
                    ))
                record_id = _stable_id(block.table_id, row_index, col, property_name, value, prefix="spec")
                record = ConditionalFactRecord(
                    subject=entry.canonical_subject,
                    subject_type="材料",
                    property_name=property_name,
                    value_text=value,
                    unit=parsed.unit or header_unit,
                    value_num=parsed.value_num,
                    lower_bound=parsed.lower_bound,
                    upper_bound=parsed.upper_bound,
                    comparator=parsed.comparator,
                    value_role="specification_value",
                    method=method,
                    conditions=conditions,
                    evidence=block.raw_text,
                    confidence=0.97 if safe else 0.65,
                    source=SourceLocation(
                        source_path="",
                        block_id=block.table_id,
                        table_id=block.table_id,
                        row_index=row_index,
                        column_index=col,
                        line_start=block.line_start,
                        line_end=block.line_end,
                    ),
                    record_id=record_id,
                    table_id=block.table_id,
                    row_index=row_index,
                    column_index=col,
                    property_role="property",
                    normalized_value_text=parsed.normalized_text or value,
                    subject_source="specification_entry_scope",
                    row_header_path=[property_raw],
                    column_header_path=[grade] if grade else [],
                    record_status="ready" if safe else "candidate",
                    unresolved_reasons=[] if safe else ["specification_value_not_safely_projectable"],
                )
                decision = RecordGateDecision(
                    record_id=record_id,
                    table_id=block.table_id,
                    accepted=safe,
                    action="export" if safe else "hold",
                    canonical_subject=entry.canonical_subject,
                    subject_id=f"spec:{entry.section_id}",
                    subject_type="材料",
                    original_status=record.record_status,
                    final_status="ready" if safe else "candidate",
                    confidence=record.confidence,
                    reasons=["specification_entry_scoped_table"] if safe else ["specification_value_not_safely_projectable"],
                )
                decisions.append(decision)
                if safe:
                    accepted.append((record, decision, plan))
                audit_rows.append({
                    "table_id": block.table_id,
                    "section_id": entry.section_id,
                    "subject": entry.canonical_subject,
                    "row_index": row_index,
                    "column_index": col,
                    "property_raw": property_raw,
                    "property_name": property_name,
                    "value": value,
                    "unit": parsed.unit or header_unit,
                    "grade": grade,
                    "method": method,
                    "decision": decision.action,
                    "reason": "|".join(decision.reasons),
                })

    stage_dir = output_dir / "step_specification_handbook"
    stage_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "table_id", "section_id", "subject", "row_index", "column_index", "property_raw",
        "property_name", "value", "unit", "grade", "method", "decision", "reason",
    ]
    with (stage_dir / "specification_table_projection.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(audit_rows)
    report = {
        "ok": True,
        "stage": "specification_table_projection_v2_phase102",
        "entries_detected": len(entries),
        "tables_matched": tables_matched,
        "projected_records": len(decisions),
        "ready_records": len(accepted),
        "held_records": len(decisions) - len(accepted),
        "projection_path": str(stage_dir / "specification_table_projection.tsv"),
        "policy": "Project rows as material properties; project/indicator/method columns never become subjects. Multi-grade columns are represented as conditions.",
    }
    (stage_dir / "specification_handbook_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return accepted, decisions, report


__all__ = ["build_specification_text_layer", "build_specification_table_layer"]
