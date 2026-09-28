from __future__ import annotations

from book_engine.core.schemas import TableBlock
from book_engine.routing.heading_subject_resolver import classify_heading_subject
from book_engine.tables.context_metadata_extractor import extract_context_metadata


def test_bare_synthesis_topic_extracts_entity_span():
    result = classify_heading_subject("1）PECH的合成")
    assert result.kind == "entity_with_topic"
    assert result.entity == "PECH"


def test_bare_preparation_topic_is_generic_not_material_specific():
    result = classify_heading_subject("3.2 GAP的制备")
    assert result.kind == "entity_with_topic"
    assert result.entity == "GAP"


def test_complex_process_topic_ending_in_polymerization_stays_topic_only():
    result = classify_heading_subject("环张力、取代基和溶剂效应对环醚聚合")
    assert result.kind == "topic_only"
    assert not result.entity


def test_single_heading_beats_local_reaction_caption_and_keeps_legacy_source():
    block = TableBlock(
        table_id="T2",
        source_type="html",
        raw_text="<table></table>",
        line_start=1,
        line_end=1,
        heading="1）PECH的合成",
        preceding_text="表3-10反应条件对ECH聚合的影响",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "PECH"
    assert metadata.subject_source == "heading"


def test_real_hierarchical_path_keeps_path_source_label():
    block = TableBlock(
        table_id="T3",
        source_type="html",
        raw_text="<table></table>",
        line_start=1,
        line_end=1,
        heading="力学性能",
        heading_path=["第3章 HTPB推进剂", "3.2 力学性能"],
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "HTPB推进剂"
    assert metadata.subject_source.startswith("heading_path:")
