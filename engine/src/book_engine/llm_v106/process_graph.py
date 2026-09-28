from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .process_flow import ProcessSpan, extract_expected_labels
from .schema59 import SCHEMA59_COLUMNS, PropertyOntologyIndex, span_supported, subject_is_valid


ALLOWED_PROCESS_TYPES = {"制备工艺", "试验流程", "装配流程", "计算流程", "处理流程", "操作流程"}
_GENERIC_PROCESS_NAMES = {
    "方法", "过程", "步骤", "流程", "工艺", "操作", "程序", "计算步骤", "工作流程步骤",
    "method", "process", "procedure", "steps", "workflow", "operation",
}
_GENERIC_ACTIONS = {
    "步骤", "过程", "方法", "首先", "其次", "随后", "最后", "性能调节", "由规则推理", "采用无反馈",
    "介绍", "讨论", "分析", "阐述", "说明", "总结", "研制时间", "研制国家", "发明者",
    "first", "second", "then", "next", "subsequently", "afterward", "finally",
    "introduce", "introduction", "discuss", "analyze", "analyse", "describe", "summarize", "summary",
}
_BAD_RELATIVE = re.compile(
    r"(?:上一步|上步骤|前述步骤|之前的步骤|其它之前|上述步骤|该步骤之前|前面的步骤|"
    r"previous\s+step|prior\s+step|preceding\s+step|above\s+step|earlier\s+step|"
    r"before\s+(?:this|the)\s+step|after\s+the\s+previous\s+step)",
    re.IGNORECASE,
)
_SENTENCE_PUNCT = re.compile(r"[。；;!?！？]")
_SOURCE_LABEL_NUMBER = re.compile(r"\d{1,2}")
_ENTITY_TYPE_MAP = {
    "材料": "材料",
    "样品/产品": "材料",
    "设备/系统": "设备/系统",
    "方法/模型": "方法/模型",
    "其他明确实体": "其他实体",
}


@dataclass(frozen=True)
class ProcessValidation:
    valid_processes: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    repair_recommended: bool


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" ，,。；;：:\t\r\n")


def _slug(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or "")).casefold()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:80] or "unknown"


def _hash(prefix: str, *parts: Any, length: int = 24) -> str:
    digest = hashlib.sha1("|".join(str(x or "") for x in parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:length]}"


def _label_number(label: Any) -> int | None:
    raw = str(label or "").strip()
    match = _SOURCE_LABEL_NUMBER.search(raw)
    if match:
        number = int(match.group(0))
        return number if 1 <= number <= 40 else None
    labels = extract_expected_labels(raw)
    return labels[0] if len(labels) == 1 else None


def _normalize_step(step: Mapping[str, Any], fallback_index: int) -> dict[str, Any]:
    value = dict(step)
    try:
        index = int(value.get("step_index", fallback_index) or fallback_index)
    except Exception:
        index = fallback_index
    value["step_index"] = index
    for field in ("source_step_label", "action", "action_span", "object", "object_span", "condition", "result", "step_evidence"):
        value[field] = _clean(value.get(field, ""))
    for field in ("materials", "equipment"):
        raw = value.get(field, [])
        if not isinstance(raw, list):
            raw = []
        value[field] = [_clean(item) for item in raw if _clean(item)]
    return value


def validate_process_response(
    response: Mapping[str, Any],
    *,
    span: ProcessSpan,
    confidence_threshold: float = 0.72,
    max_steps: int = 40,
) -> ProcessValidation:
    raw_processes = response.get("processes", []) if isinstance(response, Mapping) else []
    if not isinstance(raw_processes, list):
        return ProcessValidation((), ("processes_not_list",), True)
    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    single_process = len(raw_processes) == 1

    for process_pos, raw_process in enumerate(raw_processes, start=1):
        prefix = f"process_{process_pos}"
        if not isinstance(raw_process, Mapping):
            errors.append(f"{prefix}:not_object")
            continue
        process = dict(raw_process)
        name = _clean(process.get("process_name", ""))
        process_type = _clean(process.get("process_type", ""))
        process_object = _clean(process.get("process_object", ""))
        object_type = _clean(process.get("process_object_type", ""))
        process_span = _clean(process.get("process_evidence_span", ""))
        try:
            confidence = float(process.get("confidence", 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        local_errors: list[str] = []
        if len(name) < 3 or len(name) > 100 or name.casefold() in _GENERIC_PROCESS_NAMES:
            local_errors.append("invalid_process_name")
        if process_type not in ALLOWED_PROCESS_TYPES:
            local_errors.append("invalid_process_type")
        if confidence < confidence_threshold:
            local_errors.append("process_confidence_below_threshold")
        if process_span and not span_supported(process_span, span.text):
            local_errors.append("process_evidence_span_not_supported")

        raw_steps = process.get("steps", [])
        if not isinstance(raw_steps, list):
            raw_steps = []
        if not 2 <= len(raw_steps) <= max_steps:
            local_errors.append("invalid_step_count")
        steps = [_normalize_step(step, i) for i, step in enumerate(raw_steps, start=1) if isinstance(step, Mapping)]
        steps.sort(key=lambda item: int(item.get("step_index", 0) or 0))
        indices = [int(step.get("step_index", 0) or 0) for step in steps]
        if indices != list(range(1, len(steps) + 1)):
            local_errors.append(f"non_contiguous_step_indices:{indices}")

        source_numbers: list[int] = []
        evidence_seen: set[str] = set()
        for step_pos, step in enumerate(steps, start=1):
            step_prefix = f"step_{step_pos}"
            action = _clean(step.get("action", ""))
            action_span = _clean(step.get("action_span", ""))
            obj = _clean(step.get("object", ""))
            object_span = _clean(step.get("object_span", ""))
            condition = _clean(step.get("condition", ""))
            result = _clean(step.get("result", ""))
            evidence = _clean(step.get("step_evidence", ""))
            if not action or action.casefold() in _GENERIC_ACTIONS or len(action) > 40 or _SENTENCE_PUNCT.search(action):
                local_errors.append(f"{step_prefix}:invalid_action")
            if not action_span or not span_supported(action_span, span.text):
                local_errors.append(f"{step_prefix}:action_span_not_supported")
            if object_span and not span_supported(object_span, span.text):
                local_errors.append(f"{step_prefix}:object_span_not_supported")
            if not obj and not result:
                local_errors.append(f"{step_prefix}:empty_object_and_result")
            if not evidence or not span_supported(evidence, span.text):
                local_errors.append(f"{step_prefix}:step_evidence_not_supported")
            elif _compact(evidence) in evidence_seen:
                local_errors.append(f"{step_prefix}:duplicate_step_evidence")
            else:
                evidence_seen.add(_compact(evidence))
            if _BAD_RELATIVE.search(condition) or _BAD_RELATIVE.search(result):
                local_errors.append(f"{step_prefix}:unresolved_relative_reference")
            number = _label_number(step.get("source_step_label", ""))
            if number:
                source_numbers.append(number)

        # When a detector found a clean 1..N numbered sequence and the model says
        # there is one process, require complete coverage. This prevents 1,3,5,8
        # fragments from reaching the final graph.
        expected = list(span.expected_labels)
        if single_process and len(expected) >= 2:
            if expected[0] != 1:
                local_errors.append(f"source_sequence_starts_after_one:{expected}")
            elif expected != list(range(1, max(expected) + 1)):
                local_errors.append(f"source_numbering_gap_in_evidence:{expected}")
            else:
                returned = sorted(set(source_numbers))
                if returned != expected:
                    missing = sorted(set(expected) - set(returned))
                    extra = sorted(set(returned) - set(expected))
                    local_errors.append(f"source_step_coverage_mismatch:missing={missing};extra={extra}")

        if process_object:
            valid_subject, reason = subject_is_valid(process_object, language=span.language)
            if not valid_subject and not name:
                local_errors.append(f"invalid_process_object:{reason}")
        process.update(
            {
                "process_name": name,
                "process_type": process_type,
                "process_object": process_object,
                "process_object_type": object_type,
                "process_evidence_span": process_span,
                "confidence": confidence,
                "steps": steps,
            }
        )
        if local_errors:
            errors.extend(f"{prefix}:{item}" for item in local_errors)
        else:
            valid.append(process)

    repair_recommended = bool(errors) and bool(raw_processes)
    return ProcessValidation(tuple(valid), tuple(errors), repair_recommended)


def compile_process_graphs(
    processes: Sequence[Mapping[str, Any]],
    *,
    span: ProcessSpan,
    ontology: PropertyOntologyIndex,
    document_id: str,
    heading_path: str,
    source_locator: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    graphs: list[dict[str, Any]] = []
    prop = ontology.resolve("操作步骤")
    if prop.canonical_name == "未解析属性":
        prop_name = "操作步骤"
        prop_category = "方法/工艺"
    else:
        prop_name = prop.canonical_name
        prop_category = prop.category or "方法/工艺"

    for process_pos, process in enumerate(processes, start=1):
        process_name = _clean(process.get("process_name", ""))
        process_type = _clean(process.get("process_type", ""))
        process_object = _clean(process.get("process_object", ""))
        process_object_type = _clean(process.get("process_object_type", ""))
        confidence = float(process.get("confidence", 0.0) or 0.0)
        process_id = _hash(
            "process",
            document_id,
            span.book_title,
            heading_path,
            span.line_start,
            process_name,
            process_pos,
            length=20,
        )
        steps = list(process.get("steps", []) or [])
        step_ids = [f"{process_id}:step:{index:03d}" for index in range(1, len(steps) + 1)]
        graph_steps: list[dict[str, Any]] = []
        for offset, step in enumerate(steps):
            index = offset + 1
            action = _clean(step.get("action", ""))
            obj = _clean(step.get("object", ""))
            result = _clean(step.get("result", ""))
            condition = _clean(step.get("condition", ""))
            label = _clean(step.get("source_step_label", "")) or f"步骤{index}"
            subject = obj or process_object or process_name
            valid_subject, _ = subject_is_valid(subject, language=span.language)
            if not valid_subject:
                subject = process_object or process_name
            subject_type = _ENTITY_TYPE_MAP.get(process_object_type, "材料" if process_object else "方法/模型")
            value = action if not obj or obj in action else f"{action}{obj}"
            step_id = step_ids[offset]
            previous_id = step_ids[offset - 1] if offset > 0 else ""
            next_id = step_ids[offset + 1] if offset + 1 < len(step_ids) else ""
            row = {name: "" for name in SCHEMA59_COLUMNS}
            fact_digest = hashlib.sha1(
                f"{document_id}|{process_id}|{step_id}|{subject}|{action}|{obj}|{condition}|{source_locator}".encode("utf-8")
            ).hexdigest()
            row.update(
                {
                    "fact_id": f"fact:{fact_digest[:20]}",
                    "graph_fact_key": f"gfk:{fact_digest}",
                    "文档ID": document_id,
                    "书名": span.book_title,
                    "章节路径": heading_path,
                    "来源定位": f"{source_locator};P{process_pos};S{index}",
                    "来源类型": "llm_process_flow_direct",
                    "主体名称": subject,
                    "主体类型": subject_type,
                    "事实类型": "方法步骤事实",
                    "predicate_raw": "操作步骤",
                    "edge_verb": "执行步骤",
                    "fact_node_label": "操作步骤事实",
                    "attribute_category": prop_category,
                    "attribute_category_key": f"cat:{_slug(prop_category)}",
                    "attribute_name": prop_name,
                    "attribute_key": f"cat:{_slug(prop_category)}::attr:{_slug(prop_name)}",
                    "尾实体/取值文本": value,
                    "value_hidden": "否",
                    "value_display_policy": "show_non_numeric_value",
                    "value_display_reason": "process_step_visible_v106p",
                    "normalized_value_text": value,
                    "normalized_unit": "",
                    "数值类型": "文本",
                    "value_type": "text",
                    "条件文本": condition,
                    "structured_condition_json": json.dumps([{"name": "步骤条件", "value_text": condition}], ensure_ascii=False) if condition else "[]",
                    "condition_attributes": json.dumps({"步骤条件": [condition]}, ensure_ascii=False) if condition else "{}",
                    "方法名称": process_name,
                    "process_id": process_id,
                    "process_name": process_name,
                    "process_type": process_type,
                    "step_id": step_id,
                    "step_index": str(index),
                    "step_label": label,
                    "step_action": action,
                    "step_object": obj,
                    "step_condition_text": condition,
                    "step_result_text": result,
                    "previous_step_id": previous_id,
                    "next_step_id": next_id,
                    "置信度": f"{confidence:.4f}",
                    "抽取来源": "qwen3_max_process_flow_specialized_v106p",
                    "证据文本": span.text,
                }
            )
            rows.append(row)
            graph_steps.append(
                {
                    "step_id": step_id,
                    "step_index": index,
                    "source_step_label": label,
                    "action": action,
                    "object": obj,
                    "condition": condition,
                    "result": result,
                    "previous_step_id": previous_id,
                    "next_step_id": next_id,
                    "step_evidence": _clean(step.get("step_evidence", "")),
                    "materials": list(step.get("materials", []) or []),
                    "equipment": list(step.get("equipment", []) or []),
                }
            )
        graphs.append(
            {
                "process_id": process_id,
                "process_name": process_name,
                "process_type": process_type,
                "process_object": process_object,
                "book_title": span.book_title,
                "heading_path": heading_path,
                "source_locator": source_locator,
                "source_span_id": span.span_id,
                "confidence": confidence,
                "steps": graph_steps,
            }
        )
    return rows, graphs


def validate_compiled_process_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_process: dict[str, list[Mapping[str, Any]]] = {}
    errors: list[dict[str, Any]] = []
    for row in rows:
        pid = str(row.get("process_id", "") or "")
        by_process.setdefault(pid, []).append(row)
    for pid, items in by_process.items():
        if not pid:
            errors.append({"process_id": pid, "reason": "empty_process_id"})
            continue
        ordered = sorted(items, key=lambda row: int(row.get("step_index", 0) or 0))
        indices = [int(row.get("step_index", 0) or 0) for row in ordered]
        if indices != list(range(1, len(ordered) + 1)) or len(ordered) < 2:
            errors.append({"process_id": pid, "reason": "invalid_step_sequence", "indices": indices})
            continue
        ids = [str(row.get("step_id", "") or "") for row in ordered]
        if len(set(ids)) != len(ids) or any(not item for item in ids):
            errors.append({"process_id": pid, "reason": "invalid_step_ids"})
            continue
        for offset, row in enumerate(ordered):
            expected_prev = ids[offset - 1] if offset > 0 else ""
            expected_next = ids[offset + 1] if offset + 1 < len(ids) else ""
            if str(row.get("previous_step_id", "") or "") != expected_prev:
                errors.append({"process_id": pid, "step": offset + 1, "reason": "previous_link_mismatch"})
            if str(row.get("next_step_id", "") or "") != expected_next:
                errors.append({"process_id": pid, "step": offset + 1, "reason": "next_link_mismatch"})
    return {
        "ok": not errors,
        "process_count": len(by_process),
        "step_rows": len(rows),
        "errors": errors[:100],
        "error_count": len(errors),
    }


__all__ = [
    "ALLOWED_PROCESS_TYPES", "ProcessValidation", "validate_process_response", "compile_process_graphs",
    "validate_compiled_process_rows",
]
