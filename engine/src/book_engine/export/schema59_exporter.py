from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from book_engine.core.schemas import ConditionalFactRecord, TableBlock
from book_engine.export.schema59_columns import SCHEMA59_COLUMNS
from book_engine.export.schema59_contract import header_fingerprint, validate_graph_file
from book_engine.export.cross_source_deduplicator import deduplicate_rows, find_semantic_conflicts, semantic_key as cross_source_semantic_key
from book_engine.export.quality_audit_writer import write_phase7_quality_outputs
from book_engine.routing.subject_type_harmonizer import SubjectTypeAudit
from book_engine.text.text_condition_extractor import TextConditionAudit
from book_engine.gates.table_record_gate import RecordGateDecision
from book_engine.gates.text_fact_gate import TextGateDecision
from book_engine.text.text_fact_extractor import TextFactRecord
from book_engine.ontology.property_alignment_engine import PropertyAlignmentEngine
from book_engine.export.property_alignment_audit_writer import write_property_alignment_outputs
from book_engine.quality.production_release_gate import apply_production_release_gate
from book_engine.export.production_audit_writer import write_production_release_outputs
from book_engine.export.table_evidence_writer import escape_single_line
from book_engine.quality.semantic_release_consistency import apply_semantic_release_guard
from book_engine.export.release_consistency_audit_writer import write_release_consistency_outputs
from book_engine.quality.generalized_release_gate import apply_generalized_release_gate
from book_engine.export.generalized_release_audit_writer import write_generalized_release_outputs
from book_engine.ontology.supplementary_constraints import SupplementaryConstraints, write_audits


def _slug(text: str) -> str:
    value = re.sub(r"\s+", "", text or "").casefold()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:80] or "unknown"


def _deduplicated_conditions(record: ConditionalFactRecord):
    """Return semantically unique condition atoms without altering the audit record.

    OCR and clause binding can preserve both ``6,86 kPa`` and ``6.86 kPa``.
    They are useful in the source audit but must not appear twice in the locked
    59-column production projection.
    """
    unique = {}
    for atom in record.conditions:
        name = atom.normalized_name or atom.name
        numeric = (atom.value_num, atom.lower_bound, atom.upper_bound)
        if any(value is not None for value in numeric):
            value_key = tuple(round(float(value), 12) if value is not None else None for value in numeric)
            key = (name, value_key, (atom.unit or "").strip().casefold())
        else:
            compact = re.sub(r"\s+", "", atom.value_text or "").replace(",", ".").casefold()
            key = (name, compact, (atom.unit or "").strip().casefold())
        previous = unique.get(key)
        if previous is None or (atom.confidence or 0.0) > (previous.confidence or 0.0):
            unique[key] = atom
    return sorted(
        unique.values(),
        key=lambda item: (-(item.priority or 0), item.normalized_name or item.name, item.value_text or ""),
    )


def _condition_text(record: ConditionalFactRecord) -> str:
    return "；".join(
        f"{atom.normalized_name or atom.name}={atom.value_text}"
        for atom in _deduplicated_conditions(record)
    )


def _condition_attributes(record: ConditionalFactRecord) -> str:
    payload: Dict[str, List[str]] = {}
    for atom in _deduplicated_conditions(record):
        value = atom.value_text
        values = payload.setdefault(atom.normalized_name or atom.name, [])
        if value not in values:
            values.append(value)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _value_projection(
    value_num: float | None,
    lower_bound: float | None,
    upper_bound: float | None,
) -> Dict[str, str]:
    numeric = value_num is not None or lower_bound is not None or upper_bound is not None
    if numeric:
        if lower_bound is not None or upper_bound is not None:
            numeric_type, value_type = "范围", "range"
        else:
            numeric_type, value_type = "标量", "number"
        return {
            "value_hidden": "是",
            "value_display_policy": "hide_numeric_attribute",
            "value_display_reason": "explicit_numeric_metric_v2_phase7",
            "数值类型": numeric_type,
            "value_type": value_type,
        }
    return {
        "value_hidden": "否",
        "value_display_policy": "show_non_numeric_value",
        "value_display_reason": "non_numeric_value_visible_v2_phase7",
        "数值类型": "文本",
        "value_type": "text",
    }


def _semantic_key(subject: str, property_name: str, value_text: str, condition_text: str) -> str:
    normalized = "|".join(
        re.sub(r"\s+", "", item or "").casefold()
        for item in (subject, property_name, value_text, condition_text)
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _table_fact_ids(document_id: str, decision: RecordGateDecision, record: ConditionalFactRecord) -> Tuple[str, str]:
    condition_json = json.dumps([asdict(item) for item in record.conditions], ensure_ascii=False, sort_keys=True, default=str)
    base = "|".join((document_id, decision.subject_id, record.property_name, record.value_text, condition_json, record.table_id))
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()
    return f"fact:{digest[:20]}", f"gfk:{digest}"


def _text_fact_ids(document_id: str, decision: TextGateDecision, record: TextFactRecord) -> Tuple[str, str]:
    base = "|".join(
        (
            document_id,
            decision.canonical_subject,
            record.property_name,
            record.value_text,
            record.process_id,
            record.step_id,
            " > ".join(record.heading_path),
            str(record.line_start),
        )
    )
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()
    return f"fact:{digest[:20]}", f"gfk:{digest}"


def _blank_row() -> Dict[str, object]:
    return {column: "" for column in SCHEMA59_COLUMNS}


def _project_table_row(
    document_id: str,
    book_title: str,
    record: ConditionalFactRecord,
    decision: RecordGateDecision,
    plan: object,
    block: TableBlock,
) -> Dict[str, object]:
    fact_id, graph_key = _table_fact_ids(document_id, decision, record)
    category = ""
    condition_json = json.dumps([asdict(item) for item in record.conditions], ensure_ascii=False, sort_keys=True, default=str)
    condition_text = _condition_text(record)
    is_component = record.property_role == "composition" or record.value_role == "formulation_component"
    row = _blank_row()
    row.update(
        {
            "fact_id": fact_id,
            "graph_fact_key": graph_key,
            "文档ID": document_id,
            "书名": book_title,
            "章节路径": block.heading,
            "来源定位": f"L{block.line_start}-L{block.line_end};R{record.row_index};C{record.column_index}",
            "来源类型": "table_conditional_record",
            "所属表格ID": record.table_id,
            "所属表格标题": block.heading,
            "主体名称": decision.canonical_subject,
            "主体类型": decision.subject_type,
            "事实类型": "组成事实" if is_component else "属性事实",
            "predicate_raw": record.property_name,
            "edge_verb": "包含组分" if is_component else "具有",
            "fact_node_label": f"{record.property_name}事实",
            "attribute_category": category,
            "attribute_category_key": f"cat:{_slug(category)}",
            "attribute_name": record.property_name,
            "attribute_key": f"cat:{_slug(category)}::attr:{_slug(record.property_name)}",
            "尾实体/取值文本": record.value_text,
            "normalized_value_text": record.normalized_value_text or record.value_text,
            "normalized_value_num": "" if record.value_num is None else record.value_num,
            "normalized_unit": record.unit,
            "数值": "" if record.value_num is None else record.value_num,
            "范围下限": "" if record.lower_bound is None else record.lower_bound,
            "范围上限": "" if record.upper_bound is None else record.upper_bound,
            "单位": record.unit,
            "条件文本": condition_text,
            "structured_condition_json": condition_json,
            "condition_attributes": _condition_attributes(record),
            "方法名称": record.method,
            "component_name": record.property_name if is_component else "",
            "component_role": "配方组分" if is_component else "",
            "component_amount_text": record.value_text if is_component else "",
            "component_amount_value": ("" if record.value_num is None else record.value_num) if is_component else "",
            "component_amount_unit": record.unit if is_component else "",
            "component_amount_attribute": ("质量分数" if record.unit in {"%", "‰"} else "组分用量") if is_component else "",
            "table_semantic_type": getattr(plan, "topology", ""),
            "condition_metric_role": "conditional_measurement" if record.conditions else "unconditional_measurement",
            "置信度": f"{decision.confidence:.4f}",
            "抽取来源": "book_engine_v2_table_phase102",
            # Every table-derived fact carries the complete original table block.
            # Row/column-local evidence is stored in step_table_evidence.
            "证据文本": block.raw_text or record.evidence or f"{block.heading} | {record.property_name} | {record.value_text}",
        }
    )
    row.update(_value_projection(record.value_num, record.lower_bound, record.upper_bound))
    return row


_RELATION_KIND_EXPORT = {
    "classification": ("分类关系事实", "分为"),
    "definition": ("定义关系事实", "定义为"),
    "application": ("应用关系事实", "用于"),
    "function": ("功能关系事实", "具有功能"),
    "connection": ("连接关系事实", "连接"),
    "location": ("位置关系事实", "位于"),
    "method": ("方法关系事实", "采用方法"),
    "comparison": ("比较关系事实", "比较"),
    "effect": ("作用影响事实", "影响"),
    "causal": ("因果关系事实", "导致"),
    "experiment_result": ("试验结果事实", "产生结果"),
    "safety": ("安全储运事实", "要求"),
    "method_step": ("方法步骤事实", "包含步骤"),
}


def _relation_export_meta(record: TextFactRecord) -> tuple[str, str, str]:
    if record.relation_kind == "process_step":
        return "工艺步骤事实", "包含步骤", "工艺步骤"
    if record.relation_kind == "composition":
        return "组成事实", "组成说明", f"{record.property_name}事实"
    fact_type, edge = _RELATION_KIND_EXPORT.get(
        record.relation_kind, ("属性事实", "具有")
    )
    label = "方法步骤" if record.relation_kind == "method_step" else f"{record.property_name}事实"
    return fact_type, edge, label


def _project_text_row(
    document_id: str,
    book_title: str,
    record: TextFactRecord,
    decision: TextGateDecision,
) -> Dict[str, object]:
    fact_id, graph_key = _text_fact_ids(document_id, decision, record)
    category = ""
    is_component = record.relation_kind == "composition"
    fact_type, edge_verb, fact_label = _relation_export_meta(record)
    row = _blank_row()
    row.update(
        {
            "fact_id": fact_id,
            "graph_fact_key": graph_key,
            "文档ID": document_id,
            "书名": book_title,
            "章节路径": " > ".join(record.heading_path),
            "来源定位": f"L{record.line_start}-L{record.line_end}",
            "来源类型": record.source_type,
            "所属表格ID": "",
            "所属表格标题": "",
            "主体名称": decision.canonical_subject,
            "主体类型": decision.subject_type,
            "事实类型": fact_type,
            "predicate_raw": record.property_name,
            "edge_verb": edge_verb,
            "fact_node_label": fact_label,
            "attribute_category": category,
            "attribute_category_key": f"cat:{_slug(category)}",
            "attribute_name": record.property_name,
            "attribute_key": f"cat:{_slug(category)}::attr:{_slug(record.property_name)}",
            "尾实体/取值文本": record.value_text,
            "normalized_value_text": record.normalized_value_text or record.value_text,
            "normalized_value_num": "" if record.value_num is None else record.value_num,
            "normalized_unit": record.unit,
            "数值": "" if record.value_num is None else record.value_num,
            "范围下限": "" if record.lower_bound is None else record.lower_bound,
            "范围上限": "" if record.upper_bound is None else record.upper_bound,
            "单位": record.unit,
            "条件文本": _condition_text(record),
            "structured_condition_json": json.dumps([asdict(item) for item in record.conditions], ensure_ascii=False, sort_keys=True, default=str),
            "condition_attributes": _condition_attributes(record),
            "方法名称": record.method,
            "component_name": "" if not is_component else record.value_text,
            "component_role": "组成描述" if is_component else "",
            "component_amount_text": "",
            "component_amount_value": "",
            "component_amount_unit": "",
            "component_amount_attribute": "",
            "table_semantic_type": "",
            "condition_metric_role": "conditional_text_measurement" if record.conditions else "text_statement",
            "置信度": f"{decision.confidence:.4f}",
            "抽取来源": "book_engine_v2_text_phase103",
            "证据文本": record.evidence,
        }
    )
    if record.relation_kind in {"process", "process_step", "method_step"}:
        process_id = record.process_id or (
            "proc:" + hashlib.sha1(
                f"{decision.canonical_subject}|{record.property_name}|{record.line_start}".encode("utf-8")
            ).hexdigest()[:16]
        )
        row.update(
            {
                "process_id": process_id,
                "process_name": record.process_name or record.property_name,
                "process_type": record.process_type or "文本工艺描述",
                "step_id": record.step_id,
                "step_index": "" if record.step_index is None else record.step_index,
                "step_label": record.step_label,
                "step_action": record.step_action,
                "step_object": record.step_object,
                "step_condition_text": record.step_condition_text,
                "step_result_text": record.step_result_text or (record.value_text if record.relation_kind == "process" else ""),
                "previous_step_id": record.previous_step_id,
                "next_step_id": record.next_step_id,
            }
        )
    row.update(_value_projection(record.value_num, record.lower_bound, record.upper_bound))
    return row


_HARD_TABLE_CANDIDATE_REASONS = {
    "empty_value", "property_unresolved", "identifier_only_subject",
    "suspected_subject_property_axis_inversion", "canonical_subject_too_long",
}
_HARD_TEXT_CANDIDATE_REASONS = {
    "invalid_topic_or_property_like_subject", "unresolved_deictic_subject",
    "coordinated_heading_used_as_single_subject", "descriptive_heading_used_as_subject",
    "single_letter_variable_subject", "method_or_sentence_fragment_subject",
    "table_of_contents_or_page_number_evidence", "figure_or_table_caption_evidence",
    "unit_or_variable_subject", "invalid_property", "empty_value",
    "external_visual_or_list_required", "visual_reference_without_self_contained_arguments",
    "visual_dependent_narrative", "incomplete_composition_description", "false_composition_trigger",
}


def _candidate_worthy_table(record: ConditionalFactRecord, decision: RecordGateDecision) -> bool:
    if not record.subject or not record.property_name or not record.value_text:
        return False
    if _HARD_TABLE_CANDIDATE_REASONS.intersection(decision.reasons):
        return False
    if record.confidence < 0.35:
        return False
    return True


def _candidate_worthy_text(record: TextFactRecord, decision: TextGateDecision) -> bool:
    if not record.subject or not record.property_name or not record.value_text:
        return False
    if _HARD_TEXT_CANDIDATE_REASONS.intersection(decision.reasons):
        return False
    if not decision.evidence_self_contained or record.confidence < 0.55:
        return False
    return True


def _relink_released_process_steps(rows: Sequence[Dict[str, object]]) -> int:
    groups: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        process_id = str(row.get("process_id", "") or "")
        step_id = str(row.get("step_id", "") or "")
        if process_id and step_id:
            groups.setdefault(process_id, []).append(row)
    changed = 0
    for group in groups.values():
        def order_key(item: Dict[str, object]) -> Tuple[int, str]:
            try:
                index = int(float(str(item.get("step_index", "") or "0")))
            except Exception:
                index = 0
            return index, str(item.get("step_id", "") or "")
        group.sort(key=order_key)
        for pos, row in enumerate(group):
            current_index = order_key(row)[0]
            previous = group[pos - 1] if pos > 0 else None
            following = group[pos + 1] if pos + 1 < len(group) else None
            previous_id = ""
            next_id = ""
            if previous is not None and order_key(previous)[0] + 1 == current_index:
                previous_id = str(previous.get("step_id", "") or "")
            if following is not None and current_index + 1 == order_key(following)[0]:
                next_id = str(following.get("step_id", "") or "")
            if str(row.get("previous_step_id", "") or "") != previous_id:
                row["previous_step_id"] = previous_id
                changed += 1
            if str(row.get("next_step_id", "") or "") != next_id:
                row["next_step_id"] = next_id
                changed += 1
    return changed


def _write_rows(path: Path, rows: Sequence[Dict[str, object]]) -> Dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=SCHEMA59_COLUMNS, delimiter="\t", extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            # Preserve full evidence reversibly while keeping every TSV record on one
            # physical line. This prevents multiline table evidence from breaking
            # 59-column validation and downstream imports.
            writer.writerow({column: escape_single_line(row.get(column, "")) for column in SCHEMA59_COLUMNS})
    report = validate_graph_file(path, SCHEMA59_COLUMNS, check_rows=True)
    if not report.get("ok"):
        raise RuntimeError(f"Legacy schema59 contract validation failed: {report}")
    return report


def _decorate_gate_metadata(
    row: Dict[str, object],
    decision: RecordGateDecision | TextGateDecision,
    *,
    origin_stream: str,
    evidence_self_contained: bool = True,
) -> Dict[str, object]:
    output = dict(row)
    output["_gate_reasons"] = ";".join(getattr(decision, "reasons", []) or [])
    output["_gate_accepted"] = "1" if bool(getattr(decision, "accepted", False)) else "0"
    output["_origin_stream"] = origin_stream
    output["_evidence_self_contained"] = "1" if evidence_self_contained else "0"
    output["_gate_action"] = str(getattr(decision, "action", "") or "")
    return output


def export_schema59(
    output_dir: Path,
    input_path: Path,
    accepted: Sequence[Tuple[ConditionalFactRecord, RecordGateDecision, object]],
    block_by_table: Mapping[str, TableBlock],
    accepted_text: Sequence[Tuple[TextFactRecord, TextGateDecision]] = (),
    *,
    held_table: Sequence[Tuple[ConditionalFactRecord, RecordGateDecision, object]] = (),
    held_text: Sequence[Tuple[TextFactRecord, TextGateDecision]] = (),
    type_audits: Sequence[SubjectTypeAudit] = (),
    text_condition_audits: Sequence[TextConditionAudit] = (),
) -> Dict[str, object]:
    graph_dir = output_dir / "step_graph_guard"
    release_dir = output_dir / "step_release"
    graph_dir.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)

    document_id = "doc:" + hashlib.sha1(str(input_path.resolve()).encode("utf-8")).hexdigest()[:16]
    book_title = input_path.stem

    # Strict/high-precision source stream.  Its behavior remains the phase-103
    # baseline and continues to feed graph_import_ready.tsv.
    projected_table_rows: List[Dict[str, object]] = []
    for record, decision, plan in accepted:
        block = block_by_table[record.table_id]
        row = _project_table_row(document_id, book_title, record, decision, plan, block)
        projected_table_rows.append(
            _decorate_gate_metadata(row, decision, origin_stream="accepted_table", evidence_self_contained=True)
        )

    projected_text_rows: List[Dict[str, object]] = []
    for record, decision in accepted_text:
        row = _project_text_row(document_id, book_title, record, decision)
        projected_text_rows.append(
            _decorate_gate_metadata(
                row,
                decision,
                origin_stream="accepted_text",
                evidence_self_contained=bool(decision.evidence_self_contained),
            )
        )

    # The legacy candidate file retains the previous evidence-quality filter.
    # The generalized stream below is wider and independently applies its own
    # catastrophic-error hard gate plus soft risk scoring.
    held_candidate_rows: List[Dict[str, object]] = []
    generalized_held_rows: List[Dict[str, object]] = []
    held_table_candidate_rows = 0
    held_text_candidate_rows = 0
    generalized_held_table_rows = 0
    generalized_held_text_rows = 0

    for record, decision, plan in held_table:
        block = block_by_table.get(record.table_id)
        if block is None:
            continue
        row = _project_table_row(document_id, book_title, record, decision, plan, block)
        row["抽取来源"] = "book_engine_v2_table_held_candidate_phase105"
        row["置信度"] = f"{max(0.01, decision.confidence):.4f}"
        decorated = _decorate_gate_metadata(
            row, decision, origin_stream="held_table", evidence_self_contained=True
        )
        generalized_held_rows.append(decorated)
        generalized_held_table_rows += 1
        if _candidate_worthy_table(record, decision):
            held_candidate_rows.append(decorated)
            held_table_candidate_rows += 1

    for record, decision in held_text:
        row = _project_text_row(document_id, book_title, record, decision)
        row["抽取来源"] = "book_engine_v2_text_held_candidate_phase105"
        decorated = _decorate_gate_metadata(
            row,
            decision,
            origin_stream="held_text",
            evidence_self_contained=bool(decision.evidence_self_contained),
        )
        generalized_held_rows.append(decorated)
        generalized_held_text_rows += 1
        if _candidate_worthy_text(record, decision):
            held_candidate_rows.append(decorated)
            held_text_candidate_rows += 1

    # Legacy resources remain a configurable supplement, never a closed
    # schema: only explicit aliases are normalized and lexicon hits are audit
    # evidence.  Subject deny-list hits are routed after the normal gates.
    supplementary_constraints = SupplementaryConstraints.from_default_config()
    supplementary_audits: List[Dict[str, object]] = []

    def prepare_supplementary_rows(rows: Sequence[Mapping[str, object]], stage: str) -> List[Dict[str, object]]:
        prepared_rows: List[Dict[str, object]] = []
        for item in rows:
            prepared, audits = supplementary_constraints.prepare_row(item, stage=stage)
            prepared_rows.append(prepared)
            supplementary_audits.extend(audits)
        return prepared_rows

    projected_table_rows = prepare_supplementary_rows(projected_table_rows, "v105_strict_table")
    projected_text_rows = prepare_supplementary_rows(projected_text_rows, "v105_strict_text")
    held_candidate_rows = prepare_supplementary_rows(held_candidate_rows, "v105_held_candidate")
    generalized_held_rows = prepare_supplementary_rows(generalized_held_rows, "v105_generalized_held")

    # Phase 8A strict alignment.  Only rows that already passed the strict
    # source gates enter the high-precision production gate.
    property_aligner = PropertyAlignmentEngine.from_default_config()
    aligned_table_rows, table_property_decisions = property_aligner.align_rows(projected_table_rows)
    aligned_text_rows, text_property_decisions = property_aligner.align_rows(projected_text_rows)
    property_alignment_report = write_property_alignment_outputs(
        output_dir, list(table_property_decisions) + list(text_property_decisions)
    )

    table_rows, table_dedup_audits = deduplicate_rows(aligned_table_rows)
    merged_rows, merged_dedup_audits = deduplicate_rows(table_rows + aligned_text_rows)
    all_dedup_audits = list(table_dedup_audits) + list(merged_dedup_audits)
    preproduction_conflicts = find_semantic_conflicts(merged_rows)

    release_guard_result = apply_semantic_release_guard(merged_rows)
    release_consistency_report = write_release_consistency_outputs(output_dir, release_guard_result)
    production_result = apply_production_release_gate(release_guard_result.releasable_rows)
    production_report = write_production_release_outputs(output_dir, production_result)
    def route_supplementary_subjects(
        rows: Sequence[Mapping[str, object]], stage: str
    ) -> tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
        released: List[Dict[str, object]] = []
        review: List[Dict[str, object]] = []
        rejected: List[Dict[str, object]] = []
        for item in rows:
            row = dict(item)
            action, audits = supplementary_constraints.subject_decision(
                str(row.get("主体名称", "") or ""), stage=stage
            )
            supplementary_audits.extend(audits)
            if action == "manual_review":
                review.append(row)
            elif action == "reject":
                rejected.append(row)
            else:
                released.append(row)
        return released, review, rejected

    release_rows, supplementary_strict_review, supplementary_strict_rejected = route_supplementary_subjects(
        production_result.released_rows, "v105_strict_release"
    )
    process_links_rebuilt = _relink_released_process_steps(release_rows)
    production_candidate_rows = list(production_result.candidate_rows) + supplementary_strict_review
    semantic_guard_candidate_rows = list(release_guard_result.candidate_rows)
    candidate_rows, held_candidate_dedup = deduplicate_rows(
        production_candidate_rows + semantic_guard_candidate_rows + held_candidate_rows
    )
    rejected_rows = (
        list(production_result.rejected_rows)
        + list(release_guard_result.rejected_rows)
        + supplementary_strict_rejected
    )
    release_table_rows = [
        row for row in release_rows if str(row.get("来源类型", "")) == "table_conditional_record"
    ]
    release_conflicts = find_semantic_conflicts(release_rows)

    # Phase 105 generalized stream: align and score both strict-accepted and
    # source-held records.  Only catastrophic structural/semantic errors are
    # hard rejected; registry status, anchor confidence and plan confidence are
    # soft risks.  The strict rows are always a subset of this output.
    generalized_raw_rows = list(projected_table_rows) + list(projected_text_rows) + generalized_held_rows
    generalized_aligned_rows, generalized_property_decisions = property_aligner.align_rows(generalized_raw_rows)
    generalized_merged_rows, generalized_dedup_audits = deduplicate_rows(generalized_aligned_rows)

    strict_semantic_keys = {cross_source_semantic_key(row) for row in release_rows}
    for row in generalized_merged_rows:
        if cross_source_semantic_key(row) in strict_semantic_keys:
            row["_strict_released"] = "1"

    generalized_guard = apply_semantic_release_guard(generalized_merged_rows)
    semantic_reason_by_fact = {
        item.fact_id: item.reasons for item in generalized_guard.audits if item.reasons
    }
    generalized_result = apply_generalized_release_gate(
        generalized_merged_rows,
        strict_fact_ids=[str(row.get("fact_id", "") or "") for row in release_rows],
        semantic_reason_by_fact=semantic_reason_by_fact,
    )
    generalized_release_rows, generalized_postgate_dedup = deduplicate_rows(
        list(generalized_result.released_rows)
    )
    (
        generalized_release_rows,
        supplementary_generalized_review,
        supplementary_generalized_rejected,
    ) = route_supplementary_subjects(generalized_release_rows, "v105_generalized_release")
    generalized_process_links_rebuilt = _relink_released_process_steps(generalized_release_rows)
    generalized_candidate_rows, _ = deduplicate_rows(
        list(generalized_result.candidate_rows) + supplementary_generalized_review
    )
    generalized_rejected_rows, _ = deduplicate_rows(
        list(generalized_result.rejected_rows) + supplementary_generalized_rejected
    )
    generalized_release_report = write_generalized_release_outputs(output_dir, generalized_result)
    generalized_table_rows = [
        row for row in generalized_release_rows
        if str(row.get("来源类型", "")) == "table_conditional_record"
    ]
    generalized_conflicts = find_semantic_conflicts(generalized_release_rows)

    # Legacy/high-precision files.
    table_snapshot_path = graph_dir / "table_graph_import_ready.tsv"
    table_contract_report = _write_rows(table_snapshot_path, release_table_rows)
    graph_path = graph_dir / "graph_import_ready.tsv"
    merged_contract_report = _write_rows(graph_path, release_rows)
    high_precision_path = graph_dir / "graph_import_ready_high_precision.tsv"
    high_precision_contract_report = _write_rows(high_precision_path, release_rows)
    high_precision_table_path = graph_dir / "table_graph_import_ready_high_precision.tsv"
    high_precision_table_contract_report = _write_rows(high_precision_table_path, release_table_rows)
    candidate_path = graph_dir / "graph_candidate_review_59.tsv"
    candidate_contract_report = _write_rows(candidate_path, candidate_rows)
    rejected_path = graph_dir / "graph_rejected_59.tsv"
    rejected_contract_report = _write_rows(rejected_path, rejected_rows)

    # Phase-105 generalized files, also locked to the exact 59-column contract.
    generalized_path = graph_dir / "graph_import_ready_generalized.tsv"
    generalized_contract_report = _write_rows(generalized_path, generalized_release_rows)
    generalized_table_path = graph_dir / "table_graph_import_ready_generalized.tsv"
    generalized_table_contract_report = _write_rows(generalized_table_path, generalized_table_rows)
    generalized_candidate_path = graph_dir / "graph_generalized_candidate_review_59.tsv"
    generalized_candidate_contract_report = _write_rows(generalized_candidate_path, generalized_candidate_rows)
    generalized_rejected_path = graph_dir / "graph_generalized_rejected_59.tsv"
    generalized_rejected_contract_report = _write_rows(generalized_rejected_path, generalized_rejected_rows)
    supplementary_audit_path = graph_dir / "supplementary_constraints_audit.jsonl"
    supplementary_audit_path.write_text("", encoding="utf-8")
    write_audits(supplementary_audit_path, supplementary_audits)

    quality_report = write_phase7_quality_outputs(
        output_dir,
        release_rows,
        all_dedup_audits,
        type_audits,
        text_condition_audits,
        release_conflicts,
    )

    report = {
        "ok": all(
            bool(item.get("ok"))
            for item in (
                merged_contract_report,
                table_contract_report,
                high_precision_contract_report,
                high_precision_table_contract_report,
                candidate_contract_report,
                rejected_contract_report,
                generalized_contract_report,
                generalized_table_contract_report,
                generalized_candidate_contract_report,
                generalized_rejected_contract_report,
                quality_report,
                property_alignment_report,
                production_report,
                release_consistency_report,
                generalized_release_report,
            )
        ),
        "stage": "schema59_generalized_high_recall_v2_phase105",
        "document_id": document_id,
        "book_title": book_title,
        "column_count": len(SCHEMA59_COLUMNS),
        "column_order_locked": True,
        "legacy_header_sha256": header_fingerprint(SCHEMA59_COLUMNS),
        "schema_contract_validation": merged_contract_report,
        "table_snapshot_contract_validation": table_contract_report,
        "projected_table_rows_before_alignment": len(projected_table_rows),
        "aligned_table_rows_before_dedup": len(aligned_table_rows),
        "table_rows_before_production_gate": len(table_rows),
        "released_table_rows": len(release_table_rows),
        "released_table_ids": len({str(row.get("所属表格ID", "")) for row in release_table_rows if str(row.get("所属表格ID", ""))}),
        "released_text_rows": len(release_rows) - len(release_table_rows),
        "projected_text_rows_before_alignment": len(projected_text_rows),
        "aligned_text_rows_before_dedup": len(aligned_text_rows),
        "accepted_text_rows_before_dedup": len(aligned_text_rows),
        "text_deduplicated_against_table": sum(
            1 for item in all_dedup_audits
            if item.kept_source == "table_conditional_record" and item.dropped_source != "table_conditional_record"
        ),
        "preproduction_merged_rows": len(merged_rows),
        "release_consistency_pass_rows": len(release_guard_result.releasable_rows),
        "release_consistency_candidate_rows": len(release_guard_result.candidate_rows),
        "exported_rows": len(release_rows),
        "merged_rows": len(release_rows),
        "candidate_rows": len(candidate_rows),
        "production_candidate_rows": len(production_candidate_rows),
        "semantic_guard_candidate_rows": len(semantic_guard_candidate_rows),
        "held_table_candidate_rows": held_table_candidate_rows,
        "held_text_candidate_rows": held_text_candidate_rows,
        "held_candidate_duplicates_removed": len(held_candidate_dedup),
        "rejected_rows": len(rejected_rows),
        "supplementary_constraints": {
            "audit_count": len(supplementary_audits),
            "strict_manual_review_rows": len(supplementary_strict_review),
            "generalized_manual_review_rows": len(supplementary_generalized_review),
            "audit_path": str(supplementary_audit_path),
        },
        "unique_fact_ids": len({str(row.get("fact_id", "")) for row in release_rows}),
        "unique_graph_fact_keys": len({str(row.get("graph_fact_key", "")) for row in release_rows}),
        "cross_source_duplicates_removed": len(all_dedup_audits),
        "semantic_conflict_groups_before_production_gate": len(preproduction_conflicts),
        "semantic_conflict_groups": len(release_conflicts),
        "process_links_rebuilt": process_links_rebuilt,
        "generalized": {
            "input_projected_rows": len(generalized_raw_rows),
            "aligned_rows": len(generalized_aligned_rows),
            "merged_rows_before_gate": len(generalized_merged_rows),
            "released_rows": len(generalized_release_rows),
            "released_table_rows": len(generalized_table_rows),
            "released_text_rows": len(generalized_release_rows) - len(generalized_table_rows),
            "candidate_rows": len(generalized_candidate_rows),
            "rejected_rows": len(generalized_rejected_rows),
            "held_table_rows_considered": generalized_held_table_rows,
            "held_text_rows_considered": generalized_held_text_rows,
            "property_alignment_decisions": len(generalized_property_decisions),
            "deduplicated_before_gate": len(generalized_dedup_audits),
            "deduplicated_after_gate": len(generalized_postgate_dedup),
            "semantic_conflict_groups": len(generalized_conflicts),
            "process_links_rebuilt": generalized_process_links_rebuilt,
            "strict_rows_preserved": sum(
                1 for row in generalized_release_rows
                if cross_source_semantic_key(row) in strict_semantic_keys
            ),
            "release_report": generalized_release_report,
        },
        "graph_import_ready": str(graph_path),
        "graph_import_ready_high_precision": str(high_precision_path),
        "graph_import_ready_generalized": str(generalized_path),
        "table_graph_import_ready": str(table_snapshot_path),
        "table_graph_import_ready_high_precision": str(high_precision_table_path),
        "table_graph_import_ready_generalized": str(generalized_table_path),
        "graph_candidate_review_59": str(candidate_path),
        "graph_rejected_59": str(rejected_path),
        "graph_generalized_candidate_review_59": str(generalized_candidate_path),
        "graph_generalized_rejected_59": str(generalized_rejected_path),
        "supplementary_constraints_audit": str(supplementary_audit_path),
        "quality_report": quality_report,
        "property_alignment_report": property_alignment_report,
        "production_release_report": production_report,
        "release_consistency_report": release_consistency_report,
        "generalized_release_report": generalized_release_report,
        "scope": "dual_high_precision_and_generalized_80_with_full_evidence_and_exact_legacy_schema59",
        "note": (
            "graph_import_ready.tsv and graph_import_ready_high_precision.tsv retain the strict baseline. "
            "graph_import_ready_generalized.tsv adds medium-risk, evidence-complete facts under a catastrophic-error hard gate "
            "and soft risk scoring for manual cleaning."
        ),
    }
    (graph_dir / "schema59_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig"
    )
    return report


__all__ = ["export_schema59"]
