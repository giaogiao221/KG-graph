from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from book_engine.core.schemas import ConditionalFactRecord, TableBlock, TableCell, TableGrid
from book_engine.document.block_segmenter import TextBlock
from book_engine.document.semantic_block_splitter import split_multisubject_blocks
from book_engine.export.schema59_columns import SCHEMA59_COLUMNS
from book_engine.export.schema59_exporter import _write_rows
from book_engine.export.table_evidence_writer import (
    unescape_single_line,
    write_table_evidence_outputs,
)
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry
from book_engine.routing.text_subject_anchor_resolver import (
    TextSubjectAnchor,
    resolve_text_subject_anchors_with_audit,
)
from book_engine.tables.table_axis_llm_adjudicator import TableAxisLLMAdjudicator
from book_engine.text.process_step_extractor import extract_process_step_facts
from book_engine.text.process_step_llm_adjudicator import ProcessStepLLMAdjudicator


def _entry(name: str) -> SubjectRegistryEntry:
    return SubjectRegistryEntry(
        subject_id=f"subj:{name}",
        canonical_name=name,
        normalized_key=name.lower(),
        subject_type="单质炸药",
        status="confirmed",
        score=0.9,
        type_confidence=0.9,
    )


def test_multisubject_block_splits_only_at_subject_focus_change(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SEMANTIC_SPLIT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="B1",
        text="RDX的密度为1.80 g/cm3。 HMX的密度为1.91 g/cm3。",
        line_start=1,
        line_end=1,
        heading_path=["性能比较"],
        heading_title="性能比较",
        role="comparison",
    )
    output, audits = split_multisubject_blocks([block], [_entry("RDX"), _entry("HMX")])
    assert len(output) == 2
    assert output[0].text.startswith("RDX")
    assert output[1].text.startswith("HMX")
    assert all(item.action == "split" for item in audits)


def test_process_subject_anchor_uses_exact_source_span(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SUBJECT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="P1",
        text="Brill等人报道了以GAP基含能聚合物包覆CL－20 的制备方法和性能。首先进行交联。",
        line_start=1,
        line_end=1,
        heading_path=["应用", "包覆"],
        heading_title="包覆",
        role="process",
    )
    anchors, by_block, audits = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "GAP基含能聚合物包覆CL-20"
    assert anchors[0].source == "explicit_process_subject_span"
    assert anchors[0].status == "confirmed"
    assert by_block["P1"].subject == anchors[0].subject
    assert audits[0].deterministic_source == "explicit_process_subject_span"


def test_process_steps_are_structured_linked_and_do_not_absorb_discussion(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = (
        "首先通过GAP多元醇与HDI在溶液中交联形成凝胶，"
        "再将CL-20和凝胶溶解，通过冷冻干燥法除去溶剂，制备出包覆CL-20。"
        "其中，后续性能测试结果如图所示。"
    )
    block = TextBlock(
        block_id="P2",
        text=text,
        line_start=1,
        line_end=2,
        heading_path=["制备方法"],
        heading_title="制备方法",
        role="process",
    )
    anchor = TextSubjectAnchor("P2", "GAP包覆CL-20", "配方/材料体系", "heading_anchor", 0.93, "confirmed")
    records, audits, blocks = extract_process_step_facts([block], {"P2": anchor})
    assert len(records) == 3
    assert [item.step_action for item in records] == ["交联", "溶解", "除去"]
    assert [item.step_index for item in records] == [1, 2, 3]
    assert records[0].next_step_id == records[1].step_id
    assert records[1].previous_step_id == records[0].step_id
    assert records[1].next_step_id == records[2].step_id
    assert records[2].previous_step_id == records[1].step_id
    assert "性能测试" not in records[-1].step_label
    assert records[0].step_condition_text == "介质=在溶液中"
    assert "方法=通过冷冻干燥法" in records[2].step_condition_text
    assert "P2" in blocks
    assert len(audits) == 3


def test_toc_numbering_is_not_process_steps(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="TOC",
        text="3.1 结晶基本原理 ........ 46\n3.2 干燥设备 ........ 50\n3.3 装填密度 ........ 55\n3.4 测试方法 ........ 60\n3.5 反应机理 ........ 64",
        line_start=1,
        line_end=5,
        heading_path=["目录"],
        heading_title="工艺流程",
        role="process",
    )
    anchor = TextSubjectAnchor("TOC", "RDX", "单质炸药", "heading_anchor", 0.9, "confirmed")
    records, audits, blocks = extract_process_step_facts([block], {"TOC": anchor})
    assert records == []
    assert audits == []
    assert blocks == set()


def test_full_raw_table_is_authoritative_evidence_and_sidecar_hash_matches(tmp_path: Path):
    raw = "| 材料 | 密度/(g/cm3) |\n|---|---:|\n| RDX | 1.80 |"
    block = TableBlock("T1", "markdown_pipe", raw, 10, 12, heading="表1")
    cells = [
        [TableCell("T1", 0, 0, "材料"), TableCell("T1", 0, 1, "密度/(g/cm3)")],
        [TableCell("T1", 1, 0, "RDX"), TableCell("T1", 1, 1, "1.80")],
    ]
    grid = TableGrid("T1", "markdown_pipe", cells, 2, 2, 2, 2)
    record = ConditionalFactRecord(
        subject="RDX",
        subject_type="单质炸药",
        property_name="密度",
        value_text="1.80",
        unit="g/cm3",
        evidence=raw,
        record_id="R1",
        table_id="T1",
        row_index=1,
        column_index=1,
        row_header_path=["RDX"],
        column_header_path=["密度/(g/cm3)"],
        record_status="ready",
    )
    result = (block, grid, object(), [], [record], [], [], object())
    report = write_table_evidence_outputs(tmp_path, [block], [result])
    assert report["ok"] is True
    payload = json.loads((tmp_path / "step_table_evidence/table_evidence_store.jsonl").read_text(encoding="utf-8"))
    assert payload["raw_table_text"] == raw
    assert payload["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    local = (tmp_path / "step_table_evidence/table_fact_local_evidence.tsv").read_text(encoding="utf-8-sig")
    assert "R1" in local and "1.80" in local


def test_schema59_full_evidence_stays_one_physical_line_and_is_reversible(tmp_path: Path):
    raw = "| A | B |\n|---|---|\n| x | y |"
    row = {column: "" for column in SCHEMA59_COLUMNS}
    row["fact_id"] = "F1"
    row["graph_fact_key"] = "G1"
    row["证据文本"] = raw
    path = tmp_path / "graph_import_ready.tsv"
    report = _write_rows(path, [row])
    assert report["ok"] is True
    assert len(path.read_text(encoding="utf-8-sig").splitlines()) == 2
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        parsed = list(csv.DictReader(handle, delimiter="\t"))
    assert len(parsed) == 1
    assert unescape_single_line(parsed[0]["证据文本"]) == raw
    assert len(parsed[0]) == 59


def test_closed_set_llm_validators_cannot_invent_axes_or_steps():
    axis = TableAxisLLMAdjudicator._validate(
        {
            "topology": "invented",
            "orientation": "diagonal",
            "column_roles": {"0": "entity", "99": "value", "1": "invented"},
            "confidence": 2,
        },
        {0, 1},
        "entity_by_property",
        "row_subject",
    )
    assert axis.topology == "entity_by_property"
    assert axis.orientation == "row_subject"
    assert axis.column_roles == {0: "entity"}
    assert axis.confidence == 1.0

    process = ProcessStepLLMAdjudicator._validate(
        {"selected_candidate_ids": ["C2", "INVENTED", "C1"], "confidence": 0.9},
        {
            "C1": {"source_index": 1},
            "C2": {"source_index": 2},
        },
    )
    assert process.selected_candidate_ids == ("C1", "C2")
    assert process.status == "selected"


def test_bare_numbered_application_examples_are_not_steps(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="EXAMPLES",
        text="（1）高温蒸馏器：铜与不锈钢的焊接；（2）储槽：铜与软钢的焊接；（3）运输轨道连接：铜与钢的焊接。",
        line_start=1,
        line_end=1,
        heading_path=["应用", "金属包覆和焊接"],
        heading_title="金属包覆和焊接",
        role="process",
    )
    anchor = TextSubjectAnchor("EXAMPLES", "雷管", "火工品器件", "heading_anchor", 0.9, "confirmed")
    records, audits, blocks = extract_process_step_facts([block], {"EXAMPLES": anchor})
    assert records == []
    assert audits == []
    assert blocks == set()


def test_single_numbered_method_is_split_by_exact_operation_spans(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = "（1）热分解法。将原料粉碎至60目,放入焙烧炉中于500℃氧化焙烧,用氨水浸出,得到溶液。"
    block = TextBlock(
        block_id="METHOD",
        text=text,
        line_start=1,
        line_end=1,
        heading_path=["材料", "4.制备方法"],
        heading_title="4.制备方法",
        role="process",
    )
    anchor = TextSubjectAnchor("METHOD", "三氧化钼", "材料", "heading_anchor", 0.9, "confirmed")
    records, audits, blocks = extract_process_step_facts([block], {"METHOD": anchor})
    assert [item.step_action for item in records] == ["粉碎", "焙烧", "浸出"]
    assert all(item.step_label in text for item in records)
    assert records[0].step_label != text
    assert "METHOD" in blocks
    assert len(audits) == 3


def test_process_final_product_span_beats_intermediate_registry_mentions(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SUBJECT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="FINAL",
        text="将原料浸出得到钼酸铵溶液，再热分解得到三氧化钼。",
        line_start=1,
        line_end=1,
        heading_path=["抑烟剂", "4.制备方法"],
        heading_title="4.制备方法",
        role="process",
    )
    anchors, by_block, audits = resolve_text_subject_anchors_with_audit([block], [_entry("钼酸铵")])
    assert anchors[0].subject == "三氧化钼"
    assert anchors[0].source == "explicit_process_final_product_span"
    assert by_block["FINAL"].status == "confirmed"
    assert audits[0].deterministic_source == "explicit_process_final_product_span"


def test_reaction_after_is_temporal_bridge_not_a_standalone_step(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = "将混合物反应2 h，反应后冷却并过滤，最后干燥。"
    block = TextBlock(
        block_id="REACTION_AFTER", text=text, line_start=1, line_end=1,
        heading_path=["RDX", "制备方法"], heading_title="制备方法", role="process",
    )
    anchor = TextSubjectAnchor(
        "REACTION_AFTER", "RDX", "单质炸药", "heading_anchor", 0.9, "confirmed"
    )
    records, _, _ = extract_process_step_facts([block], {"REACTION_AFTER": anchor})
    assert "反应" in [item.step_action for item in records]
    assert [item.step_action for item in records].count("反应") == 1
    assert any(item.step_action == "冷却" for item in records)
    assert all(item.step_label.strip() not in {"反应后", "反应结束后"} for item in records)


def test_body_only_registry_mention_cannot_anchor_process_chain(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = "先加入苯胺，再加热反应，最后过滤得到目标产品。"
    block = TextBlock(
        block_id="REAGENT_ONLY", text=text, line_start=1, line_end=1,
        heading_path=["促进剂", "制备方法"], heading_title="制备方法", role="process",
    )
    anchor = TextSubjectAnchor(
        "REAGENT_ONLY", "苯胺", "材料", "unique_confirmed_registry_mention", 0.88, "confirmed"
    )
    records, audits, blocks = extract_process_step_facts([block], {"REAGENT_ONLY": anchor})
    assert records == []
    assert audits == []
    assert blocks == set()


def test_process_final_product_rejects_generated_metric_predicates(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SUBJECT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="METRIC_NOT_PRODUCT",
        text="反应过程中生成速度取决于混合时间，而生成速度取决于反应时间。",
        line_start=1, line_end=1,
        heading_path=["反应结晶"], heading_title="反应结晶", role="process",
    )
    anchors, _, audits = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].status == "unresolved"
    assert anchors[0].subject == ""
    assert audits[0].final_status == "unresolved"


def test_process_local_device_entity_beats_topic_inheritance(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SUBJECT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="LOCAL_DEVICE",
        text="LZ-4加强帽长，必须先把加强帽中的装药过程分离出来，先装好，再和装了底部药的管壳结合。",
        line_start=1, line_end=1,
        heading_path=["炮弹雷管", "雷管装配", "生产工艺流程"],
        heading_title="生产工艺流程", role="process",
    )
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "LZ-4加强帽"
    assert anchors[0].source == "explicit_process_local_entity_span"
    assert anchors[0].subject_type == "火工品器件"


def test_polymer_noun_does_not_hide_later_dissolution_action(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="POLYMER_NOUN",
        text="先将CL-20和含能聚合物凝胶溶解，再通过冷冻干燥法除去溶剂。",
        line_start=1, line_end=1,
        heading_path=["GAP包覆CL-20", "制备方法"], heading_title="制备方法", role="process",
    )
    anchor = TextSubjectAnchor(
        "POLYMER_NOUN", "GAP包覆CL-20", "配方/材料体系", "heading_anchor", 0.9, "confirmed"
    )
    records, _, _ = extract_process_step_facts([block], {"POLYMER_NOUN": anchor})
    assert [item.step_action for item in records] == ["溶解", "除去"]


def test_standalone_de_obtains_last_explicit_process_product(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SUBJECT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="STANDALONE_DE",
        text="用己烷提取得毛油，随后脱胶处理，得大豆磷脂。",
        line_start=1, line_end=1,
        heading_path=["工艺助剂", "制备方法"], heading_title="制备方法", role="process",
    )
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "大豆磷脂"
    assert anchors[0].source == "explicit_process_final_product_span"


def test_colored_final_product_is_normalized_to_material_name(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_SUBJECT_LLM_ENABLED", raising=False)
    block = TextBlock(
        block_id="COLORED_FINAL",
        text="钼粉反应生成过氧化钼酸溶胶，干燥，得淡蓝色三氧化钼。",
        line_start=1, line_end=1,
        heading_path=["抑烟剂", "制备方法"], heading_title="制备方法", role="process",
    )
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "三氧化钼"


def test_material_preprocessing_operations_are_individual_steps(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = "将大豆原料精选、脱皮、粗粉碎、预热、压扁，再用己烷提取得毛油。"
    block = TextBlock(
        block_id="PREPROCESS", text=text, line_start=1, line_end=1,
        heading_path=["大豆磷脂", "制备方法"], heading_title="制备方法", role="process",
    )
    anchor = TextSubjectAnchor(
        "PREPROCESS", "大豆磷脂", "材料", "explicit_process_final_product_span", 0.9, "confirmed"
    )
    records, _, _ = extract_process_step_facts([block], {"PREPROCESS": anchor})
    actions = [item.step_action for item in records]
    assert actions == ["精选", "脱皮", "粉碎", "加热", "压扁", "提取"]
    assert all(item.step_label in text for item in records)


def test_explicit_compound_operations_split_without_rewriting(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = "先冷藏12 h后过滤，再自然冷却洗涤，最后干燥。"
    block = TextBlock(
        block_id="COMPOUND_OPS", text=text, line_start=1, line_end=1,
        heading_path=["蛋卵磷脂", "制备方法"], heading_title="制备方法", role="process",
    )
    anchor = TextSubjectAnchor(
        "COMPOUND_OPS", "蛋卵磷脂", "材料", "heading_anchor", 0.9, "confirmed"
    )
    records, _, _ = extract_process_step_facts([block], {"COMPOUND_OPS": anchor})
    assert [item.step_action for item in records] == ["冷藏", "过滤", "冷却", "洗涤", "干燥"]
    assert all(item.step_label in text for item in records)


def test_process_prefix_conditions_are_kept_with_the_action_span(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PROCESS_LLM_ENABLED", raising=False)
    text = "步骤1：以三乙胺为催化剂，以二氯甲烷为溶剂，由甲基氮丙啶与三氯氧磷进行缩合反应，得到MAPO。"
    block = TextBlock(
        block_id="PREFIX_COND", text=text, line_start=1, line_end=1,
        heading_path=["MAPO", "制备方法"], heading_title="制备方法", role="process",
    )
    anchor = TextSubjectAnchor(
        "PREFIX_COND", "MAPO", "材料", "heading_anchor", 0.9, "confirmed"
    )
    records, _, _ = extract_process_step_facts([block], {"PREFIX_COND": anchor})
    assert len(records) == 1
    assert records[0].step_action == "缩合"
    assert "以三乙胺为催化剂" in records[0].step_label
    assert "催化剂=以三乙胺为催化剂" in records[0].step_condition_text
    assert "溶剂=以二氯甲烷为溶剂" in records[0].step_condition_text
