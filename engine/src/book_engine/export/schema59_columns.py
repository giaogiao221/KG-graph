from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from book_engine.export.schema59_contract import load_contract, validate_columns

# Fallback only. After Phase 6A installation, the captured legacy contract is the source of truth.
SCHEMA59_FALLBACK_COLUMNS = [
    "fact_id", "graph_fact_key", "文档ID", "书名", "章节路径", "来源定位", "来源类型", "所属表格ID",
    "所属表格标题", "主体名称", "主体类型", "事实类型", "predicate_raw", "edge_verb", "fact_node_label",
    "attribute_category", "attribute_category_key", "attribute_name", "attribute_key", "尾实体/取值文本",
    "value_hidden", "value_display_policy", "value_display_reason", "normalized_value_text", "normalized_value_num",
    "normalized_unit", "数值", "范围下限", "范围上限", "单位", "数值类型", "value_type", "条件文本",
    "structured_condition_json", "condition_attributes", "方法名称", "process_id", "process_name", "process_type",
    "step_id", "step_index", "step_label", "step_action", "step_object", "step_condition_text", "step_result_text",
    "previous_step_id", "next_step_id", "component_name", "component_role", "component_amount_text",
    "component_amount_value", "component_amount_unit", "component_amount_attribute", "table_semantic_type",
    "condition_metric_role", "置信度", "抽取来源", "证据文本",
]


def _default_contract_path() -> Path:
    # .../model/src/book_engine/export/schema59_columns.py -> .../model/config
    model_root = Path(__file__).resolve().parents[2]
    return model_root / "config" / "legacy_schema59_contract.json"


def load_schema59_columns() -> List[str]:
    override = os.environ.get("KGCHOUQU_SCHEMA59_CONTRACT", "").strip()
    contract_path = Path(override) if override else _default_contract_path()
    if contract_path.exists():
        columns = list(load_contract(contract_path)["columns"])
    else:
        columns = list(SCHEMA59_FALLBACK_COLUMNS)
    validate_columns(columns, required_count=59)
    return columns


SCHEMA59_COLUMNS = load_schema59_columns()

__all__ = ["SCHEMA59_COLUMNS", "SCHEMA59_FALLBACK_COLUMNS", "load_schema59_columns"]
