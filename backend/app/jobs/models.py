from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


JOB_STATUSES = (
    "queued",
    "running",
    "partial_success",
    "completed",
    "retryable_failed",
    "permanent_failed",
    "cancelled",
)
STEP_KINDS = (
    "rule_text",
    "llm_text",
    "rule_table",
    "llm_table",
    "merge",
    "validate",
)
ARTIFACT_STATUSES = (
    "reserved", "writing", "ready", "referenced", "expiring", "expired",
    "importing", "import_cleanup", "imported_cleanup", "cleanup_failed",
    "ownership_failed",
)


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ExtractionBatch(Base):
    __tablename__ = "extraction_batches"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_values(JOB_STATUSES)})", name="ck_extraction_batch_status"
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    profile_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    request_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default=text("'queued'")
    )
    is_archived: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    project: Mapped["Project"] = relationship()
    profile_version: Mapped["ProfileVersion"] = relationship()
    created_by: Mapped["User | None"] = relationship()
    document_jobs: Mapped[list["DocumentJob"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="DocumentJob.created_at",
    )


class DocumentJob(Base):
    __tablename__ = "document_jobs"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "document_version_id", name="uq_document_job_batch_document"
        ),
        CheckConstraint(
            f"status IN ({_values(JOB_STATUSES)})", name="ck_document_job_status"
        ),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default=text("'queued'")
    )
    batch: Mapped[ExtractionBatch] = relationship(back_populates="document_jobs")
    document_version: Mapped["DocumentVersion"] = relationship()
    steps: Mapped[list["JobStep"]] = relationship(
        back_populates="document_job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="JobStep.position",
    )

    @property
    def original_filename(self) -> str:
        return self.document_version.original_filename

    @property
    def document_version_number(self) -> int:
        return self.document_version.version_number


class JobStep(Base):
    __tablename__ = "job_steps"
    __table_args__ = (
        UniqueConstraint("document_job_id", "kind", name="uq_job_step_kind"),
        UniqueConstraint(
            "document_job_id", "position", name="uq_job_step_position"
        ),
        UniqueConstraint(
            "document_job_id",
            "idempotency_key",
            name="uq_job_step_execution_fingerprint",
        ),
        CheckConstraint(f"kind IN ({_values(STEP_KINDS)})", name="ck_job_step_kind"),
        CheckConstraint(
            f"status IN ({_values(JOB_STATUSES)})", name="ck_job_step_status"
        ),
        CheckConstraint("version >= 1", name="ck_job_step_version"),
        CheckConstraint("position >= 0", name="ck_job_step_position"),
        CheckConstraint("stage >= 0", name="ck_job_step_stage"),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 3",
            name="ck_job_step_attempt_count",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status != 'running' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_job_step_lease_pair",
        ),
        CheckConstraint(
            "(kind IN ('rule_text', 'llm_text', 'rule_table', 'llm_table') AND stage = 0) "
            "OR (kind = 'merge' AND stage = 1) "
            "OR (kind = 'validate' AND stage = 2)",
            name="ck_job_step_kind_stage",
        ),
    )

    document_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default=text("'queued'")
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    document_job: Mapped[DocumentJob] = relationship(back_populates="steps")
    artifacts: Mapped[list["JobArtifact"]] = relationship(
        back_populates="step", cascade="all, delete-orphan", lazy="selectin"
    )


class JobArtifact(Base):
    __tablename__ = "job_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('manifest', 'candidate_tsv')",
            name="ck_job_artifact_kind",
        ),
        CheckConstraint(
            f"status IN ({_values(ARTIFACT_STATUSES)})",
            name="ck_job_artifact_status",
        ),
        CheckConstraint(
            "(status IN ('reserved', 'writing') AND size_bytes IS NULL AND sha256 IS NULL) OR "
            "(status IN ('ready', 'referenced', 'importing', 'import_cleanup') AND "
            "size_bytes IS NOT NULL AND size_bytes >= 0 AND sha256 IS NOT NULL AND length(sha256) = 64) OR "
            "(status IN ('expiring', 'cleanup_failed', 'expired', 'imported_cleanup', 'ownership_failed') AND "
            "((size_bytes IS NULL AND sha256 IS NULL) OR "
            "(size_bytes IS NOT NULL AND size_bytes >= 0 AND sha256 IS NOT NULL AND length(sha256) = 64)))",
            name="ck_job_artifact_metadata",
        ),
        CheckConstraint(
            "length(owner_token) = 32",
            name="ck_job_artifact_owner_token",
        ),
        CheckConstraint(
            "storage_key LIKE 'artifacts/' || owner_token || '/payload.%'",
            name="ck_job_artifact_storage_key",
        ),
        CheckConstraint(
            "(status IN ('expiring', 'importing', 'import_cleanup', 'cleanup_failed') AND "
            "claim_token IS NOT NULL AND claimed_at IS NOT NULL) OR "
            "(status NOT IN ('expiring', 'importing', 'import_cleanup', 'cleanup_failed') AND "
            "claim_token IS NULL AND claimed_at IS NULL)",
            name="ck_job_artifact_claim",
        ),
        CheckConstraint("version >= 1", name="ck_job_artifact_version"),
    )

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    step_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_token: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved", server_default=text("'reserved'"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step: Mapped[JobStep] = relationship(back_populates="artifacts")


class JobDispatchOutbox(Base):
    __tablename__ = "job_dispatch_outbox"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "dispatch_generation",
            name="uq_job_dispatch_step_generation",
        ),
        CheckConstraint("queue IN ('rule', 'llm')", name="ck_job_dispatch_queue"),
        CheckConstraint(
            "dispatch_generation >= 1", name="ck_job_dispatch_generation"
        ),
        CheckConstraint(
            "publish_attempts >= 0", name="ck_job_dispatch_publish_attempts"
        ),
        CheckConstraint(
            "(claim_token IS NULL AND claimed_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_job_dispatch_claim_pair",
        ),
    )

    step_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    queue: Mapped[str] = mapped_column(String(16), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(String(200), nullable=True)


class JobCleanupOutbox(Base):
    __tablename__ = "job_cleanup_outbox"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "attempt_number",
            "work_token",
            name="uq_job_cleanup_attempt_token",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed')", name="ck_job_cleanup_status"
        ),
        CheckConstraint("attempt_number >= 1", name="ck_job_cleanup_attempt"),
        CheckConstraint("retry_count >= 0", name="ck_job_cleanup_retry_count"),
        CheckConstraint(
            "(claim_token IS NULL AND claimed_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_job_cleanup_claim_pair",
        ),
    )

    step_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    work_token: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


from app.documents.models import Document, DocumentVersion  # noqa: E402, F401
from app.profiles.models import (  # noqa: E402, F401
    ExtractionProfile,
    ProfileVersion,
)
from app.projects.models import Project  # noqa: E402, F401
from app.auth.models import User  # noqa: E402, F401


for operation in ("INSERT", "UPDATE"):
    event.listen(
        ExtractionBatch.__table__,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER enforce_batch_profile_project_{operation.lower()}
            BEFORE {operation} ON extraction_batches
            WHEN NOT EXISTS (
                SELECT 1
                FROM profile_versions AS pv
                JOIN extraction_profiles AS ep ON ep.id = pv.profile_id
                WHERE pv.id = NEW.profile_version_id
                  AND ep.project_id = NEW.project_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'batch profile project mismatch');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        DocumentJob.__table__,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER enforce_job_document_project_{operation.lower()}
            BEFORE {operation} ON document_jobs
            WHEN NOT EXISTS (
                SELECT 1
                FROM extraction_batches AS eb
                JOIN document_versions AS dv ON dv.id = NEW.document_version_id
                JOIN documents AS d ON d.id = dv.document_id
                WHERE eb.id = NEW.batch_id
                  AND eb.project_id = d.project_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'job document project mismatch');
            END
            """
        ).execute_if(dialect="sqlite"),
    )


event.listen(
    ExtractionBatch.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER prevent_batch_project_change_breaking_jobs
        BEFORE UPDATE OF project_id ON extraction_batches
        WHEN EXISTS (
            SELECT 1
            FROM document_jobs AS dj
            JOIN document_versions AS dv ON dv.id = dj.document_version_id
            JOIN documents AS d ON d.id = dv.document_id
            WHERE dj.batch_id = OLD.id
              AND d.project_id != NEW.project_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'batch project change breaks jobs');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    Document.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER prevent_document_project_change_breaking_jobs
        BEFORE UPDATE OF project_id ON documents
        WHEN EXISTS (
            SELECT 1
            FROM document_versions AS dv
            JOIN document_jobs AS dj ON dj.document_version_id = dv.id
            JOIN extraction_batches AS eb ON eb.id = dj.batch_id
            WHERE dv.document_id = OLD.id
              AND eb.project_id != NEW.project_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'document project change breaks jobs');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ExtractionProfile.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER prevent_profile_project_change_breaking_batches
        BEFORE UPDATE OF project_id ON extraction_profiles
        WHEN EXISTS (
            SELECT 1
            FROM profile_versions AS pv
            JOIN extraction_batches AS eb ON eb.profile_version_id = pv.id
            WHERE pv.profile_id = OLD.id
              AND eb.project_id != NEW.project_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile project change breaks batches');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    JobArtifact.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER prevent_job_artifact_owner_change
        BEFORE UPDATE OF owner_token ON job_artifacts
        WHEN NEW.owner_token != OLD.owner_token
        BEGIN
            SELECT RAISE(ABORT, 'job artifact owner is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
