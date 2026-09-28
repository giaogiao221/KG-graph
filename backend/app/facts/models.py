from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DDL,
    Float,
    ForeignKey,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RawFact(Base):
    __tablename__ = "raw_facts"
    __table_args__ = (
        UniqueConstraint(
            "document_job_id", "dedup_key", name="uq_raw_fact_job_dedup"
        ),
        CheckConstraint("length(dedup_key) = 66", name="ck_raw_fact_dedup_key"),
        CheckConstraint("version >= 1", name="ck_raw_fact_version"),
        CheckConstraint(
            "graph_fact_key IS NULL OR length(graph_fact_key) > 0",
            name="ck_raw_fact_graph_key",
        ),
        CheckConstraint("length(evidence_hash) = 64", name="ck_raw_fact_evidence_hash"),
        CheckConstraint("length(evidence_text) > 0", name="ck_raw_fact_evidence_text"),
        CheckConstraint(
            "review_status IN ('candidate', 'candidate_review', 'approved', 'rejected')",
            name="ck_raw_fact_review_status",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    document_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    import_step_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_steps.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_step_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_steps.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    graph_fact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(66), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    property: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(512), nullable=False)
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_source: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    route: Mapped[str] = mapped_column(String(128), nullable=False)
    row_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)

    project: Mapped["Project"] = relationship()
    document: Mapped["Document"] = relationship(foreign_keys=[document_id])
    document_version: Mapped["DocumentVersion"] = relationship(
        foreign_keys=[document_version_id]
    )
    document_job: Mapped["DocumentJob"] = relationship(foreign_keys=[document_job_id])
    import_step: Mapped["JobStep"] = relationship(foreign_keys=[import_step_id])
    source_step: Mapped["JobStep"] = relationship(foreign_keys=[source_step_id])


@event.listens_for(RawFact, "before_update")
@event.listens_for(RawFact, "before_delete")
def _prevent_raw_fact_mutation(*_args: object) -> None:
    raise ValueError("raw facts are immutable")


for operation in ("UPDATE", "DELETE"):
    event.listen(
        RawFact.__table__,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER prevent_raw_fact_{operation.lower()}
            BEFORE {operation} ON raw_facts
            BEGIN
                SELECT RAISE(ABORT, 'raw facts are immutable');
            END
            """
        ).execute_if(dialect="sqlite"),
    )

event.listen(
    RawFact.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER enforce_raw_fact_linkage
        BEFORE INSERT ON raw_facts
        WHEN NOT EXISTS (
            SELECT 1
            FROM document_jobs AS dj
            JOIN extraction_batches AS eb ON eb.id = dj.batch_id
            JOIN document_versions AS dv ON dv.id = dj.document_version_id
            JOIN documents AS d ON d.id = dv.document_id
            JOIN job_steps AS import_step
              ON import_step.id = NEW.import_step_id
             AND import_step.document_job_id = dj.id
             AND import_step.kind IN ('merge', 'validate')
            JOIN job_steps AS source_step
              ON source_step.id = NEW.source_step_id
             AND source_step.document_job_id = dj.id
             AND source_step.kind = 'merge'
            WHERE dj.id = NEW.document_job_id
              AND eb.project_id = NEW.project_id
              AND d.project_id = NEW.project_id
              AND d.id = NEW.document_id
              AND dv.id = NEW.document_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'raw fact linkage mismatch');
        END
        """
    ).execute_if(dialect="sqlite"),
)


from app.documents.models import Document, DocumentVersion  # noqa: E402, F401
from app.jobs.models import DocumentJob, JobStep  # noqa: E402, F401
from app.projects.models import Project  # noqa: E402, F401
