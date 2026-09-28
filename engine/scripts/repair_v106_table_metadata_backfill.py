from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from book_engine.llm_v106.client import QwenOpenAIClient
from book_engine.llm_v106.table_metadata_backfill import (
    TableCatalog,
    build_resolution_payload,
    discover_book_jobs,
    repair_book_directory,
    repair_rows,
    validate_resolution_response,
)


SCHEMA_MARKERS = {"所属表格ID", "所属表格标题", "来源类型"}


def _json_dump(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _load_json_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {str(key): str(value) for key, value in data.items() if key and value}


def _load_title_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, Mapping):
        raise ValueError("title-map must contain one JSON object")
    return {str(key): str(value) for key, value in data.items() if key and value}


def _schema_rows(path: Path) -> list[dict[str, str]] | None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not SCHEMA_MARKERS.issubset(reader.fieldnames or []):
            return None
        return list(reader)


def _write_unresolved(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    fields = [
        "source_file",
        "fact_id",
        "书名",
        "来源定位",
        "主体名称",
        "predicate_raw",
        "attribute_name",
        "尾实体/取值文本",
        "数值",
        "条件文本",
        "candidate_table_ids",
        "candidate_table_titles",
        "resolution_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _audit_jobs(
    input_root: Path,
    *,
    source_books_root: Path | None = None,
    results_root: Path | None = None,
    title_map: Mapping[str, str] | None = None,
) -> tuple[list[Any], dict[str, TableCatalog], Counter[str], list[dict[str, str]], list[dict[str, Any]]]:
    jobs = discover_book_jobs(
        input_root,
        source_books_root=source_books_root,
        results_root=results_root,
        title_map=title_map,
    )
    relative_root = results_root or input_root
    catalogs: dict[str, TableCatalog] = {}
    total: Counter[str] = Counter()
    unresolved: list[dict[str, str]] = []
    books: list[dict[str, Any]] = []
    for job in jobs:
        catalog = TableCatalog.from_markdown(job.source_markdown)
        catalogs[job.book_title] = catalog
        book_audit: Counter[str] = Counter()
        book_unresolved: list[dict[str, str]] = []
        for tsv in job.source_book_dir.rglob("*.tsv"):
            rows = _schema_rows(tsv)
            if rows is None:
                continue
            _, audit, pending = repair_rows(rows, catalog)
            book_audit["schema_tsv_files"] += 1
            book_audit.update(audit)
            for item in pending:
                book_unresolved.append({"source_file": str(tsv.relative_to(relative_root)), **item})
        missing = (
            book_audit["filled_single_candidate"]
            + book_audit["filled_content_match"]
            + book_audit["unresolved"]
        )
        book_audit["missing_table_metadata_rows"] = missing
        book_audit["deterministically_resolved_rows"] = (
            book_audit["filled_single_candidate"] + book_audit["filled_content_match"]
        )
        total.update(book_audit)
        unresolved.extend(book_unresolved)
        books.append(
            {
                "book_title": job.book_title,
                "source_markdown": str(job.source_markdown),
                "source_result_dir": str(job.source_book_dir),
                "table_count": len(catalog.tables),
                **dict(book_audit),
            }
        )
    return jobs, catalogs, total, unresolved, books


def _deduplicate_unresolved(rows: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        fact_id = str(row.get("fact_id", "") or "")
        if fact_id:
            output.setdefault(fact_id, dict(row))
    return list(output.values())


def _resolve_with_qwen(
    unresolved: Sequence[Mapping[str, str]],
    catalogs: Mapping[str, TableCatalog],
    resolutions: dict[str, str],
    *,
    client: QwenOpenAIClient,
    resolution_path: Path,
    batch_size: int,
) -> tuple[dict[str, str], list[dict[str, str]], Counter[str]]:
    pending = [row for row in _deduplicate_unresolved(unresolved) if row.get("fact_id") not in resolutions]
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in pending:
        groups[(row.get("书名", ""), row.get("来源定位", ""))].append(row)
    stats: Counter[str] = Counter()
    rejected_rows: list[dict[str, str]] = []

    for (book_title, locator), group in sorted(groups.items()):
        catalog = catalogs[book_title]
        for offset in range(0, len(group), batch_size):
            batch = group[offset : offset + batch_size]
            active = batch
            batch_resolutions: dict[str, str] = {}
            batch_rejected: dict[str, str] = {}
            for semantic_attempt in range(2):
                payload = build_resolution_payload(book_title, locator, active, catalog)
                if semantic_attempt:
                    payload["correction_attempt"] = semantic_attempt
                    payload["previous_validation_errors"] = batch_rejected
                response = client.chat_json(
                    system=(
                        "You are a strict provenance classifier. Assign each fact to exactly one supplied source table. "
                        "Return one JSON object only and never invent a table ID."
                    ),
                    user=payload,
                    max_tokens=min(8192, max(1024, len(active) * 120)),
                    temperature=0.0,
                    request_tag=f"table-metadata:{book_title}:{locator}:{offset}:{semantic_attempt}",
                )
                stats["qwen_requests"] += 1
                if response.cached:
                    stats["qwen_cached_requests"] += 1
                valid, rejected = validate_resolution_response(response.data, active)
                batch_resolutions.update(valid)
                batch_rejected = rejected
                if not rejected:
                    break
                active = [row for row in active if row.get("fact_id") in rejected]
            resolutions.update(batch_resolutions)
            _json_dump(resolution_path, resolutions)
            stats["qwen_resolved_facts"] += len(batch_resolutions)
            for row in active if batch_rejected else []:
                rejected_rows.append({**row, "resolution_reason": batch_rejected.get(row.get("fact_id", ""), "unresolved")})
            print(
                f"[QWEN] {book_title} {locator} batch={offset // batch_size + 1} "
                f"resolved={len(batch_resolutions)} rejected={len(batch_rejected)}",
                flush=True,
            )
    return resolutions, rejected_rows, stats


def _copy_batch_sidecars(output_root: Path, jobs: Sequence[Any]) -> None:
    batch_roots = {
        (job.source_book_dir.parent, job.relative_output_dir.parent)
        for job in jobs
    }
    for batch_root, relative_parent in batch_roots:
        target = output_root / relative_parent
        target.mkdir(parents=True, exist_ok=True)
        for source in batch_root.iterdir():
            if source.is_file() and source.suffix.lower() != ".zip":
                shutil.copy2(source, target / source.name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill v106 table IDs and titles from original Markdown books")
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--source-books-root", type=Path)
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--title-map", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--resolve-with-qwen", action="store_true")
    parser.add_argument("--allow-unresolved", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=ROOT.parent / "llm_cache_v106" / "qwen3-max-table-metadata-backfill")
    parser.add_argument("--resolution-file", type=Path)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "qwen3-max"))
    parser.add_argument("--batch-size", type=int, default=30)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.input_root and (args.source_books_root or args.results_root):
        raise ValueError("use either input-root or source-books-root/results-root, not both")
    if not args.input_root and not (args.source_books_root and args.results_root):
        raise ValueError("provide input-root or both source-books-root and results-root")
    if bool(args.source_books_root) != bool(args.results_root):
        raise ValueError("source-books-root and results-root must be provided together")

    input_root = args.input_root.resolve() if args.input_root else args.results_root.resolve()
    source_books_root = args.source_books_root.resolve() if args.source_books_root else None
    results_root = args.results_root.resolve() if args.results_root else None
    output_root = args.output_root.resolve()
    protected_root = results_root or input_root
    if protected_root == output_root:
        raise ValueError("results root and output-root must differ; the original batch is read-only")
    report_path = args.report_path or output_root / "table_metadata_backfill_report.json"
    resolution_path = args.resolution_file or args.cache_dir / "validated_resolutions.json"
    title_map = _load_title_map(args.title_map)

    jobs, catalogs, total, unresolved, book_reports = _audit_jobs(
        input_root,
        source_books_root=source_books_root,
        results_root=results_root,
        title_map=title_map,
    )
    plan = {
        "status": "planned" if args.plan_only else "running",
        "input_root": str(input_root),
        "source_books_root": str(source_books_root) if source_books_root else None,
        "results_root": str(results_root) if results_root else None,
        "output_root": str(output_root),
        "book_count": len(jobs),
        "missing_table_metadata_rows": total["missing_table_metadata_rows"],
        "deterministically_resolved_rows": total["deterministically_resolved_rows"],
        "ambiguous_rows": len(unresolved),
        "canonicalized_existing_title_rows": total["canonicalized_existing_title"],
        "books": book_reports,
    }
    _json_dump(report_path, plan)
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if args.plan_only:
        return 0

    resolutions = _load_json_map(resolution_path)
    qwen_stats: Counter[str] = Counter()
    qwen_rejected: list[dict[str, str]] = []
    if unresolved and args.resolve_with_qwen:
        client = QwenOpenAIClient(model=args.model, cache_dir=args.cache_dir, max_retries=6)
        client.assert_configured()
        resolutions, qwen_rejected, qwen_stats = _resolve_with_qwen(
            unresolved,
            catalogs,
            resolutions,
            client=client,
            resolution_path=resolution_path,
            batch_size=max(1, args.batch_size),
        )

    output_root.mkdir(parents=True, exist_ok=True)
    _copy_batch_sidecars(output_root, jobs)
    final_total: Counter[str] = Counter()
    final_unresolved: list[dict[str, str]] = []
    for job in jobs:
        target = output_root / job.relative_output_dir
        report = repair_book_directory(
            job.source_book_dir,
            target,
            job.source_markdown,
            resolutions=resolutions,
        )
        for key, value in report.items():
            if key == "unresolved_rows":
                final_unresolved.extend(value)  # type: ignore[arg-type]
            elif isinstance(value, int):
                final_total[key] += value
        print(f"[REPAIRED] {job.book_title} unresolved={len(report['unresolved_rows'])}", flush=True)

    unresolved_path = output_root / "table_metadata_backfill_unresolved.tsv"
    _write_unresolved(unresolved_path, final_unresolved)
    final_report = {
        **plan,
        "status": "complete" if not final_unresolved else "complete_with_unresolved",
        "validated_resolution_count": len(resolutions),
        "qwen": dict(qwen_stats),
        "qwen_rejected_after_retry": len(qwen_rejected),
        "final": dict(final_total),
        "final_unresolved_rows": len(final_unresolved),
        "unresolved_report": str(unresolved_path),
    }
    _json_dump(output_root / "table_metadata_backfill_report.json", final_report)
    if report_path != output_root / "table_metadata_backfill_report.json":
        _json_dump(report_path, final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2), flush=True)
    if final_unresolved and not args.allow_unresolved:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
