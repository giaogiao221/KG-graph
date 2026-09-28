from __future__ import annotations

from dataclasses import dataclass, field
from itertools import chain

from app.extraction.contracts import AdapterContext, AdapterResult, ProgressSink, QueueName
from app.extraction.native.common import (
    BasicRuleEngine,
    DocumentReader,
    MarkdownDocumentReader,
    RuleEngine,
    require_document,
    write_route_output,
)


@dataclass(frozen=True, slots=True)
class RuleTableAdapter:
    reader: DocumentReader = field(default_factory=MarkdownDocumentReader)
    engine: RuleEngine = field(default_factory=BasicRuleEngine)
    name: str = "rule_table"
    version: str = "native-1"
    queue: QueueName = "rule"

    def run(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        document = require_document(context, self.reader)
        candidates = chain.from_iterable(
            self.engine.extract_table(table) for table in document.tables
        )
        result = write_route_output(context, self.name, candidates, emit_progress)
        return AdapterResult(
            result.manifest_path,
            {**result.metrics, "table_candidates": result.metrics["records"]},
        )
