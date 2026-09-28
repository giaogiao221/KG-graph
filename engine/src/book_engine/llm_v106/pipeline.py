from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .client import QwenClientError, QwenOpenAIClient
from .english_aliases import extract_explicit_abbreviation_pairs
from .language_compat import detect_language, is_english_like
from .markdown_units import SourceUnit, decode_hash_u_name, parse_markdown_units
from .process_flow import ProcessSpan, detect_process_spans
from .process_graph import compile_process_graphs, validate_compiled_process_rows, validate_process_response
from .prompts import (
    candidate_review_payload,
    candidate_review_system,
    direct_extraction_payload,
    direct_extraction_system,
    process_extraction_payload,
    process_extraction_system,
    process_repair_payload,
)
from .schema59 import (
    PropertyOntologyIndex,
    apply_llm_fact,
    deduplicate,
    read_tsv,
    validate_rows,
    write_tsv,
)
from book_engine.ontology.supplementary_constraints import SupplementaryConstraints, write_audits


@dataclass(frozen=True)
class BookInput:
    title: str
    source_path: Path
    v105_dir: Path
    document_id: str


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    book_title: str
    heading_path: str
    source_locator: str
    evidence: str
    language: str = "zh"
    candidates: tuple[dict[str, Any], ...] = ()
    unit: SourceUnit | None = None
    process_span: ProcessSpan | None = None


def _json_dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def _append_jsonl(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _hash_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:24]}"


def _source_path_by_title(books_dir: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted(books_dir.glob("*.md")):
        result[decode_hash_u_name(path.stem)] = path
    return result


def discover_books(v105_root: Path, books_dir: Path, excluded_titles: set[str]) -> list[BookInput]:
    sources = _source_path_by_title(books_dir)
    books: list[BookInput] = []
    for report_path in sorted(v105_root.glob("*/book_engine_run_report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        schema = report.get("schema59_merged_export") or {}
        title = str(schema.get("book_title", "") or "").strip()
        if not title:
            title = decode_hash_u_name(report_path.parent.name)
        if title in excluded_titles:
            continue
        source = sources.get(title)
        if not source:
            input_raw = str(report.get("input", "") or "")
            input_name = Path(input_raw.replace("\\", "/")).name
            source = next((p for p in books_dir.glob("*.md") if decode_hash_u_name(p.name) == decode_hash_u_name(input_name)), None)
        if not source or not source.exists():
            raise FileNotFoundError(f"Source Markdown not found for book: {title}")
        document_id = str(schema.get("document_id", "") or "")
        if not document_id:
            document_id = "doc:" + hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
        books.append(BookInput(title, source, report_path.parent, document_id))
    if not books:
        raise RuntimeError(f"No v105 books found under {v105_root}")
    return books


def _infer_relation_type(row: Mapping[str, Any]) -> str:
    fact_type = str(row.get("事实类型", "") or "")
    edge = str(row.get("edge_verb", "") or "")
    text = fact_type + "|" + edge
    for token, label in (
        ("组成", "组成"), ("分类", "分类"), ("定义", "定义"), ("应用", "用途"), ("用于", "用途"),
        ("功能", "功能"), ("影响", "影响"), ("因果", "因果"), ("导致", "因果"), ("比较", "比较"),
        ("方法步骤", "步骤"), ("步骤", "步骤"), ("方法", "方法"), ("位置", "位置"), ("连接", "连接"),
        ("命名", "命名/别名"), ("又称", "命名/别名"),
    ):
        if token in text:
            return label
    return "属性"


def _candidate_from_row(
    row: Mapping[str, Any],
    origin: str,
    *,
    constraints: SupplementaryConstraints | None = None,
) -> dict[str, Any]:
    fallback_relation = _infer_relation_type(row)
    raw_relation = str(
        row.get("predicate_raw", "") or row.get("edge_verb", "") or row.get("attribute_name", "") or ""
    )
    relation_type, supplementary_audits = (
        constraints.normalize_relation_type(raw_relation, fallback=fallback_relation, stage="v106_candidate")
        if constraints is not None
        else (fallback_relation, [])
    )
    return {
        "candidate_id": str(row.get("fact_id", "") or _hash_id("candidate", json.dumps(row, ensure_ascii=False, sort_keys=True))),
        "subject": str(row.get("主体名称", "") or ""),
        "subject_type": str(row.get("主体类型", "") or ""),
        "property": str(row.get("attribute_name", "") or row.get("predicate_raw", "") or ""),
        "relation_type": relation_type,
        "value": str(row.get("尾实体/取值文本", "") or ""),
        "unit": str(row.get("normalized_unit", "") or row.get("单位", "") or ""),
        "condition": str(row.get("条件文本", "") or ""),
        "source_type": str(row.get("来源类型", "") or ""),
        "confidence": str(row.get("置信度", "") or ""),
        "origin": origin,
        "base_row": dict(row),
        "_supplementary_audits": supplementary_audits,
    }


def build_candidate_jobs(
    book: BookInput,
    batch_size: int,
    *,
    constraints: SupplementaryConstraints | None = None,
) -> tuple[list[Job], list[dict[str, str]], dict[str, int]]:
    guard = book.v105_dir / "step_graph_guard"
    generalized = read_tsv(guard / "graph_import_ready_generalized.tsv")
    candidates = read_tsv(guard / "graph_generalized_candidate_review_59.tsv")
    high_precision = read_tsv(guard / "graph_import_ready_high_precision.tsv")
    strict_table_ids = {
        str(row.get("fact_id", "") or "")
        for row in high_precision
        if str(row.get("来源类型", "") or "") == "table_conditional_record"
    }
    passthrough_tables = [
        dict(row) for row in high_precision
        if str(row.get("fact_id", "") or "") in strict_table_ids
    ]

    pool: dict[str, dict[str, Any]] = {}
    for row in generalized:
        fact_id = str(row.get("fact_id", "") or "")
        # Strict table rows are retained as the deterministic safety layer.
        if fact_id in strict_table_ids:
            continue
        pool[fact_id or _hash_id("candidate", json.dumps(row, ensure_ascii=False, sort_keys=True))] = _candidate_from_row(
            row, "v105_generalized", constraints=constraints
        )
    for row in candidates:
        fact_id = str(row.get("fact_id", "") or "")
        pool.setdefault(
            fact_id or _hash_id("candidate", json.dumps(row, ensure_ascii=False, sort_keys=True)),
            _candidate_from_row(row, "v105_candidate", constraints=constraints),
        )

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in pool.values():
        row = item["base_row"]
        evidence = str(row.get("证据文本", "") or "").strip()
        locator = str(row.get("来源定位", "") or "")
        heading = str(row.get("章节路径", "") or "")
        if not evidence:
            continue
        evidence_key = hashlib.sha1(evidence.encode("utf-8")).hexdigest()
        grouped[(evidence_key, heading, locator.split(";R", 1)[0])].append(item)

    jobs: list[Job] = []
    for (_, heading, locator_prefix), items in grouped.items():
        evidence = str(items[0]["base_row"].get("证据文本", "") or "")
        for start in range(0, len(items), max(1, batch_size)):
            chunk = items[start : start + max(1, batch_size)]
            job_id = _hash_id("review", book.title, heading, locator_prefix, str(start), evidence)
            jobs.append(
                Job(
                    job_id=job_id,
                    kind="candidate_review",
                    book_title=book.title,
                    heading_path=heading,
                    source_locator=locator_prefix,
                    evidence=evidence,
                    language=detect_language(evidence).language,
                    candidates=tuple(chunk),
                )
            )
    stats = {
        "v105_generalized_rows": len(generalized),
        "v105_candidate_rows": len(candidates),
        "strict_table_passthrough_rows": len(passthrough_tables),
        "candidate_rows_for_llm": len(pool),
        "candidate_review_jobs": len(jobs),
        "candidate_language_counts": {
            language: sum(1 for job in jobs if job.language == language)
            for language in ("zh", "en", "mixed")
        },
    }
    return jobs, passthrough_tables, stats


def build_direct_jobs(
    book: BookInput,
    *,
    max_window_chars: int,
    max_table_chars: int,
    max_text_windows: int,
    max_tables: int,
) -> tuple[list[Job], dict[str, int]]:
    windows, tables = parse_markdown_units(
        book.source_path,
        max_window_chars=max_window_chars,
        max_table_chars=max_table_chars,
    )
    if max_text_windows > 0:
        windows = windows[:max_text_windows]
    if max_tables > 0:
        tables = tables[:max_tables]
    jobs: list[Job] = []
    for unit in [*windows, *tables]:
        jobs.append(
            Job(
                job_id=_hash_id("direct", book.title, unit.unit_id),
                kind="direct_text" if unit.kind == "text_window" else "direct_table",
                book_title=book.title,
                heading_path=" > ".join(unit.heading_path),
                source_locator=f"L{unit.line_start}-L{unit.line_end}",
                evidence=unit.text,
                language=detect_language(unit.text).language,
                unit=unit,
            )
        )
    return jobs, {
        "text_windows": len(windows),
        "tables": len(tables),
        "direct_jobs": len(jobs),
        "direct_language_counts": {
            language: sum(1 for job in jobs if job.language == language)
            for language in ("zh", "en", "mixed")
        },
    }


def _table_provenance_catalog(book: BookInput, jobs: Sequence[Job]) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    for job in jobs:
        unit = job.unit
        if job.kind != "direct_table" or unit is None:
            continue
        record = tables.get(unit.table_id)
        if record is None:
            tables[unit.table_id] = {
                "table_id": unit.table_id,
                "table_title": unit.table_title,
                "table_title_source": unit.table_title_source,
                "table_context": unit.table_context,
                "caption_line_start": unit.caption_line_start,
                "caption_line_end": unit.caption_line_end,
                "line_start": unit.line_start,
                "line_end": unit.line_end,
            }
        else:
            record["line_start"] = min(int(record["line_start"]), unit.line_start)
            record["line_end"] = max(int(record["line_end"]), unit.line_end)
    return {
        "book_title": book.title,
        "tables": [tables[key] for key in sorted(tables)],
    }


def _aggregate_usage(total: dict[str, int], usage: Mapping[str, Any]) -> None:
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for target, keys in aliases.items():
        for key in keys:
            try:
                value = int(usage.get(key, 0) or 0)
            except Exception:
                value = 0
            if value:
                total[target] = total.get(target, 0) + value
                break



def build_process_jobs(
    book: BookInput,
    *,
    max_process_chars: int,
    max_process_spans: int,
) -> tuple[list[Job], dict[str, int]]:
    spans = detect_process_spans(
        book.source_path,
        max_span_chars=max_process_chars,
        max_spans=max_process_spans,
        include_tables=True,
    )
    jobs: list[Job] = []
    for span in spans:
        jobs.append(
            Job(
                job_id=_hash_id("process", book.title, span.span_id),
                kind=span.kind,
                book_title=book.title,
                heading_path=" > ".join(span.heading_path),
                source_locator=f"L{span.line_start}-L{span.line_end}",
                evidence=span.text,
                language=span.language,
                process_span=span,
            )
        )
    return jobs, {
        "process_text_spans": sum(1 for span in spans if span.kind == "process_text"),
        "process_table_spans": sum(1 for span in spans if span.kind == "process_table"),
        "process_jobs": len(jobs),
        "process_language_counts": {
            language: sum(1 for span in spans if span.language == language)
            for language in ("zh", "en", "mixed")
        },
    }


def build_explicit_abbreviation_rows(
    book: BookInput,
    direct_jobs: Sequence[Job],
    ontology: PropertyOntologyIndex,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    detected = 0
    rejected = 0
    for job in direct_jobs:
        if not is_english_like(job.language):
            continue
        for pair in extract_explicit_abbreviation_pairs(job.evidence):
            detected += 1
            row, reasons = apply_llm_fact(
                fact={
                    "subject": pair.full_name,
                    "subject_type": "其他明确实体",
                    "property": "别名",
                    "relation_type": "命名/别名",
                    "value": pair.abbreviation,
                    "unit": "",
                    "condition": "",
                    "polarity": "肯定",
                    "subject_span": pair.full_name_span,
                    "value_span": pair.abbreviation_span,
                    "confidence": 0.99,
                },
                ontology=ontology,
                evidence=job.evidence,
                book_title=book.title,
                document_id=book.document_id,
                heading_path=job.heading_path,
                source_locator=job.source_locator,
                source_type="llm_table_direct" if job.kind == "direct_table" else "llm_text_window_direct",
                table_id=job.unit.table_id if job.kind == "direct_table" and job.unit else "",
                table_title=job.unit.table_title if job.kind == "direct_table" and job.unit else "",
                extraction_source="deterministic_english_abbreviation_v106e",
                language=job.language,
            )
            if row is None or reasons:
                rejected += 1
                continue
            rows.append(row)
    rows = deduplicate(rows)
    return rows, {
        "explicit_abbreviation_pairs_detected": detected,
        "explicit_abbreviation_rows": len(rows),
        "explicit_abbreviation_pairs_rejected": rejected,
    }


def process_book(
    book: BookInput,
    *,
    output_root: Path,
    ontology: PropertyOntologyIndex,
    client: QwenOpenAIClient,
    candidate_batch_size: int,
    max_window_chars: int,
    max_table_chars: int,
    max_text_windows: int,
    max_tables: int,
    final_threshold: float,
    direct_max_facts_text: int,
    direct_max_facts_table: int,
    max_process_chars: int,
    max_process_spans: int,
    process_threshold: float,
    process_max_steps: int,
    process_max_processes_per_span: int,
    plan_only: bool,
) -> dict[str, Any]:
    started = time.time()
    out = output_root / book.title
    guard = out / "step_graph_guard"
    audit_dir = out / "step_llm_v106"
    guard.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    supplementary_constraints = SupplementaryConstraints.from_default_config()
    supplementary_audits: list[dict[str, Any]] = []

    candidate_jobs, passthrough_rows, candidate_stats = build_candidate_jobs(
        book, candidate_batch_size, constraints=supplementary_constraints
    )
    for candidate_job in candidate_jobs:
        for candidate in candidate_job.candidates:
            supplementary_audits.extend(candidate.get("_supplementary_audits", []))
    direct_jobs, direct_stats = build_direct_jobs(
        book,
        max_window_chars=max_window_chars,
        max_table_chars=max_table_chars,
        max_text_windows=max_text_windows,
        max_tables=max_tables,
    )
    _json_dump(out / "table_provenance_catalog.json", _table_provenance_catalog(book, direct_jobs))
    process_jobs, process_stats = build_process_jobs(
        book,
        max_process_chars=max_process_chars,
        max_process_spans=max_process_spans,
    )
    abbreviation_rows, abbreviation_stats = build_explicit_abbreviation_rows(book, direct_jobs, ontology)
    jobs = candidate_jobs + direct_jobs + process_jobs
    plan = {
        "book_title": book.title,
        "source_path": str(book.source_path),
        "v105_dir": str(book.v105_dir),
        "output_dir": str(out),
        **candidate_stats,
        **direct_stats,
        **process_stats,
        **abbreviation_stats,
        "total_llm_jobs": len(jobs),
    }
    _json_dump(audit_dir / "v106_book_plan.json", plan)
    process_plan_path = audit_dir / "process_span_plan.tsv"
    with process_plan_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["span_id", "kind", "language", "heading_path", "source_locator", "expected_labels", "process_type_hint", "evidence_preview"]
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for process_job in process_jobs:
            span = process_job.process_span
            if span is None:
                continue
            writer.writerow(
                {
                    "span_id": span.span_id,
                    "kind": span.kind,
                    "language": span.language,
                    "heading_path": process_job.heading_path,
                    "source_locator": process_job.source_locator,
                    "expected_labels": ",".join(str(item) for item in span.expected_labels),
                    "process_type_hint": span.process_type_hint,
                    "evidence_preview": re.sub(r"\s+", " ", span.text)[:500],
                }
            )
    if plan_only:
        return {**plan, "ok": True, "status": "planned", "elapsed_seconds": round(time.time() - started, 3)}

    final_rows: list[dict[str, Any]] = [*passthrough_rows, *abbreviation_rows]
    review_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    process_rows: list[dict[str, Any]] = []
    process_graphs: list[dict[str, Any]] = []
    usage_total: dict[str, int] = {}
    request_count = 0
    cached_count = 0
    error_count = 0
    accepted_from_review = 0
    accepted_direct = 0
    accepted_process_steps = 0
    accepted_process_graphs = 0
    process_repair_requests = 0
    process_no_flow_spans = 0
    process_review_spans = 0
    generic_step_deferred = 0

    decisions_log = audit_dir / "llm_decisions.jsonl"
    errors_log = audit_dir / "llm_errors.jsonl"
    process_graph_log = audit_dir / "process_flow_graphs.jsonl"
    process_review_log = audit_dir / "process_flow_review.jsonl"
    for path in (decisions_log, errors_log, process_graph_log, process_review_log):
        if path.exists():
            path.unlink()

    def register_response(response: Any) -> None:
        nonlocal request_count, cached_count
        request_count += 1
        if response.cached:
            cached_count += 1
        _aggregate_usage(usage_total, response.usage)

    for index, job in enumerate(jobs, start=1):
        print(f"[{book.title}] job {index}/{len(jobs)} {job.kind} {job.language} {job.source_locator}", flush=True)
        try:
            if job.kind == "candidate_review":
                payload = candidate_review_payload(
                    book_title=book.title,
                    heading_path=job.heading_path,
                    source_locator=job.source_locator,
                    evidence=job.evidence,
                    candidates=job.candidates,
                    property_names=ontology.preferred_names,
                    language=job.language,
                )
                response = client.chat_json(
                    system=candidate_review_system(job.language),
                    user=payload,
                    max_tokens=10000,
                    request_tag=job.job_id,
                )
                register_response(response)
                decisions = response.data.get("decisions", [])
                if not isinstance(decisions, list):
                    raise ValueError("candidate response decisions is not a list")
                by_id = {str(item.get("candidate_id", "") or ""): item for item in decisions if isinstance(item, Mapping)}
                for candidate in job.candidates:
                    cid = str(candidate["candidate_id"])
                    decision = by_id.get(cid)
                    base_row = candidate["base_row"]
                    if not decision:
                        review_rows.append(dict(base_row))
                        _append_jsonl(decisions_log, {"job_id": job.job_id, "candidate_id": cid, "status": "missing_decision"})
                        continue
                    action = str(decision.get("action", "reject") or "reject").lower()
                    log_entry = {
                        "job_id": job.job_id,
                        "candidate_id": cid,
                        "kind": job.kind,
                        "language": job.language,
                        "action": action,
                        "decision": decision,
                        "request_id": response.request_id,
                        "cached": response.cached,
                    }
                    if action == "reject":
                        rejected_rows.append(dict(base_row))
                        _append_jsonl(decisions_log, log_entry)
                        continue
                    # Operation/process steps must be produced only by the complete
                    # process-flow branch. Cached v106 generic responses are kept for
                    # all other facts, but their fragmented step records are suppressed.
                    if str(decision.get("relation_type", "") or "").strip() == "步骤":
                        review_rows.append(dict(base_row))
                        generic_step_deferred += 1
                        log_entry["status"] = "deferred_to_process_flow_specialist"
                        _append_jsonl(decisions_log, log_entry)
                        continue
                    row, reasons = apply_llm_fact(
                        fact=decision,
                        ontology=ontology,
                        evidence=job.evidence,
                        book_title=book.title,
                        document_id=book.document_id,
                        heading_path=job.heading_path,
                        source_locator=str(base_row.get("来源定位", "") or job.source_locator),
                        source_type=str(base_row.get("来源类型", "") or "llm_candidate_review"),
                        base_row=base_row,
                        extraction_source="qwen3_max_candidate_corrected_v106",
                        language=job.language,
                    )
                    try:
                        confidence = float(decision.get("confidence", 0.0) or 0.0)
                    except Exception:
                        confidence = 0.0
                    log_entry["validation_reasons"] = reasons
                    if row is not None and not reasons and confidence >= final_threshold:
                        final_rows.append(row)
                        accepted_from_review += 1
                        log_entry["status"] = "released"
                    else:
                        review_rows.append(dict(base_row) if row is None else row)
                        log_entry["status"] = "manual_review"
                    _append_jsonl(decisions_log, log_entry)

            elif job.kind in {"direct_text", "direct_table"}:
                max_facts = direct_max_facts_table if job.kind == "direct_table" else direct_max_facts_text
                payload = direct_extraction_payload(
                    unit_kind="table" if job.kind == "direct_table" else "text_window",
                    unit_id=job.unit.unit_id if job.unit else job.job_id,
                    book_title=book.title,
                    heading_path=job.heading_path,
                    source_locator=job.source_locator,
                    evidence=job.evidence,
                    property_names=ontology.preferred_names,
                    max_facts=max_facts,
                    table_id=job.unit.table_id if job.kind == "direct_table" and job.unit else "",
                    table_title=job.unit.table_title if job.kind == "direct_table" and job.unit else "",
                    language=job.language,
                )
                response = client.chat_json(
                    system=direct_extraction_system(job.language),
                    user=payload,
                    max_tokens=12000 if job.kind == "direct_table" else 9000,
                    request_tag=job.job_id,
                )
                register_response(response)
                facts = response.data.get("facts", [])
                if not isinstance(facts, list):
                    raise ValueError("direct response facts is not a list")
                for fact_index, fact in enumerate(facts[:max_facts], start=1):
                    if not isinstance(fact, Mapping):
                        continue
                    if str(fact.get("relation_type", "") or "").strip() == "步骤":
                        generic_step_deferred += 1
                        _append_jsonl(
                            decisions_log,
                            {
                                "job_id": job.job_id,
                                "fact_index": fact_index,
                                "kind": job.kind,
                                "fact": fact,
                                "status": "deferred_to_process_flow_specialist",
                                "request_id": response.request_id,
                                "cached": response.cached,
                            },
                        )
                        continue
                    row, reasons = apply_llm_fact(
                        fact=fact,
                        ontology=ontology,
                        evidence=job.evidence,
                        book_title=book.title,
                        document_id=book.document_id,
                        heading_path=job.heading_path,
                        source_locator=job.source_locator,
                        source_type="llm_table_direct" if job.kind == "direct_table" else "llm_text_window_direct",
                        table_id=job.unit.table_id if job.kind == "direct_table" and job.unit else "",
                        table_title=job.unit.table_title if job.kind == "direct_table" and job.unit else "",
                        extraction_source="qwen3_max_table_direct_v106" if job.kind == "direct_table" else "qwen3_max_text_direct_v106",
                        language=job.language,
                    )
                    try:
                        confidence = float(fact.get("confidence", 0.0) or 0.0)
                    except Exception:
                        confidence = 0.0
                    entry = {
                        "job_id": job.job_id,
                        "fact_index": fact_index,
                        "kind": job.kind,
                        "language": job.language,
                        "fact": fact,
                        "validation_reasons": reasons,
                        "request_id": response.request_id,
                        "cached": response.cached,
                    }
                    if row is not None and not reasons and confidence >= final_threshold:
                        final_rows.append(row)
                        accepted_direct += 1
                        entry["status"] = "released"
                    elif row is not None:
                        review_rows.append(row)
                        entry["status"] = "manual_review"
                    else:
                        entry["status"] = "invalid_rejected"
                    _append_jsonl(decisions_log, entry)

            else:  # process_text | process_table
                span = job.process_span
                if span is None:
                    raise ValueError("process job missing process_span")
                payload = process_extraction_payload(
                    span_id=span.span_id,
                    unit_kind="table" if span.kind == "process_table" else "text_span",
                    book_title=book.title,
                    heading_path=job.heading_path,
                    source_locator=job.source_locator,
                    evidence=job.evidence,
                    expected_labels=span.expected_labels,
                    process_type_hint=span.process_type_hint,
                    max_processes=process_max_processes_per_span,
                    max_steps=process_max_steps,
                    language=job.language,
                )
                response = client.chat_json(
                    system=process_extraction_system(job.language),
                    user=payload,
                    max_tokens=14000,
                    request_tag=job.job_id,
                )
                register_response(response)
                validation = validate_process_response(
                    response.data,
                    span=span,
                    confidence_threshold=process_threshold,
                    max_steps=process_max_steps,
                )
                active_response = response
                active_validation = validation
                if validation.repair_recommended:
                    repair_payload = process_repair_payload(
                        original_payload=payload,
                        original_response=response.data,
                        validation_errors=validation.errors,
                    )
                    repaired = client.chat_json(
                        system=process_extraction_system(job.language),
                        user=repair_payload,
                        max_tokens=16000,
                        request_tag=job.job_id + ":repair",
                    )
                    register_response(repaired)
                    process_repair_requests += 1
                    active_response = repaired
                    active_validation = validate_process_response(
                        repaired.data,
                        span=span,
                        confidence_threshold=process_threshold,
                        max_steps=process_max_steps,
                    )

                if active_validation.errors:
                    process_review_spans += 1
                    _append_jsonl(
                        process_review_log,
                        {
                            "job_id": job.job_id,
                            "span_id": span.span_id,
                            "kind": span.kind,
                            "language": span.language,
                            "source_locator": job.source_locator,
                            "heading_path": job.heading_path,
                            "expected_labels": list(span.expected_labels),
                            "validation_errors": list(active_validation.errors),
                            "model_response": active_response.data,
                            "evidence": span.text,
                        },
                    )
                    _append_jsonl(
                        decisions_log,
                        {
                            "job_id": job.job_id,
                            "kind": job.kind,
                            "status": "process_manual_review",
                            "validation_errors": list(active_validation.errors),
                            "request_id": active_response.request_id,
                            "cached": active_response.cached,
                        },
                    )
                    continue

                if not active_validation.valid_processes:
                    process_no_flow_spans += 1
                    _append_jsonl(
                        decisions_log,
                        {
                            "job_id": job.job_id,
                            "kind": job.kind,
                            "status": "not_an_executable_process",
                            "request_id": active_response.request_id,
                            "cached": active_response.cached,
                        },
                    )
                    continue

                compiled_rows, compiled_graphs = compile_process_graphs(
                    active_validation.valid_processes,
                    span=span,
                    ontology=ontology,
                    document_id=book.document_id,
                    heading_path=job.heading_path,
                    source_locator=job.source_locator,
                )
                compiled_validation = validate_compiled_process_rows(compiled_rows)
                if not compiled_validation.get("ok"):
                    process_review_spans += 1
                    _append_jsonl(
                        process_review_log,
                        {
                            "job_id": job.job_id,
                            "span_id": span.span_id,
                            "kind": span.kind,
                            "source_locator": job.source_locator,
                            "validation_errors": compiled_validation.get("errors", []),
                            "model_response": active_response.data,
                            "evidence": span.text,
                        },
                    )
                    continue
                process_rows.extend(compiled_rows)
                final_rows.extend(compiled_rows)
                process_graphs.extend(compiled_graphs)
                accepted_process_steps += len(compiled_rows)
                accepted_process_graphs += len(compiled_graphs)
                for graph in compiled_graphs:
                    _append_jsonl(process_graph_log, graph)
                _append_jsonl(
                    decisions_log,
                    {
                        "job_id": job.job_id,
                        "kind": job.kind,
                        "status": "process_released",
                        "process_count": len(compiled_graphs),
                        "step_count": len(compiled_rows),
                        "request_id": active_response.request_id,
                        "cached": active_response.cached,
                    },
                )

        except Exception as exc:  # noqa: BLE001
            error_count += 1
            if job.kind == "candidate_review":
                review_rows.extend(dict(item["base_row"]) for item in job.candidates)
            _append_jsonl(
                errors_log,
                {
                    "job_id": job.job_id,
                    "kind": job.kind,
                    "language": job.language,
                    "source_locator": job.source_locator,
                    "error": repr(exc),
                },
            )
            print(f"[{book.title}] WARNING job failed: {exc!r}", file=sys.stderr, flush=True)

    # Enforce a single writer for process fields: generic fragmented step facts
    # never reach the final graph after this specialization is installed.
    filtered_final: list[dict[str, Any]] = []
    for row in final_rows:
        is_step = str(row.get("事实类型", "") or "") == "方法步骤事实" or bool(str(row.get("step_action", "") or "").strip())
        specialized = str(row.get("抽取来源", "") or "") == "qwen3_max_process_flow_specialized_v106p"
        if is_step and not specialized:
            review_rows.append(dict(row))
            generic_step_deferred += 1
            continue
        prepared_row, row_audits = supplementary_constraints.prepare_row(row, stage="v106_final")
        supplementary_audits.extend(row_audits)
        subject_action, subject_audits = supplementary_constraints.subject_decision(
            str(prepared_row.get("主体名称", "") or ""), stage="v106_final"
        )
        supplementary_audits.extend(subject_audits)
        if subject_action == "manual_review":
            review_rows.append(prepared_row)
            continue
        if subject_action == "reject":
            rejected_rows.append(prepared_row)
            continue
        filtered_final.append(prepared_row)

    final_rows = deduplicate(filtered_final)
    review_rows = deduplicate(review_rows)
    rejected_rows = deduplicate(rejected_rows)
    process_rows = deduplicate(process_rows)
    validation = validate_rows(final_rows)
    process_validation = validate_compiled_process_rows(process_rows)

    write_tsv(guard / "graph_import_ready.tsv", final_rows)
    write_tsv(guard / "graph_import_ready_high_precision.tsv", passthrough_rows)
    write_tsv(guard / "graph_candidate_review_59.tsv", review_rows)
    write_tsv(guard / "graph_rejected_59.tsv", rejected_rows)
    write_tsv(guard / "graph_process_flow_ready.tsv", process_rows)
    supplementary_audit_path = audit_dir / "supplementary_constraints_audit.jsonl"
    supplementary_audit_path.write_text("", encoding="utf-8")
    write_audits(supplementary_audit_path, supplementary_audits)

    v105_guard = book.v105_dir / "step_graph_guard"
    for source_name, target_name in (
        ("graph_import_ready_generalized.tsv", "graph_generalized_raw_candidates.tsv"),
        ("graph_import_ready_high_precision.tsv", "graph_v105_high_precision_baseline.tsv"),
        ("graph_generalized_candidate_review_59.tsv", "graph_v105_candidate_baseline.tsv"),
    ):
        source = v105_guard / source_name
        if source.exists():
            shutil.copy2(source, guard / target_name)

    report = {
        **plan,
        "ok": bool(validation.get("ok")) and bool(process_validation.get("ok")) and error_count == 0,
        "status": "complete" if error_count == 0 else "complete_with_retryable_errors",
        "final_rows": len(final_rows),
        "strict_table_passthrough_rows": len(passthrough_rows),
        "explicit_abbreviation_rows": len(abbreviation_rows),
        "accepted_candidate_review_rows": accepted_from_review,
        "accepted_direct_rows": accepted_direct,
        "accepted_process_graphs": accepted_process_graphs,
        "accepted_process_step_rows": accepted_process_steps,
        "process_repair_requests": process_repair_requests,
        "process_no_flow_spans": process_no_flow_spans,
        "process_manual_review_spans": process_review_spans,
        "generic_step_rows_deferred": generic_step_deferred,
        "manual_review_rows": len(review_rows),
        "rejected_rows": len(rejected_rows),
        "supplementary_constraint_audits": len(supplementary_audits),
        "llm_requests": request_count,
        "llm_cached_requests": cached_count,
        "llm_error_jobs": error_count,
        "usage": usage_total,
        "schema59_validation": validation,
        "process_graph_validation": process_validation,
        "elapsed_seconds": round(time.time() - started, 3),
        "outputs": {
            "final": str(guard / "graph_import_ready.tsv"),
            "process_steps": str(guard / "graph_process_flow_ready.tsv"),
            "process_graphs": str(process_graph_log),
            "process_review": str(process_review_log),
            "manual_review": str(guard / "graph_candidate_review_59.tsv"),
            "rejected": str(guard / "graph_rejected_59.tsv"),
            "supplementary_constraint_audit": str(supplementary_audit_path),
            "raw_v105_generalized": str(guard / "graph_generalized_raw_candidates.tsv"),
        },
    }
    _json_dump(out / "v106_qwen3_max_book_report.json", report)
    _json_dump(guard / "schema59_validation_report.json", validation)
    _json_dump(guard / "process_graph_validation_report.json", process_validation)
    return report


def run(args: argparse.Namespace) -> int:
    excluded = {item.strip() for item in args.exclude_title if item.strip()}
    books = discover_books(args.v105_root, args.books_dir, excluded)
    if args.max_books > 0:
        books = books[: args.max_books]
    ontology = PropertyOntologyIndex.from_tsv(args.property_ontology)
    output_root = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    client = QwenOpenAIClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        cache_dir=args.cache_dir,
        timeout=args.timeout,
        max_retries=args.max_retries,
        min_interval_seconds=args.min_interval_seconds,
    )
    if not args.plan_only:
        client.assert_configured()

    reports: list[dict[str, Any]] = []
    for book in books:
        report = process_book(
            book,
            output_root=output_root,
            ontology=ontology,
            client=client,
            candidate_batch_size=args.candidate_batch_size,
            max_window_chars=args.max_window_chars,
            max_table_chars=args.max_table_chars,
            max_text_windows=args.max_text_windows_per_book,
            max_tables=args.max_tables_per_book,
            final_threshold=args.final_threshold,
            direct_max_facts_text=args.direct_max_facts_text,
            direct_max_facts_table=args.direct_max_facts_table,
            max_process_chars=args.max_process_chars,
            max_process_spans=args.max_process_spans_per_book,
            process_threshold=args.process_threshold,
            process_max_steps=args.process_max_steps,
            process_max_processes_per_span=args.process_max_processes_per_span,
            plan_only=args.plan_only,
        )
        reports.append(report)

    total_usage: dict[str, int] = {}
    for report in reports:
        for key, value in (report.get("usage") or {}).items():
            try:
                total_usage[key] = total_usage.get(key, 0) + int(value or 0)
            except Exception:
                pass
    batch = {
        "ok": all(bool(r.get("ok")) for r in reports),
        "status": "planned" if args.plan_only else "complete",
        "revision": "v106_english_compat",
        "model": args.model,
        "base_url": args.base_url or os.getenv("OPENAI_BASE_URL", ""),
        "english_property_alias_validation": ontology.validation_report(),
        "book_count": len(reports),
        "excluded_titles": sorted(excluded),
        "final_rows": sum(int(r.get("final_rows", 0) or 0) for r in reports),
        "process_graphs": sum(int(r.get("accepted_process_graphs", 0) or 0) for r in reports),
        "process_step_rows": sum(int(r.get("accepted_process_step_rows", 0) or 0) for r in reports),
        "process_review_spans": sum(int(r.get("process_manual_review_spans", 0) or 0) for r in reports),
        "manual_review_rows": sum(int(r.get("manual_review_rows", 0) or 0) for r in reports),
        "llm_jobs": sum(int(r.get("total_llm_jobs", 0) or 0) for r in reports),
        "llm_error_jobs": sum(int(r.get("llm_error_jobs", 0) or 0) for r in reports),
        "usage": total_usage,
        "books": reports,
    }
    _json_dump(output_root / "v106_qwen3_max_batch_report.json", batch)
    manifest_path = output_root / "batch_v106_manifest.tsv"
    fields = [
        "book_title", "status", "final_rows", "strict_table_passthrough_rows", "explicit_abbreviation_rows", "accepted_candidate_review_rows",
        "accepted_direct_rows", "accepted_process_graphs", "accepted_process_step_rows", "process_repair_requests",
        "process_manual_review_spans", "generic_step_rows_deferred", "manual_review_rows", "rejected_rows",
        "total_llm_jobs", "llm_error_jobs", "elapsed_seconds",
    ]
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(reports)
    print(json.dumps(batch, ensure_ascii=False, indent=2), flush=True)
    return 0 if batch["ok"] or args.plan_only else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v106 Qwen3-Max final extraction with complete process-flow specialization")
    parser.add_argument("--books-dir", type=Path, required=True)
    parser.add_argument("--v105-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--property-ontology", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "qwen3-max"))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    # The standalone package is for a generic book folder: process every
    # Markdown book by default.  Users may still exclude individual titles.
    parser.add_argument("--exclude-title", action="append", default=[])
    parser.add_argument("--candidate-batch-size", type=int, default=12)
    parser.add_argument("--max-window-chars", type=int, default=6500)
    parser.add_argument("--max-table-chars", type=int, default=30000)
    parser.add_argument("--max-text-windows-per-book", type=int, default=0)
    parser.add_argument("--max-tables-per-book", type=int, default=0)
    parser.add_argument("--max-books", type=int, default=0)
    parser.add_argument("--final-threshold", type=float, default=0.68)
    parser.add_argument("--direct-max-facts-text", type=int, default=24)
    parser.add_argument("--direct-max-facts-table", type=int, default=80)
    parser.add_argument("--max-process-chars", type=int, default=12000)
    parser.add_argument("--max-process-spans-per-book", type=int, default=0)
    parser.add_argument("--process-threshold", type=float, default=0.72)
    parser.add_argument("--process-max-steps", type=int, default=40)
    parser.add_argument("--process-max-processes-per-span", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--min-interval-seconds", type=float, default=0.25)
    parser.add_argument("--plan-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
