from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, DDL, ForeignKey, Integer, JSON,
    LargeBinary, Numeric, String, Text, UniqueConstraint, event, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ModelConfig(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        UniqueConstraint("name", name="uq_model_config_name"),
        CheckConstraint("length(name) > 0", name="ck_model_config_name"),
        CheckConstraint("length(provider) > 0", name="ck_model_config_provider"),
        CheckConstraint("length(model_name) > 0", name="ck_model_config_model_name"),
        CheckConstraint("cipher_version >= 1", name="ck_model_config_cipher_version"),
        CheckConstraint("length(key_id) > 0", name="ck_model_config_key_id"),
        CheckConstraint("version >= 1", name="ck_model_config_version"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(2048), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cipher_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    key_id: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'primary'"))
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    allow_private_network: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    allow_insecure_http: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    provider_supports_idempotency: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), index=True)


class ModelPriceVersion(Base):
    __tablename__ = "model_price_versions"
    __table_args__ = (
        UniqueConstraint("model_config_id", "effective_from", name="uq_model_price_start"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_model_price_interval"),
        CheckConstraint("prompt_per_million >= 0", name="ck_model_price_prompt"),
        CheckConstraint("completion_per_million >= 0", name="ck_model_price_completion"),
        CheckConstraint("cached_per_million >= 0", name="ck_model_price_cached"),
        CheckConstraint("version >= 1", name="ck_model_price_version"),
    )

    model_config_id: Mapped[UUID] = mapped_column(ForeignKey("model_configs.id", ondelete="RESTRICT"), nullable=False, index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prompt_per_million: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    completion_per_million: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cached_per_million: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    model_config: Mapped[ModelConfig] = relationship()


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        UniqueConstraint("step_id", "idempotency_key", "attempt", "status", name="uq_model_call_attempt_status"),
        CheckConstraint("status IN ('success','failure','retry','cache_hit')", name="ck_model_call_status"),
        CheckConstraint("route IN ('llm_text','llm_table')", name="ck_model_call_route"),
        CheckConstraint("prompt_tokens >= 0 AND completion_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0", name="ck_model_call_tokens_nonnegative"),
        CheckConstraint("total_tokens = prompt_tokens + completion_tokens + cached_tokens", name="ck_model_call_tokens_total"),
        CheckConstraint("cost >= 0", name="ck_model_call_cost"),
        CheckConstraint("attempt >= 1", name="ck_model_call_attempt"),
        CheckConstraint("completed_at >= called_at", name="ck_model_call_times"),
        CheckConstraint("version >= 1", name="ck_model_call_version"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("extraction_batches.id", ondelete="RESTRICT"), nullable=False, index=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True)
    document_version_id: Mapped[UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False, index=True)
    document_job_id: Mapped[UUID] = mapped_column(ForeignKey("document_jobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    step_id: Mapped[UUID] = mapped_column(ForeignKey("job_steps.id", ondelete="RESTRICT"), nullable=False, index=True)
    model_config_id: Mapped[UUID] = mapped_column(ForeignKey("model_configs.id", ondelete="RESTRICT"), nullable=False, index=True)
    price_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_price_versions.id", ondelete="RESTRICT"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(2048), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    route: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _immutable(*_args: object) -> None:
    raise ValueError("model usage history is immutable")


for klass in (ModelPriceVersion, ModelCall):
    event.listen(klass, "before_update", _immutable)
    event.listen(klass, "before_delete", _immutable)
    for operation in ("UPDATE", "DELETE"):
        event.listen(klass.__table__, "after_create", DDL(f"""
            CREATE TRIGGER prevent_{klass.__tablename__}_{operation.lower()}
            BEFORE {operation} ON {klass.__tablename__}
            BEGIN SELECT RAISE(ABORT, 'model usage history is immutable'); END
        """).execute_if(dialect="sqlite"))

event.listen(ModelPriceVersion.__table__, "after_create", DDL("""
    CREATE TRIGGER enforce_model_price_no_overlap
    BEFORE INSERT ON model_price_versions
    WHEN EXISTS (
      SELECT 1 FROM model_price_versions p
      WHERE p.model_config_id = NEW.model_config_id
        AND p.effective_from < COALESCE(NEW.effective_to, '9999-12-31T23:59:59+00:00')
        AND NEW.effective_from < COALESCE(p.effective_to, '9999-12-31T23:59:59+00:00')
    )
    BEGIN SELECT RAISE(ABORT, 'model price interval overlaps'); END
""").execute_if(dialect="sqlite"))

event.listen(ModelCall.__table__, "after_create", DDL("""
    CREATE TRIGGER enforce_model_call_linkage
    BEFORE INSERT ON model_calls
    WHEN NOT EXISTS (
      SELECT 1 FROM job_steps s
      JOIN document_jobs j ON j.id=s.document_job_id
      JOIN extraction_batches b ON b.id=j.batch_id
      JOIN document_versions v ON v.id=j.document_version_id
      JOIN documents d ON d.id=v.document_id
      WHERE s.id=NEW.step_id AND s.kind=NEW.route
        AND j.id=NEW.document_job_id AND b.id=NEW.batch_id
        AND b.project_id=NEW.project_id AND v.id=NEW.document_version_id
        AND d.id=NEW.document_id
        AND COALESCE(b.created_by_id, d.created_by_id)=NEW.user_id
        AND EXISTS (SELECT 1 FROM model_configs mc WHERE mc.id=NEW.model_config_id
          AND mc.provider=NEW.provider AND mc.model_name=NEW.model_name AND mc.endpoint=NEW.endpoint)
        AND (NEW.price_version_id IS NULL OR EXISTS (
          SELECT 1 FROM model_price_versions p WHERE p.id=NEW.price_version_id
            AND p.model_config_id=NEW.model_config_id
            AND p.effective_from<=NEW.called_at
            AND (p.effective_to IS NULL OR p.effective_to>NEW.called_at)
        ))
    )
    BEGIN SELECT RAISE(ABORT, 'model call linkage mismatch'); END
""").execute_if(dialect="sqlite"))


from app.auth.models import User  # noqa: E402,F401
from app.documents.models import Document, DocumentVersion  # noqa: E402,F401
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep  # noqa: E402,F401
from app.projects.models import Project  # noqa: E402,F401
