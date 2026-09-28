from __future__ import annotations

import hashlib


SCHEMA59_COLUMNS: tuple[str, ...] = (
    "fact_id", "graph_fact_key", "文档ID", "书名", "章节", "章节标题", "页码",
    "段落ID", "句子ID", "表格ID", "行号", "列号", "主体", "主体类型", "属性",
    "属性类型", "客体", "客体类型", "数值", "归一化数值", "单位", "条件", "时间",
    "地点", "限定词", "极性", "语言", "置信度", "审核状态", "冲突组", "证据哈希",
    "证据上下文", "证据起始", "证据结束", "来源定位", "来源类型", "路由", "执行器",
    "执行器版本", "模型配置ID", "模型调用ID", "文档哈希", "批次ID", "任务ID", "步骤ID",
    "幂等键", "模式版本", "表标题", "行标题", "列标题", "原始主体", "原始属性",
    "原始值", "原始单位", "附加信息", "溯源信息", "抽取时间", "抽取来源", "证据文本",
)

if len(SCHEMA59_COLUMNS) != 59 or len(set(SCHEMA59_COLUMNS)) != 59:
    raise RuntimeError("schema59 contract must contain 59 unique columns")


FIELD: dict[str, str] = {
    "document_id": "文档ID", "book_title": "书名", "subject": "主体",
    "property": "属性", "value": "数值", "unit": "单位", "condition": "条件",
    "table_id": "表格ID", "table_row": "行号", "table_column": "列号",
    "source_locator": "来源定位", "source_kind": "来源类型", "route": "路由",
    "model_config_id": "模型配置ID", "model_call_id": "模型调用ID",
    "review_status": "审核状态", "conflict_group": "冲突组",
    "evidence_hash": "证据哈希", "evidence_text": "证据文本",
    "provenance": "溯源信息", "step_id": "步骤ID",
    "execution_idempotency_key": "幂等键", "schema_version": "模式版本",
    "extraction_source": "抽取来源",
}


def canonical_evidence(value: str) -> str:
    return " ".join(value.split()).casefold()


def evidence_hash(value: str) -> str:
    return hashlib.sha256(canonical_evidence(value).encode("utf-8")).hexdigest()
