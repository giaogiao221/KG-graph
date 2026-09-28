from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import sys
from typing import Sequence

from app.extraction.contracts import AdapterContext, AdapterResult, PermanentAdapterError, ProgressEvent, ProgressSink, RetryableAdapterError, StageCleanupError, validate_adapter_result
from app.extraction.legacy.bridge import write_script_route_output
from app.extraction.legacy.capabilities import LegacyCapability
from app.extraction.legacy.output_parser import LegacyParseError, parse_v106_report, resolve_legacy_output
from app.extraction.process_runner import ProcessRunner


def _base_url(endpoint: str) -> str:
    value = endpoint.strip().rstrip("/")
    suffix = "/chat/completions"
    return value[:-len(suffix)] if value.endswith(suffix) else value


class V106HybridAdapter:
    name = "legacy_v106_hybrid"
    version = "106-script-bridge-v1"
    queue = "llm"
    capability = LegacyCapability(engine=name, mode="combined_hybrid_compatibility", supported_route_sets=(("llm_table", "llm_text", "rule_table", "rule_text"),))

    def __init__(
        self,
        *,
        source_root: Path,
        input_source_roots: Sequence[Path] = (),
        engine_subdir: str = "model",
        config_subdir: str = "config",
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.input_source_roots = tuple(Path(root).resolve() for root in input_source_roots)
        # Two layout parameters describe where the engine lives below source_root:
        #   KGchouqu_clean -> engine_subdir="model", config_subdir="config"
        #                     (model/src/book_engine + model/config)
        #   KGchouqu_hzy    -> engine_subdir=".",     config_subdir="src/config"
        #                     (src/book_engine + src/config)
        self.engine_subdir = Path(engine_subdir.strip().strip("/") or ".")
        self.config_subdir = Path(config_subdir.strip().strip("/") or ".")

    def require_routes(self, routes: set[str]) -> None:
        self.capability.require(routes)

    def run(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        if context.document_path is None:
            return self._run_compatibility(context, emit_progress)
        if context.model_config_id is None:
            raise PermanentAdapterError("KGchouqu hybrid input is unavailable")
        profile = context.profile_snapshot
        api_key = str(profile.get("legacy_model_api_key", ""))
        endpoint = _base_url(str(profile.get("legacy_model_endpoint", "")))
        model = str(profile.get("legacy_model_name", ""))
        if not api_key or not endpoint or not model:
            raise PermanentAdapterError("KGchouqu hybrid model is not configured")

        engine = self.source_root / self.engine_subdir
        source_package = engine / "src" / "book_engine"
        config_root = self.source_root / self.config_subdir
        config = config_root / "pipeline_stages_v2.yaml"
        ontology = config_root / "property_ontology_v2.tsv"
        if not source_package.is_dir() or not config.is_file() or not ontology.is_file():
            raise PermanentAdapterError("KGchouqu v106 engine is not configured")

        runner = ProcessRunner(
            allowed_work_root=context.work_dir,
            allowed_source_roots=(*self.input_source_roots, self.source_root),
            timeout_seconds=7200,
            max_copy_files=50_000,
            max_copy_bytes=2 * 1024 * 1024 * 1024,
        )
        budget = runner.new_stage_budget()
        resolved_document = context.document_path.resolve()
        resolved_work_dir = context.work_dir.resolve()
        if resolved_document == resolved_work_dir or resolved_work_dir in resolved_document.parents:
            # The job worker has already copied and verified this input inside
            # the isolated work directory.
            document = resolved_document
        else:
            document = runner.stage_source_document(context.document_path, cwd=context.work_dir, stage_name="document", budget=budget)
        staged_config_root = runner.stage_source_tree(config_root, cwd=context.work_dir, name="config", area="staged-engine", budget=budget)
        staged_config = staged_config_root / "pipeline_stages_v2.yaml"
        staged_ontology = staged_config_root / "property_ontology_v2.tsv"
        package = runner.stage_source_tree(source_package, cwd=context.work_dir, name="book_engine", area="staged-engine/v106", budget=budget)

        books = context.output_path("kgchouqu-books")
        v105_root = context.output_path("kgchouqu-v105-root")
        v105_book = v105_root / "book-1"
        v106_output = context.output_path("kgchouqu-v106")
        cache_key = hashlib.sha256(
            f"{context.model_config_id}:{context.execution_idempotency_key}".encode("utf-8")
        ).hexdigest()
        cache_root = (context.persistent_cache_root or context.work_dir / "persistent-cache").resolve()
        cache = (cache_root / "kgchouqu-v106" / cache_key).resolve()
        if cache_root != cache and cache_root not in cache.parents:
            raise PermanentAdapterError("KGchouqu v106 cache path is invalid")
        for directory in (books, v105_book, v106_output, cache):
            directory.mkdir(parents=True, exist_ok=True)
        book_source = books / document.name
        shutil.copy2(document, book_source)

        environment = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(package.parent),
            "OPENAI_API_KEY": api_key,
            "OPENAI_BASE_URL": endpoint,
            "OPENAI_MODEL": model,
        }
        emit_progress(ProgressEvent(stage="kgchouqu_v105.started"))
        try:
            runner.seal_staging()
            v105 = runner.run([
                sys.executable, "-m", "book_engine.cli",
                "--input-md", str(book_source),
                "--output-dir", str(v105_book),
                "--config", str(staged_config),
                "--mode", "production",
            ], cwd=context.work_dir, env=environment)
            if v105.timed_out:
                raise RetryableAdapterError("KGchouqu v105 execution timed out")
            if v105.exit_code != 0:
                raise PermanentAdapterError("KGchouqu v105 execution failed")
            emit_progress(ProgressEvent(stage="kgchouqu_v106.started"))
            v106 = runner.run([
                sys.executable, "-m", "book_engine.llm_v106.pipeline",
                "--books-dir", str(books),
                "--v105-root", str(v105_root),
                "--output-root", str(v106_output),
                "--property-ontology", str(staged_ontology),
                "--cache-dir", str(cache),
                "--model", model,
            ], cwd=context.work_dir, env=environment)
        except (PermanentAdapterError, RetryableAdapterError):
            raise
        except Exception:
            try:
                cleanup_ok = runner.discard_stage()
            except BaseException:
                cleanup_ok = False
            if not cleanup_ok:
                raise StageCleanupError("KGchouqu staging cleanup could not be confirmed") from None
            raise
        if v106.timed_out:
            raise RetryableAdapterError("KGchouqu v106 execution timed out")
        if v106.exit_code != 0:
            raise RetryableAdapterError("KGchouqu v106 execution failed")

        report_path = v106_output / "v106_qwen3_max_batch_report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            books_report = report.get("books")
            if report.get("ok") is not True or not isinstance(books_report, list) or len(books_report) != 1:
                raise ValueError
            outputs = books_report[0].get("outputs") if isinstance(books_report[0], dict) else None
            declared = outputs.get("final") if isinstance(outputs, dict) else None
            if not isinstance(declared, str) or not declared:
                raise ValueError
            source_tsv = Path(declared).resolve()
            resolved_output = v106_output.resolve()
            if resolved_output not in source_tsv.parents or not source_tsv.is_file():
                raise ValueError
            metrics = {
                "final_rows": int(report.get("final_rows", 0) or 0),
                "manual_review_rows": int(report.get("manual_review_rows", 0) or 0),
                "llm_jobs": int(report.get("llm_jobs", 0) or 0),
                "llm_error_jobs": int(report.get("llm_error_jobs", 0) or 0),
            }
        except FileNotFoundError:
            raise PermanentAdapterError("KGchouqu v106 report or final TSV is missing") from None
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            raise PermanentAdapterError("KGchouqu v106 report does not declare one valid final TSV") from None
        return write_script_route_output(
            context, "llm_text", source_tsv, emit_progress,
            executor=self.name, executor_version=self.version, metrics=metrics,
        )

    def _run_compatibility(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        profile = context.profile_snapshot
        self.capability.require(set(profile.get("routes", ())))
        try:
            books_dir = Path(str(profile["books_dir"])); v105_root = Path(str(profile["v105_root"])); ontology = Path(str(profile["property_ontology"]))
        except KeyError as exc:
            raise PermanentAdapterError(f"missing v106 adapter input: {exc.args[0]}") from None
        runner = ProcessRunner(allowed_work_root=context.work_dir, allowed_source_roots=(*self.input_source_roots, self.source_root), timeout_seconds=float(profile.get("timeout_seconds", 1800)), max_copy_files=50_000, max_copy_bytes=2 * 1024 * 1024 * 1024)
        budget = runner.new_stage_budget()
        staged_books = runner.stage_source_tree(books_dir, cwd=context.work_dir, name="books", budget=budget)
        staged_v105 = runner.stage_source_tree(v105_root, cwd=context.work_dir, name="v105-root", budget=budget)
        staged_ontology = runner.stage_source_document(ontology, cwd=context.work_dir, stage_name="ontology", budget=budget)
        package = runner.stage_source_tree(self.source_root / self.engine_subdir / "src" / "book_engine", cwd=context.work_dir, name="book_engine", area="staged-engine/v106", budget=budget)
        output = context.output_path("legacy_v106"); cache = context.output_path("legacy_v106_cache"); output.mkdir(parents=True, exist_ok=True); cache.mkdir(parents=True, exist_ok=True)
        argv = [str(profile.get("python_executable", sys.executable)), "-m", "book_engine.llm_v106.pipeline", "--books-dir", str(staged_books), "--v105-root", str(staged_v105), "--output-root", str(output), "--property-ontology", str(staged_ontology), "--cache-dir", str(cache)]
        if bool(profile.get("plan_only", False)): argv.append("--plan-only")
        try:
            runner.seal_staging()
            result = runner.run(argv, cwd=context.work_dir, env={"PYTHONDONTWRITEBYTECODE":"1", "PYTHONIOENCODING":"utf-8", "PYTHONPATH":str(package.parent), "OPENAI_API_KEY":str(profile.get("api_key", "")), "OPENAI_BASE_URL":str(profile.get("base_url", "")), "OPENAI_MODEL":str(profile.get("model", "qwen3-max"))})
        except Exception:
            try: cleanup_ok = runner.discard_stage()
            except BaseException: cleanup_ok = False
            if not cleanup_ok: raise StageCleanupError("legacy v106 staging cleanup could not be confirmed") from None
            raise
        if result.timed_out: raise RetryableAdapterError("legacy v106 execution timed out")
        if result.exit_code != 0: raise RetryableAdapterError("legacy v106 execution failed")
        report_path = output / "v106_qwen3_max_batch_report.json"
        try:
            parsed = parse_v106_report(report_path)
            for relative_path in parsed.outputs.values(): resolve_legacy_output(context.work_dir, relative_path)
        except LegacyParseError:
            raise PermanentAdapterError("legacy v106 output is invalid") from None
        return validate_adapter_result(context, AdapterResult(report_path, parsed.metrics))
