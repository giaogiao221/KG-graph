from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.core.schemas import ConditionAtom
from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.handbook.entry_parser import ChemEntry, parse_entries
from book_engine.handbook.extractor import dedup_facts, extract_facts_from_entry
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.text_fact_extractor import TextFactRecord


_PROPERTY_MAP = {
    "中文名": "中文名称",
    "英文名": "英文名称",
    "CAS号": "CAS登记号",
    "相对分子量": "相对分子质量",
}
_IDENTITY_PROPERTIES = {
    "中文名称", "英文名称", "中文别名", "英文别名", "化学式", "分子式", "结构式", "CAS登记号", "编号"
}


def _float_or_none(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


_NUMERIC_TOKEN_RE = re.compile(r"[-+]?\d+(?:[.．]\d+)?")


def _evidence_aligned_value_num(value_text: str, projected_value: object) -> float | None:
    """Prefer the numeric token preserved in the evidence value.

    Legacy handbook projections occasionally truncate a decimal when OCR leaves
    a trailing degree-like glyph (for example ``323.4G`` -> ``323``).  The
    decimal is already explicit in the source value, so restoring it is a
    deterministic evidence alignment rather than an inferred correction.
    """
    projected = _float_or_none(projected_value)
    match = _NUMERIC_TOKEN_RE.search(str(value_text or "").replace("．", "."))
    if not match:
        return projected
    try:
        evidence_value = float(match.group(0).replace("．", "."))
    except Exception:
        return projected
    if projected is None:
        return evidence_value
    token = match.group(0)
    if "." in token or "．" in token:
        if abs(projected - evidence_value) >= 1e-9:
            return evidence_value
    return projected


def _record_id(entry: ChemEntry, prop: str, value: str, ordinal: int) -> str:
    digest = hashlib.sha1(
        f"{entry.entry_id}|{entry.start_line}|{prop}|{value}|{ordinal}".encode("utf-8")
    ).hexdigest()[:20]
    return f"hbk:{digest}"


def _entry_block(entry: ChemEntry) -> TextBlock:
    block_id = f"HB:{entry.entry_id}:{entry.start_line}"
    return TextBlock(
        block_id=block_id,
        text=entry.block_text,
        line_start=entry.start_line,
        line_end=entry.end_line,
        heading_path=[entry.entry_type, f"{entry.entry_id} {entry.canonical_subject_name}"],
        heading_title=entry.canonical_subject_name,
        heading_level=2,
        role="material_profile",
        role_confidence=max(0.75, entry.title_confidence),
        role_reasons=["repeated_entry_handbook_scope"],
    )


def _entry_anchor(entry: ChemEntry, block_id: str, duplicate: bool) -> TextSubjectAnchor:
    status = "candidate" if duplicate or entry.title_confidence < 0.70 else "confirmed"
    return TextSubjectAnchor(
        block_id=block_id,
        subject=entry.canonical_subject_name,
        subject_type="材料",
        source="handbook_entry_header",
        confidence=min(0.98, max(0.70, entry.title_confidence + 0.05)),
        status=status,
        reasons=["entry_code_scoped_subject"] + (["duplicate_entry_code"] if duplicate else []),
    )


def _entry_records(entry: ChemEntry, block: TextBlock, duplicate: bool, book_id: str, book_title: str) -> List[TextFactRecord]:
    raw_facts = extract_facts_from_entry(entry, book_id=book_id, book_title=book_title)
    raw_facts.append({
        "关系/属性名称": "编号",
        "尾实体/取值文本": entry.entry_id,
        "数值": "",
        "范围下限": "",
        "范围上限": "",
        "单位": "",
        "条件文本": "",
        "方法名称": "",
        "证据文本": f"{entry.entry_id} {entry.title_raw}",
        "置信度": f"{entry.title_confidence:.2f}",
        "抽取来源": "book_engine.handbook.entry_header",
    })
    raw_facts = dedup_facts(raw_facts)

    records: List[TextFactRecord] = []
    seen: set[tuple[str, str]] = set()
    for ordinal, fact in enumerate(raw_facts, start=1):
        prop = _PROPERTY_MAP.get(str(fact.get("关系/属性名称", "") or "").strip(), str(fact.get("关系/属性名称", "") or "").strip())
        value = str(fact.get("尾实体/取值文本", "") or "").strip()
        if not prop or not value:
            continue
        key = (prop, value)
        if key in seen:
            continue
        seen.add(key)
        confidence = float(str(fact.get("置信度", "0.87") or "0.87"))
        source_type = "handbook_entry_identity" if prop in _IDENTITY_PROPERTIES else "handbook_entry_property"
        record_status = "candidate" if duplicate or entry.title_confidence < 0.70 else "ready"
        reasons = ["duplicate_entry_code"] if duplicate else []
        evidence = str(fact.get("证据文本", "") or "").strip() or f"{prop}：{value}"
        conditions = []
        for part in re.split(r"[；;]", str(fact.get("条件文本", "") or "")):
            if "=" not in part:
                continue
            name, cond_value = part.split("=", 1)
            name, cond_value = name.strip(), cond_value.strip()
            if name and cond_value:
                conditions.append(ConditionAtom(
                    name=name,
                    normalized_name=name,
                    condition_type="handbook_field_qualifier",
                    value_text=cond_value,
                    scope="field_record",
                    confidence=0.96,
                    source_kind="handbook_field",
                    source_text=part.strip(),
                ))
        records.append(
            TextFactRecord(
                record_id=_record_id(entry, prop, value, ordinal),
                block_id=block.block_id,
                subject=entry.canonical_subject_name,
                subject_type="材料",
                property_name=prop,
                value_text=value,
                unit=str(fact.get("单位", "") or "").strip(),
                value_num=None if prop in _IDENTITY_PROPERTIES else _evidence_aligned_value_num(value, fact.get("数值")),
                lower_bound=None if prop in _IDENTITY_PROPERTIES else _float_or_none(fact.get("范围下限")),
                upper_bound=None if prop in _IDENTITY_PROPERTIES else _float_or_none(fact.get("范围上限")),
                comparator="",
                normalized_value_text=value,
                conditions=conditions,
                method=str(fact.get("方法名称", "") or "").strip(),
                relation_kind="attribute",
                source_type=source_type,
                heading_path=list(block.heading_path),
                line_start=entry.start_line,
                line_end=entry.end_line,
                evidence=evidence,
                confidence=min(0.98, max(0.70, confidence)),
                record_status=record_status,
                unresolved_reasons=reasons,
                anchor_source="handbook_entry_header",
                fact_clause=evidence,
                owner_evidence=f"{entry.entry_id} {entry.title_raw}",
                owner_source="handbook_entry_scope",
            )
        )
    return records


def build_repeated_entry_text_layer(
    document: MarkdownDocument,
    *,
    output_dir: Path,
    book_id: str,
    book_title: str,
) -> Tuple[List[TextBlock], List[TextSubjectAnchor], Dict[str, TextSubjectAnchor], List[TextFactRecord], dict[str, object]]:
    parsed = parse_entries(document.text)
    code_counts = Counter(entry.entry_id for entry in parsed)
    entries = [entry for entry in parsed if entry.canonical_subject_name]

    blocks: List[TextBlock] = []
    anchors: List[TextSubjectAnchor] = []
    anchor_by_block: Dict[str, TextSubjectAnchor] = {}
    records: List[TextFactRecord] = []
    registry_rows: List[dict[str, object]] = []

    for entry in entries:
        duplicate = code_counts[entry.entry_id] > 1
        block = _entry_block(entry)
        anchor = _entry_anchor(entry, block.block_id, duplicate)
        entry_records = _entry_records(entry, block, duplicate, book_id, book_title)
        blocks.append(block)
        anchors.append(anchor)
        anchor_by_block[block.block_id] = anchor
        records.extend(entry_records)
        registry_rows.append({
            "entry_code": entry.entry_id,
            "canonical_subject": entry.canonical_subject_name,
            "english_name_raw": entry.english_name,
            "entry_type": entry.entry_type,
            "line_start": entry.start_line,
            "line_end": entry.end_line,
            "title_raw": entry.title_raw,
            "title_normalized": entry.title_normalized,
            "title_confidence": f"{entry.title_confidence:.4f}",
            "duplicate_entry_code": "yes" if duplicate else "no",
            "record_count": len(entry_records),
        })

    stage_dir = output_dir / "step_handbook_entries"
    stage_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "entry_code", "canonical_subject", "english_name_raw", "entry_type", "line_start", "line_end",
        "title_raw", "title_normalized", "title_confidence", "duplicate_entry_code", "record_count",
    ]
    with (stage_dir / "handbook_entry_registry.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(registry_rows)

    report = {
        "ok": True,
        "stage": "repeated_entry_handbook_extraction_v2_phase101",
        "entries_detected": len(parsed),
        "entries_with_subject": len(entries),
        "unique_entry_codes": len(code_counts),
        "duplicate_entry_codes": sum(1 for count in code_counts.values() if count > 1),
        "duplicate_entry_instances": sum(count for count in code_counts.values() if count > 1),
        "entry_blocks": len(blocks),
        "entry_fact_candidates": len(records),
        "ready_entry_fact_candidates": sum(1 for record in records if record.record_status == "ready"),
        "candidate_entry_fact_candidates": sum(1 for record in records if record.record_status != "ready"),
        "registry_path": str(stage_dir / "handbook_entry_registry.tsv"),
        "policy": "Six-digit entry headers define hard subject scopes before generic text routing; no fact may cross the next entry boundary.",
    }
    (stage_dir / "handbook_entry_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return blocks, anchors, anchor_by_block, records, report


__all__ = ["build_repeated_entry_text_layer"]
