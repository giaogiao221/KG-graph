from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, DDL, ForeignKey, Index, JSON, String, UniqueConstraint, event, inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ExportRecord(Base):
    __tablename__ = "exports"
    __table_args__ = (
        UniqueConstraint("project_id", "actor_id", "idempotency_key", name="uq_export_actor_request"),
        UniqueConstraint("storage_key", name="uq_export_storage_key"),
        CheckConstraint("format IN ('tsv','csv','json')", name="ck_export_format"),
        CheckConstraint("status IN ('pending','cleanup_claimed','completed')", name="ck_export_status"),
        CheckConstraint("record_count>=0 AND size_bytes>=0", name="ck_export_sizes"),
        CheckConstraint("length(owner_token)=32 AND length(request_fingerprint)=64", name="ck_export_identity"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 128", name="ck_export_idempotency"),
        CheckConstraint("(status='pending' AND completed_at IS NULL AND sha256 IS NULL AND cleanup_token IS NULL AND cleanup_claimed_at IS NULL) OR (status='cleanup_claimed' AND completed_at IS NULL AND sha256 IS NULL AND length(cleanup_token)=32 AND cleanup_claimed_at IS NOT NULL) OR (status='completed' AND completed_at IS NOT NULL AND length(sha256)=64 AND cleanup_token IS NULL AND cleanup_claimed_at IS NULL)", name="ck_export_completion"),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    filters_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    include_unreviewed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record_count: Mapped[int] = mapped_column(nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_token: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_token: Mapped[str | None] = mapped_column(String(32),nullable=True)
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True,index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    items: Mapped[list["ExportItem"]] = relationship(back_populates="export", order_by="ExportItem.ordinal", passive_deletes=True)


class ExportItem(Base):
    __tablename__ = "export_items"
    __table_args__ = (
        UniqueConstraint("export_id", "ordinal", name="uq_export_item_ordinal"),
        UniqueConstraint("export_id", "fact_version_id", name="uq_export_item_version"),
        UniqueConstraint("export_id", "raw_fact_id", name="uq_export_item_raw"),
        CheckConstraint("ordinal>=0", name="ck_export_item_ordinal"),
        CheckConstraint("source_kind IN ('version','raw')", name="ck_export_item_source"),
        CheckConstraint("(source_kind='version' AND fact_version_id IS NOT NULL AND raw_fact_id IS NULL) OR (source_kind='raw' AND raw_fact_id IS NOT NULL AND fact_version_id IS NULL)", name="ck_export_item_xor"),
        Index("ix_export_items_project_id", "project_id"),
    )
    export_id: Mapped[UUID] = mapped_column(ForeignKey("exports.id", ondelete="RESTRICT"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    fact_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("fact_versions.id", ondelete="RESTRICT"), nullable=True, index=True)
    raw_fact_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_facts.id", ondelete="RESTRICT"), nullable=True, index=True)
    export: Mapped[ExportRecord] = relationship(back_populates="items")


@event.listens_for(ExportRecord, "before_update")
def _record_transition(_m: object, _c: object, target: ExportRecord) -> None:
    state = sa_inspect(target)
    complete_allowed = {"status", "record_count", "size_bytes", "sha256", "completed_at", "updated_at", "version"}
    cleanup_allowed = {"status","cleanup_token","cleanup_claimed_at","updated_at","version"}
    changed = {a.key for a in state.attrs if a.history.has_changes()}
    history = state.attrs.status.history
    old_status = (history.deleted or history.unchanged or [None])[0]
    if not ((old_status == "pending" and target.status == "completed" and changed <= complete_allowed) or (old_status in {"pending","cleanup_claimed"} and target.status=="cleanup_claimed" and changed<=cleanup_allowed)):
        raise ValueError("export history is immutable")


def _immutable(*_args: object) -> None: raise ValueError("export history is immutable")
@event.listens_for(ExportRecord,"before_delete")
def _record_delete(_m,_c,target):
    if target.status!="cleanup_claimed":raise ValueError("export history is immutable")
event.listen(ExportItem, "before_update", _immutable)
@event.listens_for(ExportItem,"before_delete")
def _item_delete(_m,_c,target):
    if target.export.status not in {"pending","cleanup_claimed"}:raise ValueError("export history is immutable")

event.listen(ExportRecord.__table__, "after_create", DDL("""CREATE TRIGGER enforce_export_linkage BEFORE INSERT ON exports WHEN NOT EXISTS(SELECT 1 FROM project_memberships m WHERE m.project_id=NEW.project_id AND m.user_id=NEW.actor_id) AND NOT EXISTS(SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=NEW.actor_id AND r.name='admin') BEGIN SELECT RAISE(ABORT,'export linkage mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ExportRecord.__table__, "after_create", DDL("""CREATE TRIGGER enforce_export_insert_state BEFORE INSERT ON exports WHEN NEW.status!='pending' BEGIN SELECT RAISE(ABORT,'export initial state mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ExportRecord.__table__, "after_create", DDL("""CREATE TRIGGER enforce_export_completion BEFORE UPDATE ON exports WHEN NOT ((OLD.status='pending' AND NEW.status='completed' AND NEW.id=OLD.id AND NEW.created_at=OLD.created_at AND NEW.project_id=OLD.project_id AND NEW.actor_id=OLD.actor_id AND NEW.filters_json=OLD.filters_json AND NEW.format=OLD.format AND NEW.include_unreviewed=OLD.include_unreviewed AND NEW.storage_key=OLD.storage_key AND NEW.owner_token=OLD.owner_token AND NEW.idempotency_key=OLD.idempotency_key AND NEW.request_fingerprint=OLD.request_fingerprint AND NEW.completed_at IS NOT NULL AND length(NEW.sha256)=64 AND NEW.cleanup_token IS NULL AND NEW.cleanup_claimed_at IS NULL AND (SELECT count(*) FROM export_items i WHERE i.export_id=NEW.id)=NEW.record_count AND (NEW.record_count=0 OR ((SELECT min(ordinal) FROM export_items i WHERE i.export_id=NEW.id)=0 AND (SELECT max(ordinal) FROM export_items i WHERE i.export_id=NEW.id)=NEW.record_count-1))) OR (OLD.status IN ('pending','cleanup_claimed') AND NEW.status='cleanup_claimed' AND NEW.id=OLD.id AND NEW.created_at=OLD.created_at AND NEW.project_id=OLD.project_id AND NEW.actor_id=OLD.actor_id AND NEW.filters_json=OLD.filters_json AND NEW.format=OLD.format AND NEW.include_unreviewed=OLD.include_unreviewed AND NEW.record_count=OLD.record_count AND NEW.size_bytes=OLD.size_bytes AND NEW.sha256 IS OLD.sha256 AND NEW.completed_at IS OLD.completed_at AND NEW.storage_key=OLD.storage_key AND NEW.owner_token=OLD.owner_token AND NEW.idempotency_key=OLD.idempotency_key AND NEW.request_fingerprint=OLD.request_fingerprint AND length(NEW.cleanup_token)=32 AND NEW.cleanup_claimed_at IS NOT NULL AND (OLD.status='pending' OR NEW.cleanup_token!=OLD.cleanup_token OR NEW.cleanup_claimed_at>=OLD.cleanup_claimed_at))) BEGIN SELECT RAISE(ABORT,'export completion mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ExportRecord.__table__, "after_create", DDL("""CREATE TRIGGER prevent_export_delete BEFORE DELETE ON exports WHEN OLD.status!='cleanup_claimed' BEGIN SELECT RAISE(ABORT,'export history immutable'); END""").execute_if(dialect="sqlite"))
event.listen(ExportItem.__table__, "after_create", DDL("""CREATE TRIGGER enforce_export_item_linkage BEFORE INSERT ON export_items WHEN NOT EXISTS(SELECT 1 FROM exports e WHERE e.id=NEW.export_id AND e.project_id=NEW.project_id AND e.status='pending') OR (NEW.source_kind='version' AND NOT EXISTS(SELECT 1 FROM fact_versions v WHERE v.id=NEW.fact_version_id AND v.project_id=NEW.project_id)) OR (NEW.source_kind='raw' AND NOT EXISTS(SELECT 1 FROM raw_facts r WHERE r.id=NEW.raw_fact_id AND r.project_id=NEW.project_id)) BEGIN SELECT RAISE(ABORT,'export item linkage mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(ExportItem.__table__,"after_create",DDL("CREATE TRIGGER prevent_export_items_update BEFORE UPDATE ON export_items BEGIN SELECT RAISE(ABORT,'export items immutable'); END").execute_if(dialect="sqlite"))
event.listen(ExportItem.__table__,"after_create",DDL("CREATE TRIGGER prevent_export_items_delete BEFORE DELETE ON export_items WHEN EXISTS(SELECT 1 FROM exports e WHERE e.id=OLD.export_id AND e.status='completed') BEGIN SELECT RAISE(ABORT,'export items immutable'); END").execute_if(dialect="sqlite"))
