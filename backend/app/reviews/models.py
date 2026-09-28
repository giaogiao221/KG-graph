from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, DDL, ForeignKey, Index, JSON, String, UniqueConstraint, event, inspect as sa_inspect, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


ACTIONS = ("approve", "modify_approve", "reject", "dispute", "manual_create", "delete", "publish")


class ReviewFactRoot(Base):
    __tablename__ = "review_fact_roots"
    __table_args__ = (
        UniqueConstraint("raw_fact_id", name="uq_review_root_raw_fact"),
        CheckConstraint("source IN ('raw','manual')", name="ck_review_root_source"),
        CheckConstraint("(source='raw' AND raw_fact_id IS NOT NULL) OR (source='manual' AND raw_fact_id IS NULL)", name="ck_review_root_source_link"),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    raw_fact_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_facts.id", ondelete="RESTRICT"), nullable=True, index=True)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True)
    document_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    raw_fact: Mapped["RawFact | None"] = relationship()
    versions: Mapped[list["FactVersion"]] = relationship(back_populates="root")


class FactVersion(Base):
    __tablename__ = "fact_versions"
    __table_args__ = (
        UniqueConstraint("root_id", "version_number", name="uq_fact_version_number"),
        UniqueConstraint("root_id", "idempotency_key", name="uq_fact_version_idempotency"),
        UniqueConstraint("project_id", "actor_id", "idempotency_key", name="uq_fact_version_actor_request"),
        CheckConstraint("version_number >= 1", name="ck_fact_version_number"),
        CheckConstraint("action IN ('approve','modify_approve','reject','dispute','manual_create','delete','publish')", name="ck_fact_version_action"),
        CheckConstraint("length(idempotency_key)>0 AND length(request_fingerprint)=64", name="ck_fact_version_request"),
        CheckConstraint("(action='manual_create' AND review_task_id IS NULL AND review_lease_version IS NULL) OR (action!='manual_create' AND review_task_id IS NOT NULL AND review_lease_version IS NOT NULL)", name="ck_fact_version_review_task"),
    )
    root_id: Mapped[UUID] = mapped_column(ForeignKey("review_fact_roots.id", ondelete="RESTRICT"), nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    row_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    patch_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    audit_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    is_tombstone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    review_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("review_tasks.id", ondelete="RESTRICT"), nullable=True, index=True)
    review_lease_version: Mapped[int | None] = mapped_column(nullable=True)
    root: Mapped[ReviewFactRoot] = relationship(back_populates="versions")

    @property
    def raw_fact_id(self) -> UUID | None:
        return self.root.raw_fact_id

    @property
    def editable_values(self) -> dict[str, str]:
        from app.facts.schema59 import FIELD
        return {
            "subject": self.row_json.get(FIELD["subject"], ""),
            "property": self.row_json.get(FIELD["property"], ""),
            "value": self.row_json.get(FIELD["value"], ""),
            "unit": self.row_json.get(FIELD["unit"], ""),
            "condition": self.row_json.get(FIELD["condition"], ""),
            "evidence": self.row_json.get(FIELD["evidence_text"], ""),
        }


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('pending','claimed','completed')", name="ck_review_task_status"),
        CheckConstraint("lease_version >= 0", name="ck_review_task_lease_version"),
        CheckConstraint("(status='pending' AND reviewer_id IS NULL AND lease_expires_at IS NULL) OR (status='claimed' AND reviewer_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status='completed' AND reviewer_id IS NOT NULL AND lease_expires_at IS NULL)", name="ck_review_task_lease_state"),
        Index("uq_review_task_active_root","root_id",unique=True,sqlite_where=text("status != 'completed'"),postgresql_where=text("status != 'completed'")),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    root_id: Mapped[UUID] = mapped_column(ForeignKey("review_fact_roots.id", ondelete="RESTRICT"), nullable=False, index=True)
    reviewer_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lease_version: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    root: Mapped[ReviewFactRoot] = relationship()

    @property
    def raw_fact_id(self) -> UUID | None:
        return self.root.raw_fact_id

    @property
    def batch_id(self) -> UUID | None:
        return self.root.raw_fact.document_job.batch_id if self.root.raw_fact is not None else None

    @property
    def subject(self) -> str:
        return self.root.raw_fact.subject if self.root.raw_fact is not None else ""

    @property
    def value(self) -> str:
        return self.root.raw_fact.value if self.root.raw_fact is not None else ""

    @property
    def property(self) -> str:
        return self.root.raw_fact.property if self.root.raw_fact is not None else ""


@event.listens_for(FactVersion, "before_update")
@event.listens_for(FactVersion, "before_delete")
def _immutable(*_args: object) -> None:
    raise ValueError("review history is immutable")


@event.listens_for(ReviewFactRoot, "before_update")
def _root_update(_m: object, _c: object, target: ReviewFactRoot) -> None:
    state = sa_inspect(target)
    if any(state.attrs[column.key].history.has_changes() for column in state.mapper.columns):
        raise ValueError("review history is immutable")


@event.listens_for(ReviewFactRoot, "before_delete")
def _root_delete(*_args: object) -> None:
    raise ValueError("review history is immutable")


@event.listens_for(ReviewTask, "before_update")
def _task_linkage(_m: object, _c: object, target: ReviewTask) -> None:
    state = sa_inspect(target)
    if state.attrs.project_id.history.has_changes() or state.attrs.root_id.history.has_changes():
        raise ValueError("review task linkage is immutable")


@event.listens_for(ReviewTask,"before_delete")
def _task_delete(*_args:object)->None:raise ValueError("review tasks are immutable")


for table in (ReviewFactRoot.__table__, FactVersion.__table__):
    for operation in ("UPDATE", "DELETE"):
        event.listen(table, "after_create", DDL(f"CREATE TRIGGER prevent_{table.name}_{operation.lower()} BEFORE {operation} ON {table.name} BEGIN SELECT RAISE(ABORT,'review history is immutable'); END").execute_if(dialect="sqlite"))

event.listen(ReviewFactRoot.__table__, "after_create", DDL("""CREATE TRIGGER enforce_review_root_linkage BEFORE INSERT ON review_fact_roots WHEN (NEW.source='raw' AND NOT EXISTS(SELECT 1 FROM raw_facts f WHERE f.id=NEW.raw_fact_id AND f.project_id=NEW.project_id AND f.document_id=NEW.document_id AND f.document_version_id=NEW.document_version_id)) OR (NEW.document_version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=NEW.document_version_id AND d.id=NEW.document_id AND d.project_id=NEW.project_id)) OR (NEW.document_version_id IS NULL AND NEW.document_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM documents d WHERE d.id=NEW.document_id AND d.project_id=NEW.project_id)) BEGIN SELECT RAISE(ABORT,'review root linkage mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(FactVersion.__table__, "after_create", DDL("""CREATE TRIGGER enforce_fact_version_linkage BEFORE INSERT ON fact_versions WHEN NOT EXISTS(SELECT 1 FROM review_fact_roots r WHERE r.id=NEW.root_id AND r.project_id=NEW.project_id) OR NEW.version_number!=COALESCE((SELECT MAX(v.version_number) FROM fact_versions v WHERE v.root_id=NEW.root_id),0)+1 BEGIN SELECT RAISE(ABORT,'fact version linkage mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(FactVersion.__table__, "after_create", DDL("""CREATE TRIGGER enforce_fact_version_task BEFORE INSERT ON fact_versions WHEN (NEW.action!='manual_create' AND NOT EXISTS(SELECT 1 FROM review_tasks t WHERE t.id=NEW.review_task_id AND t.root_id=NEW.root_id AND t.project_id=NEW.project_id AND t.reviewer_id=NEW.actor_id AND t.status='claimed' AND t.lease_version=NEW.review_lease_version AND t.lease_expires_at>CURRENT_TIMESTAMP)) OR (NEW.action='manual_create' AND NOT EXISTS(SELECT 1 FROM review_fact_roots r WHERE r.id=NEW.root_id AND r.source='manual' AND r.created_by_id=NEW.actor_id AND NEW.version_number=1 AND NEW.review_task_id IS NULL AND NEW.review_lease_version IS NULL)) BEGIN SELECT RAISE(ABORT,'fact version task mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ReviewTask.__table__, "after_create", DDL("""CREATE TRIGGER enforce_review_task_linkage BEFORE INSERT ON review_tasks WHEN NOT EXISTS(SELECT 1 FROM review_fact_roots r WHERE r.id=NEW.root_id AND r.project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'review task linkage mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ReviewTask.__table__, "after_create", DDL("""CREATE TRIGGER prevent_review_task_linkage_update BEFORE UPDATE ON review_tasks WHEN NEW.project_id!=OLD.project_id OR NEW.root_id!=OLD.root_id BEGIN SELECT RAISE(ABORT,'review task linkage immutable'); END""").execute_if(dialect="sqlite"))
event.listen(ReviewTask.__table__, "after_create", DDL("""CREATE TRIGGER enforce_review_task_state BEFORE UPDATE ON review_tasks WHEN NOT ((OLD.status='pending' AND NEW.status='claimed' AND NEW.lease_version=OLD.lease_version+1 AND NEW.lease_expires_at>CURRENT_TIMESTAMP) OR (OLD.status='claimed' AND NEW.status='claimed' AND NEW.lease_version=OLD.lease_version+1 AND ((NEW.reviewer_id=OLD.reviewer_id AND OLD.lease_expires_at>CURRENT_TIMESTAMP AND NEW.lease_expires_at>OLD.lease_expires_at) OR (OLD.lease_expires_at<=CURRENT_TIMESTAMP AND NEW.lease_expires_at>CURRENT_TIMESTAMP))) OR (OLD.status='claimed' AND NEW.status='pending' AND NEW.lease_version=OLD.lease_version+1) OR (OLD.status='claimed' AND OLD.lease_expires_at>CURRENT_TIMESTAMP AND NEW.status='completed' AND NEW.lease_version=OLD.lease_version+1 AND NEW.reviewer_id=OLD.reviewer_id AND EXISTS(SELECT 1 FROM fact_versions v WHERE v.review_task_id=OLD.id AND v.review_lease_version=OLD.lease_version AND v.actor_id=OLD.reviewer_id AND v.root_id=OLD.root_id))) BEGIN SELECT RAISE(ABORT,'review task state mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ReviewTask.__table__,"after_create",DDL("""CREATE TRIGGER enforce_review_task_insert BEFORE INSERT ON review_tasks WHEN NEW.status!='pending' OR NEW.lease_version!=0 OR NEW.reviewer_id IS NOT NULL OR NEW.lease_expires_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'review task initial state mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ReviewTask.__table__,"after_create",DDL("""CREATE TRIGGER prevent_review_task_delete BEFORE DELETE ON review_tasks BEGIN SELECT RAISE(ABORT,'review tasks immutable'); END""").execute_if(dialect="sqlite"))

from app.facts.models import RawFact  # noqa: E402,F401
