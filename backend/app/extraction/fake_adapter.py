from __future__ import annotations

import json
from dataclasses import dataclass

from app.extraction.contracts import (
    AdapterContext,
    AdapterResult,
    ProgressEvent,
    ProgressSink,
    QueueName,
)


@dataclass(frozen=True, slots=True)
class FakeAdapter:
    name: str = "fake"
    version: str = "1"
    queue: QueueName = "rule"

    def run(
        self, context: AdapterContext, emit_progress: ProgressSink
    ) -> AdapterResult:
        context.work_dir.mkdir(parents=True, exist_ok=True)
        manifest = context.output_path("manifest.json")
        manifest.write_text(
            json.dumps(
                {
                    "records": [{"source": self.name}],
                    "execution_idempotency_key": context.execution_idempotency_key,
                }
            ),
            encoding="utf-8",
        )
        emit_progress(ProgressEvent(stage="completed", processed=1, total=1))
        return AdapterResult(manifest_path=manifest, metrics={"records": 1})
