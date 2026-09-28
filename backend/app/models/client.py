from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.documents.models import Document, DocumentVersion
from app.extraction.contracts import PermanentAdapterError, RetryableAdapterError
from app.extraction.native.common import (
    ModelConfigurationError, ModelResult, OpenAICompatibleModelClient,
    resolve_model_configuration,
)
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
from app.models.crypto import ModelSecretError
from app.models.models import ModelConfig
from app.models.service import CallScope, record_model_call, resolved_secret


class DatabaseModelClient:
    """DB-configured provider client with short, independent accounting transactions."""

    def __init__(self, factory: sessionmaker[Session], step_id: UUID, *, transport_client: OpenAICompatibleModelClient | None = None):
        self.factory = factory
        self.step_id = step_id
        self._transport = transport_client or OpenAICompatibleModelClient(
            configuration_resolver=self._resolve, observer=self._observe
        )

    def _resolve(self, config_id: str):
        try:
            value = UUID(config_id)
        except ValueError:
            raise ModelConfigurationError("native model client is not configured") from None
        with self.factory() as session:
            config = session.get(ModelConfig, value)
            if config is None or not config.is_enabled:
                raise ModelConfigurationError("native model client is not configured")
            try:
                secret = resolved_secret(config)
            except ModelSecretError:
                raise ModelConfigurationError("native model client is not configured") from None
            values = (config.endpoint, config.model_name, secret, tuple(config.allowed_hosts),
                      config.allow_private_network, config.allow_insecure_http,
                      config.provider_supports_idempotency)
        # DNS can block; resolve only after the short configuration transaction closes.
        return resolve_model_configuration(endpoint=values[0], model=values[1], secret=values[2],
            allowed_hosts=values[3], allow_private_network=values[4], allow_insecure_http=values[5],
            provider_supports_idempotency=values[6])

    def _scope(self, session: Session) -> tuple[CallScope, int]:
        row = session.execute(
            select(JobStep, DocumentJob, ExtractionBatch, DocumentVersion, Document)
            .join(DocumentJob, JobStep.document_job_id == DocumentJob.id)
            .join(ExtractionBatch, DocumentJob.batch_id == ExtractionBatch.id)
            .join(DocumentVersion, DocumentJob.document_version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(JobStep.id == self.step_id)
        ).one_or_none()
        if row is None:
            raise ModelConfigurationError("native model client is not configured")
        step, job, batch, version, document = row
        return CallScope(user_id=batch.created_by_id or document.created_by_id, project_id=batch.project_id,
            batch_id=batch.id, document_id=document.id, document_version_id=version.id,
            document_job_id=job.id, step_id=step.id), max(1, step.attempt_count)

    def _observe(self, *, route: str, idempotency_key: str, model_config_id: str,
                 metrics: Mapping[str, int] | None = None, error: Exception | None = None,
                 cache_hit: bool = False, called_at: datetime | None = None) -> str:
        with self.factory() as session:
            scope, attempt = self._scope(session)
            status = "cache_hit" if cache_hit else ("retry" if isinstance(error, RetryableAdapterError) else ("failure" if error else "success"))
            usage = metrics or {}
            call = record_model_call(session, scope=scope, model_config_id=UUID(model_config_id),
                purpose="fact_extraction", route=route, status=status,
                idempotency_key=idempotency_key, attempt=attempt,
                prompt_tokens=int(usage.get("prompt_tokens",0)), completion_tokens=int(usage.get("completion_tokens",0)),
                cached_tokens=int(usage.get("cached_tokens",0)), error_code=("provider_unavailable" if status == "retry" else ("provider_response_invalid" if status == "failure" else None)),
                called_at=called_at or datetime.now(UTC), completed_at=datetime.now(UTC))
            return str(call.id)

    def extract(self, *, route: str, payload: str, idempotency_key: str, model_config_id: str, prompt_snapshot: object | None = None) -> ModelResult:
        options = {"prompt_snapshot": prompt_snapshot} if prompt_snapshot is not None else {}
        return self._transport.extract(route=route, payload=payload, idempotency_key=idempotency_key, model_config_id=model_config_id, **options)

    def record_cache_hit(self, *, route: str, idempotency_key: str, model_config_id: str) -> str:
        return self._observe(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,cache_hit=True)
