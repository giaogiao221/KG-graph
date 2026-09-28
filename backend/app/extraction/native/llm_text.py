from __future__ import annotations

from dataclasses import dataclass, field

from app.extraction.contracts import (
    AdapterContext,
    AdapterResult,
    PermanentAdapterError,
    ProgressSink,
    QueueName,
)
from app.extraction.native.common import (
    DocumentReader,
    MarkdownDocumentReader,
    ModelClient,
    UnconfiguredModelClient,
    ModelResult,
    PersistentUnitCache,
    require_document,
    text_windows,
    unit_idempotency_key,
    write_route_output,
)


@dataclass(frozen=True, slots=True)
class LLMTextAdapter:
    reader: DocumentReader = field(default_factory=MarkdownDocumentReader)
    client: ModelClient = field(default_factory=UnconfiguredModelClient)
    name: str = "llm_text"
    version: str = "pure-llm-schema59-v1"
    queue: QueueName = "llm"

    def run(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        document = require_document(context, self.reader)
        if context.model_config_id is None:
            raise PermanentAdapterError("native model client is not configured")
        candidates = []
        metrics = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "model_calls": 0}
        cache_root = context.persistent_cache_root or context.output_path(f"{self.name}/cache")
        cache = PersistentUnitCache(cache_root, context.execution_idempotency_key, self.name)
        for index, window in enumerate(text_windows(document.text)):
            unit_key = unit_idempotency_key(context.execution_idempotency_key, self.name, index, window)
            def compute():
                options = {}
                if "prompt_snapshot" in context.profile_snapshot:
                    options["prompt_snapshot"] = context.profile_snapshot["prompt_snapshot"]
                raw = self.client.extract(
                route=self.name,
                payload=window,
                idempotency_key=unit_key,
                model_config_id=str(context.model_config_id),
                **options,
            )
                return raw if isinstance(raw, ModelResult) else ModelResult(tuple(raw), {"prompt_tokens":0,"completion_tokens":0,"cached_tokens":0,"total_tokens":0,"model_calls":1})
            def cache_hit(key, cached):
                recorder = getattr(self.client, "record_cache_hit", None)
                call_id = recorder(route=self.name, idempotency_key=key, model_config_id=str(context.model_config_id)) if recorder else None
                return ModelResult(cached.candidates, {"prompt_tokens":0,"completion_tokens":0,"cached_tokens":0,"total_tokens":0,"model_calls":1}, call_id)
            result = cache.get_or_compute(unit_key, compute, cache_hit)
            for candidate in result.candidates:
                candidates.append({**candidate, "model_call_id": result.model_call_id or unit_key})
            for name in metrics: metrics[name] += int(result.metrics.get(name, 0))
        return write_route_output(context, self.name, candidates, emit_progress, metrics)
