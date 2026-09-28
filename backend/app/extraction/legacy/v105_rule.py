from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Sequence

from app.extraction.contracts import AdapterContext, AdapterResult, PermanentAdapterError, ProgressEvent, ProgressSink, RetryableAdapterError, StageCleanupError, validate_adapter_result
from app.extraction.legacy.bridge import write_script_route_output
from app.extraction.legacy.capabilities import LegacyCapability
from app.extraction.legacy.output_parser import LegacyParseError, parse_v105_report
from app.extraction.process_runner import ProcessRunner


# The v105 engine emits one combined TSV whose 来源类型 column distinguishes
# text facts from table facts.  Route-scoped legacy steps filter that column.
_ROUTE_SOURCE_KIND = {"rule_text": "文本", "rule_table": "表格"}


class V105RuleAdapter:
    name = "legacy_v105_rule"
    version = "105-script-bridge-v1"
    queue = "rule"
    capability = LegacyCapability(engine=name, mode="combined_rule_text_table", supported_route_sets=(("rule_table", "rule_text"),))

    def __init__(
        self,
        *,
        source_root: Path,
        input_source_roots: Sequence[Path] = (),
        engine_subdir: str = "model",
        config_subdir: str = "config",
        route: str | None = None,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.input_source_roots = tuple(Path(root).resolve() for root in input_source_roots)
        # Two layout parameters describe where the engine lives below source_root:
        #   KGchouqu_clean -> engine_subdir="model", config_subdir="config"
        #                     (model/src/book_engine + model/config)
        #   KGchouqu_hzy    -> engine_subdir=".",     config_subdir="src/config"
        #                     (src/book_engine + src/config)
        # Empty values collapse to "." so the package/config sit directly under root.
        self.engine_subdir = Path(engine_subdir.strip().strip("/") or ".")
        self.config_subdir = Path(config_subdir.strip().strip("/") or ".")
        if route is not None and route not in _ROUTE_SOURCE_KIND:
            raise PermanentAdapterError("legacy v105 route scope is invalid")
        self.route = route

    def require_routes(self, routes: set[str]) -> None:
        self.capability.require(routes)

    def for_route(self, route: str) -> "V105RuleAdapter":
        """Return a route-scoped variant that emits only that route's facts."""
        return V105RuleAdapter(
            source_root=self.source_root,
            input_source_roots=self.input_source_roots,
            engine_subdir=str(self.engine_subdir),
            config_subdir=str(self.config_subdir),
            route=route,
        )

    def run(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        route = self.route or "rule_text"
        if context.shared_source_tsv is not None:
            return self._emit_route_from_shared(context, route, context.shared_source_tsv, emit_progress)
        profile = context.profile_snapshot
        compatibility_mode = context.document_path is None
        if compatibility_mode:
            self.capability.require(set(profile.get("routes", ())))
        document_path = context.document_path or Path(str(profile.get("source_document", "")))
        if not document_path.is_file():
            raise PermanentAdapterError("KGchouqu source document is unavailable")
        engine = self.source_root / self.engine_subdir
        source_package = engine / "src" / "book_engine"
        config_root = self.source_root / self.config_subdir
        config = Path(str(profile.get("config", ""))) if compatibility_mode else config_root / "pipeline_stages_v2.yaml"
        if not source_package.is_dir() or not config.is_file():
            raise PermanentAdapterError("KGchouqu v105 engine is not configured")

        runner = ProcessRunner(
            allowed_work_root=context.work_dir,
            allowed_source_roots=(*self.input_source_roots, self.source_root),
            timeout_seconds=1800,
            max_copy_files=50_000,
            max_copy_bytes=2 * 1024 * 1024 * 1024,
        )
        budget = runner.new_stage_budget()
        resolved_document = document_path.resolve()
        resolved_work_dir = context.work_dir.resolve()
        if resolved_document == resolved_work_dir or resolved_work_dir in resolved_document.parents:
            # Job execution already copied and verified this document inside the
            # isolated work directory. Restaging it would incorrectly treat it
            # as an external read-only source.
            document = resolved_document
        else:
            document = runner.stage_source_document(document_path, cwd=context.work_dir, stage_name="document", budget=budget)
        if compatibility_mode:
            staged_config = runner.stage_source_document(config, cwd=context.work_dir, stage_name="config", budget=budget)
        else:
            staged_config_root = runner.stage_source_tree(config_root, cwd=context.work_dir, name="config", area="staged-engine", budget=budget)
            staged_config = staged_config_root / "pipeline_stages_v2.yaml"
        package = runner.stage_source_tree(source_package, cwd=context.work_dir, name="book_engine", area="staged-engine/v105", budget=budget)
        output = context.output_path("legacy_v105" if compatibility_mode else "kgchouqu-v105")
        output.mkdir(parents=True, exist_ok=True)
        emit_progress(ProgressEvent(stage="kgchouqu_v105.started"))
        try:
            runner.seal_staging()
            result = runner.run([
                sys.executable, "-m", "book_engine.cli",
                "--input-md", str(document),
                "--output-dir", str(output),
                "--config", str(staged_config),
                "--mode", "production",
            ], cwd=context.work_dir, env={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(package.parent),
            })
        except Exception:
            try:
                cleanup_ok = runner.discard_stage()
            except BaseException:
                cleanup_ok = False
            if not cleanup_ok:
                raise StageCleanupError("stage cleanup could not be confirmed") from None
            raise
        if result.timed_out:
            raise RetryableAdapterError("KGchouqu v105 execution timed out")
        if result.exit_code != 0:
            raise PermanentAdapterError("KGchouqu v105 execution failed")

        report_path = output / "book_engine_run_report.json"
        if compatibility_mode:
            try:
                parsed = parse_v105_report(report_path)
            except LegacyParseError:
                raise PermanentAdapterError("legacy v105 output is invalid") from None
            return validate_adapter_result(context, AdapterResult(report_path, parsed.metrics))
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            export = report["schema59_merged_export"]
            source_tsv = output / "step_graph_guard" / "graph_import_ready_generalized.tsv"
            if not source_tsv.is_file():
                source_tsv = Path(str(export["graph_import_ready"]))
            if not source_tsv.is_file() or output.resolve() not in source_tsv.resolve().parents:
                raise ValueError
        except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise PermanentAdapterError("KGchouqu v105 output is invalid") from None
        script_rows = int(export.get("generalized_release_rows", export.get("rows", 0)) or 0)
        if self.route is not None:
            return self._emit_route_from_shared(
                context, self.route, source_tsv, emit_progress, script_rows=script_rows
            )
        return write_script_route_output(
            context, "rule_text", source_tsv, emit_progress,
            executor=self.name, executor_version=self.version,
            metrics={"script_rows": script_rows},
            shared_artifact_name="shared_source_tsv",
        )

    def _emit_route_from_shared(
        self,
        context: AdapterContext,
        route: str,
        source_tsv: Path,
        emit_progress: ProgressSink,
        *,
        script_rows: int | None = None,
    ) -> AdapterResult:
        metrics: dict[str, int | float] = {"reused_shared_output": 1}
        if script_rows is not None:
            metrics["script_rows"] = script_rows
        return write_script_route_output(
            context, route, source_tsv, emit_progress,
            executor=self.name, executor_version=self.version,
            metrics=metrics,
            source_kind=_ROUTE_SOURCE_KIND[route],
        )
