from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .english_aliases import load_english_property_aliases, load_english_relation_aliases, relation_prompt_examples
from .language_compat import is_english_like


ENTITY_TYPES = [
    "材料", "材料类别", "配方/材料组分", "样品/产品", "性能参数", "过程/现象",
    "方法/模型", "设备/系统", "条件/环境", "机构/人员", "其他明确实体",
]

RELATION_TYPES = [
    "属性", "组成", "分类", "定义", "用途", "功能", "影响", "因果", "比较",
    "方法", "步骤", "位置", "连接", "条件", "命名/别名",
]


def _english_resource_examples() -> tuple[list[str], list[str]]:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    property_rows = load_english_property_aliases(config_dir / "english_property_aliases.tsv")
    relation_rows = load_english_relation_aliases(config_dir / "english_relation_aliases.tsv")
    property_examples = [
        f"{str(row.get('alias', '')).strip()} → {str(row.get('canonical_name', '')).strip()}"
        for row in property_rows[:80]
        if str(row.get("alias", "") or "").strip() and str(row.get("canonical_name", "") or "").strip()
    ]
    return property_examples, relation_prompt_examples(relation_rows, limit=40)


def _english_constraints(language: str, *, process: bool = False) -> list[str]:
    if not is_english_like(language):
        return []
    constraints = [
        "证据包含 English 或中英混排内容：subject、value、subject_span、value_span 和 evidence span 必须保持英文原文，不得翻译成中文。",
        "subject_type、relation_type、规范 property 和 process_type 仍必须使用给定中文本体枚举。",
        "不得把 it、this、that、which、only、also、therefore、the result、the method、the process 等代词、连接词或话语片段作为主体。",
        "不得把 is、are、has、can、may、depends on、consists of、is defined as、is used for 等关系或助动词残片包含在主体末尾。",
        "必须正确处理 not、no、without、independent of、does not、is not 等否定；不得反转为肯定事实。",
        "遇到 respectively、in the order listed、the former/the latter 等对应结构时必须逐项对齐；无法可靠对应则不返回。",
        "may/can 表示能力或可能性时，不得改写成已经发生的确定事件。",
        "ammonium perchlorate (AP)、AP (ammonium perchlorate) 等显式全称缩写关系应保留英文并可输出命名/别名事实，不得翻译主体。",
        "属性事实必须把英文属性映射为 preferred_property_names 中已有的中文规范属性；无法映射时不要自造中文属性。",
    ]
    if process:
        constraints.extend(
            [
                "英文流程中的 process_name、action、object、condition、result 和证据跨度保持英文原文，process_type 使用中文枚举。",
                "识别 Preparation procedure、Experimental procedure、Synthesis procedure、Manufacturing process、Test procedure 等流程标题。",
                "识别 Step 1、First、Then、Next、Subsequently、Finally 以及 weigh、add、mix、stir、heat、cool、filter、wash、dry、press、cast、cure、measure、calculate 等可执行步骤。",
                "First, this chapter introduces... Then it discusses... 等文章组织顺序不是工艺流程。",
            ]
        )
    return constraints


def _attach_language_payload(payload: dict[str, Any], language: str) -> dict[str, Any]:
    if not is_english_like(language):
        return payload
    property_examples, relation_examples = _english_resource_examples()
    payload["source_language"] = language
    payload["english_property_alias_examples"] = property_examples
    payload["english_relation_alias_examples"] = relation_examples
    return payload


def candidate_review_system(language: str = "zh") -> str:
    base = (
        "你是含能材料与火炸药文献知识抽取的严格语义校正器。"
        "必须只依据给出的证据，不得使用外部知识补充事实。"
        "当前候选主体和关系可能严重错误，必须从证据重新判断主体边界、实体类型、关系、值、否定极性和并列对应。"
        "输出一个合法 JSON 对象，不要输出解释性正文。"
    )
    if is_english_like(language):
        return base + " The evidence may be English; preserve English source spans while using Chinese ontology labels."
    return base


def candidate_review_payload(
    *,
    book_title: str,
    heading_path: str,
    source_locator: str,
    evidence: str,
    candidates: Sequence[Mapping[str, Any]],
    property_names: Sequence[str],
    language: str = "zh",
) -> Mapping[str, Any]:
    compact = []
    for item in candidates:
        compact.append(
            {
                "candidate_id": item.get("candidate_id"),
                "subject": item.get("subject"),
                "subject_type": item.get("subject_type"),
                "property": item.get("property"),
                "relation_type": item.get("relation_type"),
                "value": item.get("value"),
                "unit": item.get("unit"),
                "condition": item.get("condition"),
                "source_type": item.get("source_type"),
                "confidence": item.get("confidence"),
            }
        )
    payload: dict[str, Any] = {
        "task": "candidate_fact_reextraction_and_adjudication",
        "output_language": "zh-CN",
        "constraints": [
            "对每个 candidate_id 必须返回且只返回一条 decision。",
            "action 只能是 keep、repair、reject。",
            "keep 或 repair 时，主体和取值必须由证据直接支持；不得把句子残片、关系词、否定词、程度副词、属性名当主体。",
            "subject_span 和 value_span 必须是证据中的连续原文片段；没有可靠片段时必须 reject。",
            "必须保留否定极性。例如“不受压力影响”不得改成“受压力影响”。",
            "遇到‘分别为’、多主体多值、表格多级表头时必须一一对应；无法可靠对应则 reject。",
            "表格中的优等品、一级品、颗粒等通常不是独立主体，必须结合表题、行名和列名恢复完整主体。",
            "property 优先使用给定规范属性名；证据明确但列表无合适项时可用简短原文属性。",
            "relation_type 必须从给定集合选择。",
            "subject_type 必须从给定集合选择。",
            "confidence 是对修正后事实语义正确性的估计，范围 0 到 1。",
            *_english_constraints(language),
        ],
        "book_title": book_title,
        "heading_path": heading_path,
        "source_locator": source_locator,
        "evidence": evidence,
        "entity_types": ENTITY_TYPES,
        "relation_types": RELATION_TYPES,
        "preferred_property_names": list(property_names),
        "candidates": compact,
        "response_schema": {
            "decisions": [
                {
                    "candidate_id": "原 candidate_id",
                    "action": "keep|repair|reject",
                    "subject": "完整规范主体",
                    "subject_type": "给定实体类型之一",
                    "property": "规范属性或简短原文关系名",
                    "relation_type": "给定关系类型之一",
                    "value": "证据支持的值或尾实体",
                    "unit": "单位，无则空字符串",
                    "condition": "条件，无则空字符串",
                    "polarity": "肯定|否定",
                    "subject_span": "证据中的连续原文片段",
                    "value_span": "证据中的连续原文片段",
                    "confidence": 0.0,
                    "reason": "一句话说明",
                }
            ]
        },
    }
    return _attach_language_payload(payload, language)


def direct_extraction_system(language: str = "zh") -> str:
    base = (
        "你是含能材料、火药、炸药、推进剂与火工品文献的高召回知识抽取器。"
        "只能抽取当前证据明确表达的事实，不得利用外部知识，不得总结推断。"
        "主体必须是完整实体或明确科学概念，严禁句子残片。"
        "输出一个合法 JSON 对象，不要输出解释性正文。"
    )
    if is_english_like(language):
        return base + " The evidence may be English; preserve English entities and spans while selecting Chinese ontology labels."
    return base


def direct_extraction_payload(
    *,
    unit_kind: str,
    unit_id: str,
    book_title: str,
    heading_path: str,
    source_locator: str,
    evidence: str,
    property_names: Sequence[str],
    max_facts: int,
    table_id: str = "",
    table_title: str = "",
    language: str = "zh",
) -> Mapping[str, Any]:
    table_constraints = [
        "若证据是表格，必须结合完整表题/章节、表头、行名与列名确定主体和属性。",
        "多级表头、合并单元格、等级列和配方编号不得脱离父级主体。",
        "多主体多值只有在可以可靠一一对应时才拆分。",
    ] if unit_kind == "table" else [
        "正文中允许跨相邻句继承明确主体，但不得跨越主题变化或凭空继承。",
        "识别定义、组成、分类、性能参数、用途、作用、因果、比较、制备方法和操作步骤。",
        "必须正确识别否定、转折、条件和‘分别为’结构。",
    ]
    payload: dict[str, Any] = {
        "task": "direct_scientific_fact_extraction",
        "output_language": "zh-CN",
        "unit_kind": unit_kind,
        "unit_id": unit_id,
        "constraints": [
            f"最多返回 {max_facts} 条高价值事实；证据没有可靠事实时返回空 facts。",
            "每条事实必须有完整主体、关系/属性和值。",
            "subject_span 和 value_span 必须是证据中的连续原文片段。",
            "不得抽取目录、页码、参考文献、公式残片、图号本身、泛泛评价或没有明确主体的句子。",
            "不得把‘法、图、所、称、只、仅、均、都、不仅、缺点、优点’等孤立词作为主体。",
            "不得把触发词前整段句子当主体；主体应是最小但完整的科学实体或概念。",
            "property 优先使用规范属性名；列表中没有合适项时可用简短原文关系名。",
            "relation_type 和 subject_type 必须从给定集合选择。",
            "polarity 必须保留原文肯定或否定。",
            "confidence 范围 0 到 1，低于 0.65 的不应返回。",
            *table_constraints,
            *_english_constraints(language),
        ],
        "book_title": book_title,
        "heading_path": heading_path,
        "source_locator": source_locator,
        "evidence": evidence,
        "entity_types": ENTITY_TYPES,
        "relation_types": RELATION_TYPES,
        "preferred_property_names": list(property_names),
        "response_schema": {
            "facts": [
                {
                    "subject": "完整规范主体",
                    "subject_type": "给定实体类型之一",
                    "property": "规范属性或简短原文关系名",
                    "relation_type": "给定关系类型之一",
                    "value": "值或尾实体",
                    "unit": "单位，无则空字符串",
                    "condition": "条件，无则空字符串",
                    "polarity": "肯定|否定",
                    "subject_span": "证据中的连续原文片段",
                    "value_span": "证据中的连续原文片段",
                    "confidence": 0.0,
                }
            ]
        },
    }
    if unit_kind == "table":
        payload["table_id"] = table_id
        payload["table_title"] = table_title
    return _attach_language_payload(payload, language)


PROCESS_TYPES = ["制备工艺", "试验流程", "装配流程", "计算流程", "处理流程", "操作流程"]


def process_extraction_system(language: str = "zh") -> str:
    base = (
        "你是含能材料、火炸药、推进剂和火工品文献的工艺流程专项抽取器。"
        "必须把同一工艺作为整体抽取，一次返回完整、有序的步骤数组。"
        "只抽取可执行的制备、试验、装配、计算或处理流程；文章叙述顺序、发展历史、理论机理阶段不是操作流程。"
        "只能依据证据，不得补写证据没有的步骤、条件或结果。"
        "输出一个合法 JSON 对象，不要输出解释性正文。"
    )
    if is_english_like(language):
        return base + " The process evidence may be English; preserve English process and step text while using Chinese process types."
    return base


def process_extraction_payload(
    *,
    span_id: str,
    unit_kind: str,
    book_title: str,
    heading_path: str,
    source_locator: str,
    evidence: str,
    expected_labels: Sequence[int],
    process_type_hint: str,
    max_processes: int,
    max_steps: int,
    language: str = "zh",
) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "task": "complete_process_flow_extraction",
        "output_language": "zh-CN",
        "span_id": span_id,
        "unit_kind": unit_kind,
        "book_title": book_title,
        "heading_path": heading_path,
        "source_locator": source_locator,
        "process_type_hint": process_type_hint,
        "source_detected_step_numbers": list(expected_labels),
        "constraints": [
            f"最多返回 {max_processes} 个独立流程，每个流程最多 {max_steps} 个步骤。",
            "只有至少两个具有明确先后顺序的可执行步骤才是流程；否则返回空 processes。",
            "不得把‘首先介绍、其次讨论、最后总结’等文章组织顺序抽成流程。",
            "不得把研制时间、研制国家、发明者、优缺点、定义或历史沿革抽成步骤。",
            "链引发、链增长、链终止等自然反应机理阶段，除非证据明确给出人工可执行操作，否则不得作为操作工艺。",
            "必须从完整证据恢复同一流程的全部步骤，不得逐句拆成多个流程。",
            "source_step_label、action_span、object_span、step_evidence 必须是证据中的连续原文片段；没有可靠片段时不得返回该步骤。",
            "action 必须是明确可执行动词，如称取、加入、搅拌、加热、过滤、洗涤、干燥、装填、测试、计算、迭代。",
            "‘步骤、过程、方法、首先、其次、性能调节、由规则推理’不能单独作为 action。",
            "step_object 或 result 至少一项非空。条件应写成具体条件，不得保留‘上一步、前述步骤、其它之前的步骤’等无法落地的相对指代。",
            "若原文含编号步骤，必须覆盖 source_detected_step_numbers 中属于该流程的所有编号；不得跳号，不得编造缺失编号。",
            "多个方法或多个配方的步骤不得混入同一流程。",
            "process_name 必须是具体名称，不能只写‘方法、过程、步骤、流程’。",
            "process_type 必须从给定集合选择。",
            "confidence 是整个流程完整且步骤对应正确的置信度，范围0到1。",
            *_english_constraints(language, process=True),
        ],
        "process_types": PROCESS_TYPES,
        "evidence": evidence,
        "response_schema": {
            "processes": [
                {
                    "process_name": "具体流程名称",
                    "process_type": "给定流程类型之一",
                    "process_object": "流程总体处理对象或目标产品",
                    "process_object_type": "材料|样品/产品|设备/系统|方法/模型|其他明确实体",
                    "process_evidence_span": "证据中概括该流程的连续原文片段，无则空字符串",
                    "confidence": 0.0,
                    "steps": [
                        {
                            "source_step_label": "原文步骤编号或顺序词，无则空字符串",
                            "step_index": 1,
                            "action": "可执行动作",
                            "action_span": "证据中的动作原文",
                            "object": "操作对象",
                            "object_span": "证据中的对象原文，无则空字符串",
                            "materials": ["材料或组分"],
                            "equipment": ["设备"],
                            "condition": "温度、时间、压力、顺序等具体条件",
                            "result": "该步直接结果，无则空字符串",
                            "step_evidence": "完整步骤的连续原文证据",
                        }
                    ],
                }
            ]
        },
    }
    return _attach_language_payload(payload, language)


def process_repair_payload(
    *,
    original_payload: Mapping[str, Any],
    original_response: Mapping[str, Any],
    validation_errors: Sequence[str],
) -> Mapping[str, Any]:
    return {
        **dict(original_payload),
        "task": "repair_incomplete_process_flow_once",
        "previous_response": original_response,
        "validation_errors": list(validation_errors),
        "repair_constraints": [
            "这是唯一一次修复。必须重新核对完整证据并返回完整 processes 数组。",
            "只修复证据明确存在的步骤、编号、边界和字段；不得为了补齐编号编造步骤。",
            "若仍无法形成至少两个连续、完整、可执行的步骤，则返回空 processes。",
        ],
    }


__all__ = [
    "ENTITY_TYPES", "RELATION_TYPES", "candidate_review_system", "candidate_review_payload",
    "direct_extraction_system", "direct_extraction_payload",
    "PROCESS_TYPES", "process_extraction_system", "process_extraction_payload", "process_repair_payload",
]
