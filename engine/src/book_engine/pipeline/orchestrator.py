from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from book_engine.core.schemas import (
    AxisRoleDecision,
    CellRoleDecision,
    ConditionBinding,
    ConditionCandidate,
    ConditionalFactRecord,
    HeaderTree,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
    TopologyDecision,
)
from book_engine.document.markdown_loader import load_markdown
from book_engine.document.heading_rebuilder import rebuild_headings
from book_engine.document.block_segmenter import segment_text_blocks
from book_engine.routing.block_role_classifier import classify_blocks
from book_engine.routing.text_subject_anchor_resolver import resolve_text_subject_anchors_with_audit
from book_engine.document.semantic_block_splitter import split_multisubject_blocks
from book_engine.text.text_fact_extractor import extract_text_facts
from book_engine.text.deictic_subject_resolver import resolve_deictic_text_subjects
from book_engine.text.process_step_extractor import extract_process_step_facts
from book_engine.text.narrative_relation_compiler import compile_narrative_relations, write_narrative_relation_audit
from book_engine.text.generalized_relation_compiler import compile_generalized_relations, write_generalized_relation_audit
from book_engine.text.text_condition_extractor import attach_text_conditions
from book_engine.routing.subject_type_harmonizer import harmonize_subject_types
from book_engine.gates.text_fact_gate import gate_text_facts
from book_engine.export.text_audit_writer import write_text_outputs
from book_engine.export.semantic_enhancement_audit_writer import write_semantic_enhancement_outputs
from book_engine.export.table_evidence_writer import write_table_evidence_outputs
from book_engine.tables.audit_writer import write_table_structure_outputs
from book_engine.tables.condition_audit_writer import write_table_condition_outputs
from book_engine.tables.condition_extractor import extract_condition_candidates
from book_engine.tables.context_metadata_extractor import ContextMetadata
from book_engine.tables.grid_rebuilder import rebuild_grid
from book_engine.tables.header_tree_builder import build_header_tree
from book_engine.tables.record_compiler import compile_conditional_records
from book_engine.tables.semantic_audit_writer import write_table_semantic_outputs
from book_engine.tables.semantic_planner import build_semantic_plan
from book_engine.tables.table_detector import detect_tables
from book_engine.tables.record_refiner import refine_condition_results
from book_engine.tables.refinement_audit_writer import write_table_refinement_outputs
from book_engine.routing.subject_registry_builder import build_subject_registry
from book_engine.gates.table_record_gate import gate_table_records
from book_engine.export.subject_audit_writer import write_subject_and_gate_outputs
from book_engine.export.schema59_exporter import export_schema59
from book_engine.export.coverage_report_writer import write_book_coverage_report
from book_engine.handbook.profile_detector import detect_document_profile, write_document_profile_report
from book_engine.handbook.adapter import build_repeated_entry_text_layer
from book_engine.specification.adapter import build_specification_text_layer, build_specification_table_layer


@dataclass
class RunContext:
    input_path: Path
    output_dir: Path
    config_path: Path
    mode: str = "production"


def run_pipeline(ctx: RunContext) -> Dict[str, Any]:
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    document = load_markdown(ctx.input_path)
    profile_decision = detect_document_profile(document)
    profile_report = write_document_profile_report(ctx.output_dir, profile_decision)
    table_blocks = detect_tables(document)

    structure_results: List[Tuple[TableBlock, TableGrid, HeaderTree]] = []
    semantic_results: List[
        Tuple[
            TableBlock,
            TableGrid,
            TableSemanticPlan,
            List[AxisRoleDecision],
            List[CellRoleDecision],
            TopologyDecision,
        ]
    ] = []
    condition_results: List[
        Tuple[
            TableBlock,
            TableGrid,
            TableSemanticPlan,
            List[ConditionCandidate],
            List[ConditionalFactRecord],
            List[ConditionBinding],
            List[Dict[str, object]],
            ContextMetadata,
        ]
    ] = []
    structure_errors: List[Dict[str, object]] = []
    semantic_errors: List[Dict[str, object]] = []
    condition_errors: List[Dict[str, object]] = []

    for block in table_blocks:
        try:
            grid = rebuild_grid(block)
            header_tree = build_header_tree(grid)
            structure_results.append((block, grid, header_tree))
        except Exception as exc:
            structure_errors.append(
                {
                    "table_id": block.table_id,
                    "source_type": block.source_type,
                    "line_start": block.line_start,
                    "line_end": block.line_end,
                    "heading": block.heading,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        try:
            plan, axis_roles, cell_roles, topology = build_semantic_plan(block, grid, header_tree)
            semantic_results.append((block, grid, plan, axis_roles, cell_roles, topology))
        except Exception as exc:
            semantic_errors.append(
                {
                    "table_id": block.table_id,
                    "source_type": block.source_type,
                    "line_start": block.line_start,
                    "line_end": block.line_end,
                    "heading": block.heading,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        try:
            candidates = extract_condition_candidates(
                block, grid, header_tree, plan, axis_roles, cell_roles
            )
            records, bindings, unresolved, metadata = compile_conditional_records(
                block, grid, header_tree, plan, axis_roles, cell_roles, candidates
            )
            condition_results.append(
                (block, grid, plan, candidates, records, bindings, unresolved, metadata)
            )
        except Exception as exc:
            condition_errors.append(
                {
                    "table_id": block.table_id,
                    "source_type": block.source_type,
                    "line_start": block.line_start,
                    "line_end": block.line_end,
                    "heading": block.heading,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    table_report = write_table_structure_outputs(ctx.output_dir, structure_results, structure_errors)
    semantic_report = write_table_semantic_outputs(ctx.output_dir, semantic_results, semantic_errors)

    condition_results, refinement_summary = refine_condition_results(condition_results)
    refinement_report = write_table_refinement_outputs(ctx.output_dir, refinement_summary)
    condition_report = write_table_condition_outputs(ctx.output_dir, condition_results, condition_errors)
    table_evidence_report = write_table_evidence_outputs(ctx.output_dir, table_blocks, condition_results)

    registry_entries, subject_resolutions, registry_by_key = build_subject_registry(condition_results)
    accepted_table_records, table_gate_decisions = gate_table_records(condition_results, registry_by_key)
    subject_report = write_subject_and_gate_outputs(
        ctx.output_dir, registry_entries, subject_resolutions, table_gate_decisions
    )

    handbook_report: Dict[str, object] = {"ok": True, "stage": "not_applicable", "entries_detected": 0}
    specification_report: Dict[str, object] = {"ok": True, "stage": "not_applicable", "entries_detected": 0}
    narrative_relation_report: Dict[str, object] = {"ok": True, "stage": "not_applicable", "candidate_relations": 0}
    generalized_relation_report: Dict[str, object] = {"ok": True, "stage": "not_applicable", "candidate_relations": 0}
    if profile_decision.is_repeated_entry_handbook:
        text_blocks, text_anchors, text_anchor_by_block, text_records, handbook_report = build_repeated_entry_text_layer(
            document,
            output_dir=ctx.output_dir,
            book_id=f"doc:{ctx.input_path.stem}",
            book_title=ctx.input_path.stem,
        )
        split_audits = []
        anchor_audits = []
        process_audits = []
        process_blocks_with_steps = set()
        deictic_subject_audits = []
        text_condition_audits = []
        # Repeated-entry handbook fields are already bounded by entry scope.
        # Do not re-run monograph deictic/process/condition heuristics, which can
        # incorrectly borrow owners or values from adjacent entries.
    elif profile_decision.is_specification_handbook:
        (
            text_blocks,
            text_anchors,
            text_anchor_by_block,
            base_text_records,
            specification_text_report,
            specification_entries,
        ) = build_specification_text_layer(document, output_dir=ctx.output_dir)
        base_text_records, deictic_subject_audits = resolve_deictic_text_subjects(base_text_records, text_blocks)
        process_records, process_audits, process_blocks_with_steps = extract_process_step_facts(
            text_blocks, text_anchor_by_block
        )
        base_text_records = [
            record for record in base_text_records
            if not (record.block_id in process_blocks_with_steps and record.relation_kind == "process")
        ]
        text_records = list(base_text_records) + list(process_records)
        text_records, text_condition_audits = attach_text_conditions(text_records, text_blocks)
        split_audits = []
        anchor_audits = []

        # Generic table planning remains available as a candidate/audit source,
        # but final specification facts are rebuilt from project-indicator-method
        # tables under the enclosing material entry. This prevents values such as
        # “外观” from becoming table subjects.
        for decision in table_gate_decisions:
            if decision.accepted:
                decision.accepted = False
                decision.action = "hold"
                decision.final_status = "candidate"
                decision.reasons = sorted(set(decision.reasons + ["superseded_by_specification_projection"]))
        accepted_table_records = []
        spec_accepted, spec_decisions, specification_table_report = build_specification_table_layer(
            structure_results, specification_entries, output_dir=ctx.output_dir
        )
        accepted_table_records.extend(spec_accepted)
        table_gate_decisions.extend(spec_decisions)
        specification_report = {
            "ok": bool(specification_text_report.get("ok")) and bool(specification_table_report.get("ok")),
            "stage": "specification_handbook_v2_phase102",
            "text": specification_text_report,
            "tables": specification_table_report,
        }
    else:
        headings = rebuild_headings(document)
        text_blocks = segment_text_blocks(document, headings, table_blocks)
        text_blocks = classify_blocks(text_blocks)
        text_blocks, split_audits = split_multisubject_blocks(text_blocks, registry_entries)
        text_anchors, text_anchor_by_block, anchor_audits = resolve_text_subject_anchors_with_audit(
            text_blocks, registry_entries
        )
        base_text_records = extract_text_facts(text_blocks, text_anchor_by_block)
        narrative_records, narrative_relation_audits = compile_narrative_relations(text_blocks, text_anchor_by_block)
        narrative_relation_report = write_narrative_relation_audit(ctx.output_dir, narrative_relation_audits)
        generalized_records, generalized_relation_audits = compile_generalized_relations(text_blocks, text_anchor_by_block)
        generalized_relation_report = write_generalized_relation_audit(ctx.output_dir, generalized_relation_audits)
        base_text_records.extend(narrative_records)
        base_text_records.extend(generalized_records)
        base_text_records, deictic_subject_audits = resolve_deictic_text_subjects(base_text_records, text_blocks)
        process_records, process_audits, process_blocks_with_steps = extract_process_step_facts(
            text_blocks, text_anchor_by_block
        )
        # When a reliable ordered step chain exists, suppress the old whole-paragraph
        # process summary from the final candidate stream. The full paragraph remains
        # the evidence attached to each step and in the process audit sidecar.
        base_text_records = [
            record for record in base_text_records
            if not (record.block_id in process_blocks_with_steps and record.relation_kind == "process")
        ]
        text_records = list(base_text_records) + list(process_records)
        text_records, text_condition_audits = attach_text_conditions(text_records, text_blocks)

    deictic_dir = ctx.output_dir / "step_owner_value_binding"
    deictic_dir.mkdir(parents=True, exist_ok=True)
    import csv as _csv
    with (deictic_dir / "deictic_subject_resolution.tsv").open("w", encoding="utf-8", newline="") as _handle:
        _fields = ["record_id", "block_id", "original_subject", "resolved_subject", "decision", "reason"]
        _writer = _csv.DictWriter(_handle, fieldnames=_fields, delimiter="\t", extrasaction="ignore")
        _writer.writeheader()
        for _row in deictic_subject_audits:
            _writer.writerow(_row)
    accepted_text_records, text_gate_decisions = gate_text_facts(text_records, text_anchor_by_block)
    accepted_table_records, accepted_text_records, type_audits = harmonize_subject_types(
        accepted_table_records,
        accepted_text_records,
        registry_entries,
    )
    text_report = write_text_outputs(
        ctx.output_dir, text_blocks, text_anchors, text_records, text_gate_decisions
    )
    semantic_enhancement_report = write_semantic_enhancement_outputs(
        ctx.output_dir, split_audits, anchor_audits, process_audits
    )

    block_by_table = {block.table_id: block for block, *_ in condition_results}
    table_decision_by_id = {decision.record_id: decision for decision in table_gate_decisions}
    held_table_records = []
    for result in condition_results:
        plan = result[2]
        for record in result[4]:
            decision = table_decision_by_id.get(record.record_id)
            if decision is not None and not decision.accepted:
                held_table_records.append((record, decision, plan))

    text_decision_by_id = {decision.record_id: decision for decision in text_gate_decisions}
    held_text_records = []
    for record in text_records:
        decision = text_decision_by_id.get(record.record_id)
        if decision is not None and not decision.accepted:
            held_text_records.append((record, decision))

    schema59_report = export_schema59(
        ctx.output_dir,
        ctx.input_path,
        accepted_table_records,
        block_by_table,
        accepted_text=accepted_text_records,
        held_table=held_table_records,
        held_text=held_text_records,
        type_audits=type_audits,
        text_condition_audits=text_condition_audits,
    )
    coverage_report = write_book_coverage_report(
        ctx.output_dir,
        table_blocks=table_blocks,
        condition_results=condition_results,
        table_decisions=table_gate_decisions,
        text_blocks=text_blocks,
        text_records=text_records,
        text_decisions=text_gate_decisions,
        schema59_report=schema59_report,
    )

    report = {
        "ok": all(
            bool(item.get("ok"))
            for item in (
                table_report,
                semantic_report,
                refinement_report,
                condition_report,
                table_evidence_report,
                subject_report,
                text_report,
                semantic_enhancement_report,
                schema59_report,
                coverage_report,
                profile_report,
                handbook_report,
                specification_report,
                narrative_relation_report,
                generalized_relation_report,
            )
        ),
        "status": "generalized_high_recall_phase105_complete",
        "pipeline_revision": "phase105_generalized_high_recall_dual_release",
        "input": str(ctx.input_path),
        "output_dir": str(ctx.output_dir),
        "config": str(ctx.config_path),
        "mode": ctx.mode,
        "document_profile": profile_report,
        "handbook_entries": handbook_report,
        "specification_handbook": specification_report,
        "narrative_relations": narrative_relation_report,
        "generalized_relations": generalized_relation_report,
        "table_structure": table_report,
        "table_semantics": semantic_report,
        "table_refinement": refinement_report,
        "table_conditions": condition_report,
        "table_evidence": table_evidence_report,
        "subject_registry": subject_report,
        "text_semantics": text_report,
        "semantic_enhancements": semantic_enhancement_report,
        "schema59_merged_export": schema59_report,
        "coverage": coverage_report,
        "schema_contract": "captured_legacy_59_columns_exact",
        "next_stage": "phase105_full_corpus_sampling_then_one_global_calibration",
    }
    import json

    (ctx.output_dir / "book_engine_run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
