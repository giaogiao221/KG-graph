from __future__ import annotations

from dataclasses import dataclass, field

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
class RuleTextAdapter:
    reader: DocumentReader = field(default_factory=MarkdownDocumentReader)
    engine: RuleEngine = field(default_factory=BasicRuleEngine)
    name: str = "rule_text"
    version: str = "native-1"
    queue: QueueName = "rule"

    def run(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        document = require_document(context, self.reader)
        return write_route_output(
            context, self.name, self.engine.extract_text(document), emit_progress
        )
