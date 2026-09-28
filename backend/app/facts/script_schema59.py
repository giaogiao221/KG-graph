from __future__ import annotations

from collections.abc import Mapping
import json

from app.facts.schema59 import FIELD, SCHEMA59_COLUMNS


SCRIPT_SCHEMA59_COLUMNS: tuple[str, ...] = (
    "fact_id", "graph_fact_key", "文档ID", "书名", "章节路径", "来源定位", "来源类型",
    "所属表格ID", "所属表格标题", "主体名称", "主体类型", "事实类型", "predicate_raw",
    "edge_verb", "fact_node_label", "attribute_category", "attribute_category_key",
    "attribute_name", "attribute_key", "尾实体/取值文本", "value_hidden",
    "value_display_policy", "value_display_reason", "normalized_value_text",
    "normalized_value_num", "normalized_unit", "数值", "范围下限", "范围上限", "单位",
    "数值类型", "value_type", "条件文本", "structured_condition_json",
    "condition_attributes", "方法名称", "process_id", "process_name", "process_type",
    "step_id", "step_index", "step_label", "step_action", "step_object",
    "step_condition_text", "step_result_text", "previous_step_id", "next_step_id",
    "component_name", "component_role", "component_amount_text", "component_amount_value",
    "component_amount_unit", "component_amount_attribute", "table_semantic_type",
    "condition_metric_role", "置信度", "抽取来源", "证据文本",
)

if len(SCRIPT_SCHEMA59_COLUMNS) != 59 or len(set(SCRIPT_SCHEMA59_COLUMNS)) != 59:
    raise RuntimeError("script schema59 contract must contain 59 unique columns")


def _first(row: Mapping[str, str], *columns: str) -> str:
    for column in columns:
        value = str(row.get(column, ""))
        if value.strip():
            return value
    return ""


def platform_to_script_row(row: Mapping[str, str]) -> dict[str, str]:
    """Project an internal fact onto the script's exact 59-column contract."""
    result = {column: "" for column in SCRIPT_SCHEMA59_COLUMNS}
    try:
        extra = json.loads(str(row.get("附加信息", "")))
        original = extra.get("kgchouqu_script_row", {}) if isinstance(extra, dict) else {}
        if isinstance(original, dict) and set(original) == set(SCRIPT_SCHEMA59_COLUMNS):
            result.update({column: str(original.get(column, "")) for column in SCRIPT_SCHEMA59_COLUMNS})
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    subject = _first(row, FIELD["subject"])
    property_name = _first(row, FIELD["property"], "原始属性")
    value = _first(row, FIELD["value"], "客体", "原始值")
    unit = _first(row, FIELD["unit"], "原始单位")
    condition = _first(row, FIELD["condition"])
    result.update({
        "fact_id": _first(row, "fact_id"),
        "graph_fact_key": _first(row, "graph_fact_key"),
        "文档ID": _first(row, FIELD["document_id"]),
        "书名": _first(row, FIELD["book_title"]),
        "章节路径": _first(row, "章节", "章节标题"),
        "来源定位": _first(row, FIELD["source_locator"]),
        "来源类型": _first(row, FIELD["source_kind"]),
        "所属表格ID": _first(row, FIELD["table_id"]),
        "所属表格标题": _first(row, "表标题"),
        "主体名称": subject,
        "主体类型": _first(row, "主体类型"),
        "事实类型": _first(row, "属性类型"),
        "predicate_raw": _first(row, "原始属性", FIELD["property"]),
        "edge_verb": property_name,
        "attribute_category": _first(row, "属性类型"),
        "attribute_name": property_name,
        "attribute_key": property_name,
        "尾实体/取值文本": _first(row, "客体", "原始值", FIELD["value"]),
        "normalized_value_text": _first(row, "归一化数值", FIELD["value"]),
        "normalized_value_num": _first(row, "归一化数值"),
        "normalized_unit": unit,
        "数值": value,
        "单位": unit,
        "条件文本": condition,
        "置信度": _first(row, "置信度"),
        "抽取来源": _first(row, FIELD["extraction_source"], FIELD["route"]),
        "证据文本": _first(row, FIELD["evidence_text"]),
    })
    return result


def script_to_platform_values(row: Mapping[str, str]) -> dict[str, str]:
    """Extract platform business fields from one exact script-schema row."""
    if tuple(row.keys()) != SCRIPT_SCHEMA59_COLUMNS:
        raise ValueError("script schema59 header is invalid")
    result = {column: "" for column in SCHEMA59_COLUMNS}
    result.update({
        "fact_id": _first(row, "fact_id"),
        "graph_fact_key": _first(row, "graph_fact_key"),
        FIELD["document_id"]: _first(row, "文档ID"),
        FIELD["book_title"]: _first(row, "书名"),
        FIELD["subject"]: _first(row, "主体名称"),
        FIELD["property"]: _first(row, "attribute_name", "edge_verb", "predicate_raw"),
        FIELD["value"]: _first(row, "数值", "normalized_value_text", "尾实体/取值文本"),
        FIELD["unit"]: _first(row, "normalized_unit", "单位"),
        FIELD["condition"]: _first(row, "条件文本", "step_condition_text"),
        FIELD["table_id"]: _first(row, "所属表格ID"),
        FIELD["source_locator"]: _first(row, "来源定位"),
        FIELD["source_kind"]: _first(row, "来源类型"),
        FIELD["extraction_source"]: _first(row, "抽取来源"),
        FIELD["evidence_text"]: _first(row, "证据文本"),
        "置信度": _first(row, "置信度"),
        "章节": _first(row, "章节路径"),
        "主体类型": _first(row, "主体类型"),
        "属性类型": _first(row, "attribute_category", "事实类型"),
        "表标题": _first(row, "所属表格标题"),
        "原始属性": _first(row, "predicate_raw"),
        "原始值": _first(row, "尾实体/取值文本"),
        "附加信息": json.dumps(
            {"kgchouqu_script_row": {column: str(row[column]) for column in SCRIPT_SCHEMA59_COLUMNS}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    })
    return result
