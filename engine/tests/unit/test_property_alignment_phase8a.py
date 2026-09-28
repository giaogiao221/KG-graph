from __future__ import annotations

import csv
import json
from pathlib import Path

from book_engine.ontology.property_alignment_engine import PropertyAlignmentEngine


def _write_ontology(path: Path) -> None:
    rows = [
        {
            "property_id": "p_density_propellant",
            "alias": "密度",
            "canonical_name": "密度",
            "attribute_category": "物理属性",
            "root_system": "发射药",
            "ontology_path": "发射药/性质性能/基础理化性质/密度",
            "source": "test",
            "priority": "100",
            "unit_dimensions": "density",
            "allowed_value_roles": "",
            "context_positive": "",
            "context_negative": "",
        },
        {
            "property_id": "p_impact_sensitivity",
            "alias": "撞击感度",
            "canonical_name": "撞击感度",
            "attribute_category": "安全性能",
            "root_system": "发射药",
            "ontology_path": "发射药/性质性能/安全性能/撞击感度",
            "source": "test",
            "priority": "100",
            "unit_dimensions": "sensitivity",
            "allowed_value_roles": "",
            "context_positive": "安全|感度",
            "context_negative": "",
        },
        {
            "property_id": "p_absorption_physical",
            "alias": "吸湿性",
            "canonical_name": "吸湿性",
            "attribute_category": "物理属性",
            "root_system": "火工药剂",
            "ontology_path": "火工药剂/性质性能/基础理化性质/吸湿性",
            "source": "test",
            "priority": "100",
            "unit_dimensions": "",
            "allowed_value_roles": "",
            "context_positive": "基础理化性质",
            "context_negative": "",
        },
        {
            "property_id": "p_absorption_storage",
            "alias": "吸湿性",
            "canonical_name": "吸湿性",
            "attribute_category": "储存/运输",
            "root_system": "火工药剂",
            "ontology_path": "火工药剂/性质性能/安全操作与储运特性/吸湿性",
            "source": "test",
            "priority": "100",
            "unit_dimensions": "",
            "allowed_value_roles": "",
            "context_positive": "储存|运输|包装",
            "context_negative": "",
        },
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _policy(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "exact_threshold": 0.9,
                "context_threshold": 0.78,
                "ambiguity_margin": 0.07,
                "ambiguous_policy": "exclude",
                "unmapped_policy": "preserve_other",
                "llm_enabled": False,
            }
        ),
        encoding="utf-8",
    )


def test_exact_rawarrange_style_alignment(tmp_path: Path):
    ontology = tmp_path / "ontology.tsv"
    policy = tmp_path / "policy.json"
    _write_ontology(ontology)
    _policy(policy)
    engine = PropertyAlignmentEngine(ontology, policy)
    decision = engine.align_row(
        {
            "fact_id": "f1",
            "predicate_raw": "撞击感度/%",
            "主体类型": "发射药",
            "单位": "%",
            "证据文本": "该发射药的撞击感度为12%",
        }
    )
    assert decision.accepted
    assert decision.canonical_name == "撞击感度"
    assert decision.attribute_category == "安全性能"


def test_context_disambiguates_storage_absorption(tmp_path: Path):
    ontology = tmp_path / "ontology.tsv"
    policy = tmp_path / "policy.json"
    _write_ontology(ontology)
    _policy(policy)
    engine = PropertyAlignmentEngine(ontology, policy)
    decision = engine.align_row(
        {
            "fact_id": "f2",
            "predicate_raw": "吸湿性",
            "主体类型": "火工药剂",
            "章节路径": "储存与运输",
            "证据文本": "包装储存过程中应控制其吸湿性",
        }
    )
    assert decision.accepted
    assert decision.attribute_category == "储存/运输"


def test_unmapped_is_preserved_as_other(tmp_path: Path):
    ontology = tmp_path / "ontology.tsv"
    policy = tmp_path / "policy.json"
    _write_ontology(ontology)
    _policy(policy)
    engine = PropertyAlignmentEngine(ontology, policy)
    decision = engine.align_row({"fact_id": "f3", "predicate_raw": "未知自定义指标"})
    assert decision.accepted
    assert decision.status == "unmapped"
    assert decision.attribute_category == "其他属性"


def test_alignment_does_not_change_row_shape(tmp_path: Path):
    ontology = tmp_path / "ontology.tsv"
    policy = tmp_path / "policy.json"
    _write_ontology(ontology)
    _policy(policy)
    engine = PropertyAlignmentEngine(ontology, policy)
    row = {
        "fact_id": "f4",
        "graph_fact_key": "g4",
        "文档ID": "d",
        "主体名称": "样品A",
        "主体类型": "发射药",
        "predicate_raw": "密度/(g/cm3)",
        "attribute_name": "密度",
        "尾实体/取值文本": "1.80",
        "normalized_unit": "g/cm3",
        "单位": "g/cm3",
        "置信度": "0.95",
        "证据文本": "样品A密度为1.80 g/cm3",
    }
    aligned, decisions = engine.align_rows([row])
    assert len(aligned) == 1
    assert set(aligned[0]) == set(row) | {"attribute_category", "attribute_category_key", "attribute_key", "fact_node_label"}
    assert aligned[0]["attribute_name"] == "密度"


def test_alignment_keeps_repeated_process_steps_as_distinct_facts(tmp_path: Path):
    ontology = tmp_path / "ontology.tsv"
    policy = tmp_path / "policy.json"
    _write_ontology(ontology)
    _policy(policy)
    engine = PropertyAlignmentEngine(ontology, policy)
    common = {
        "文档ID": "doc:test",
        "主体名称": "固体",
        "主体类型": "材料",
        "predicate_raw": "工艺步骤",
        "attribute_name": "工艺步骤",
        "尾实体/取值文本": "过滤",
        "来源定位": "L2451-L2451",
        "来源类型": "text_process_step",
        "process_id": "proc:test",
        "置信度": "0.87",
        "证据文本": "反应结束后过滤，加入丙酮搅拌，再次过滤。",
    }
    rows = [
        dict(common, fact_id="before:7", graph_fact_key="before-key:7", step_id="step:7", step_index="7"),
        dict(common, fact_id="before:10", graph_fact_key="before-key:10", step_id="step:10", step_index="10"),
    ]

    aligned, decisions = engine.align_rows(rows)

    assert len(decisions) == 2
    assert len(aligned) == 2
    assert aligned[0]["fact_id"] != aligned[1]["fact_id"]
    assert aligned[0]["graph_fact_key"] != aligned[1]["graph_fact_key"]
