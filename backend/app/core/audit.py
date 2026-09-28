from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, DDL, ForeignKey, JSON, String, event
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base


_SAFE_METADATA_KEYS = frozenset({
    "export_id", "format", "include_unreviewed", "record_count", "size_bytes",
    "sha256", "reason", "filter_keys", "target_type",
})


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(action) BETWEEN 1 AND 100 AND outcome IN ('requested','success','failure','denied')",name="ck_audit_base"),
        CheckConstraint("length(request_id) BETWEEN 1 AND 128", name="ck_audit_request_id"),
    )

    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)


def _immutable(*_args: object) -> None:
    raise ValueError("audit history is immutable")


event.listen(AuditEvent, "before_update", _immutable)
event.listen(AuditEvent, "before_delete", _immutable)
for operation in ("UPDATE", "DELETE"):
    event.listen(
        AuditEvent.__table__, "after_create",
        DDL(f"CREATE TRIGGER prevent_audit_events_{operation.lower()} BEFORE {operation} ON audit_events BEGIN SELECT RAISE(ABORT,'audit history immutable'); END").execute_if(dialect="sqlite"),
    )
event.listen(AuditEvent.__table__,"after_create",DDL("""CREATE TRIGGER enforce_export_audit_combo BEFORE INSERT ON audit_events WHEN NEW.action LIKE 'export.%%' AND NOT ((NEW.action='export.request' AND NEW.outcome='requested' AND NEW.target_id IS NULL) OR (NEW.action='export.success' AND NEW.outcome='success' AND NEW.target_id IS NOT NULL) OR (NEW.action='export.failure' AND NEW.outcome='failure' AND NEW.target_id IS NULL) OR (NEW.action='export.denied' AND NEW.outcome='denied' AND NEW.target_id IS NULL) OR (NEW.action='export.download' AND NEW.outcome IN ('success','failure') AND NEW.target_id IS NOT NULL)) BEGIN SELECT RAISE(ABORT,'audit event action mismatch'); END""").execute_if(dialect="sqlite"))
event.listen(AuditEvent.__table__, "after_create", DDL("""CREATE TRIGGER enforce_audit_event_target BEFORE INSERT ON audit_events WHEN (NEW.action='export.success' OR (NEW.action='export.download' AND NEW.outcome='success')) AND NOT EXISTS(SELECT 1 FROM exports e WHERE e.id=NEW.target_id AND e.project_id=NEW.project_id AND e.status='completed') OR (NEW.action='export.download' AND NEW.outcome='failure' AND NOT EXISTS(SELECT 1 FROM exports e WHERE e.id=NEW.target_id AND e.project_id=NEW.project_id)) BEGIN SELECT RAISE(ABORT,'audit event target mismatch'); END""").execute_if(dialect="sqlite"))


def _safe_metadata(value: dict[str, object] | None) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, item in (value or {}).items():
        if key not in _SAFE_METADATA_KEYS:
            continue
        if isinstance(item, bool) or item is None or isinstance(item, int):
            safe[key] = item
        elif isinstance(item, str) and len(item) <= 256 and "\x00" not in item:
            safe[key] = item
        elif isinstance(item, list) and len(item) <= 50 and all(isinstance(v, str) and len(v) <= 64 for v in item):
            safe[key] = list(item)
    return safe


def audit_event(
    session: Session, *, actor_id: UUID, project_id: UUID | None, action: str,
    target_type: str, target_id: UUID | None, request_id: str, outcome: str,
    metadata: dict[str, object] | None = None,
) -> AuditEvent:
    if not (1 <= len(action) <= 100 and 1 <= len(target_type) <= 50 and 1 <= len(request_id) <= 128):
        raise ValueError("invalid audit event")
    value = AuditEvent(
        actor_id=actor_id, project_id=project_id, action=action,
        target_type=target_type, target_id=target_id, request_id=request_id,
        outcome=outcome, metadata_json=_safe_metadata(metadata),
    )
    session.add(value)
    return value
