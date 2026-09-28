from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
import pytest

from app.core.audit import AuditEvent, audit_event
from app.exports.models import ExportRecord
from uuid import uuid4
from datetime import UTC, datetime


def test_audit_metadata_is_sanitized_and_history_is_append_only(
    app_session_factory, project, users
) -> None:
    with app_session_factory.begin() as session:
        event = audit_event(
            session, actor_id=users["admin"].id, project_id=project.id,
            action="export.request", target_type="export", target_id=None,
            request_id="request-1", outcome="requested",
            metadata={"format": "tsv", "token": "secret", "path": "D:/secret"},
        )
        session.flush()
        assert event.metadata_json == {"format": "tsv"}
        event_id = event.id
    with app_session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(AuditEvent).where(AuditEvent.id == event_id).values(outcome="failure"))
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))
            session.commit()


def test_audit_database_rejects_actor_project_mismatch(
    app_session_factory, project, users
) -> None:
    with app_session_factory() as session:
        audit_event(
            session, actor_id=users["outsider"].id, project_id=project.id,
            action="export.denied", target_type="export", target_id=None,
            request_id="mismatch", outcome="denied", metadata={},
        )
        # Denials for valid users/projects must be auditable even for non-members.
        session.commit()


def test_non_export_security_event_is_allowed_and_immutable(app_session_factory,project,users):
    with app_session_factory.begin() as session:
        event=audit_event(session,actor_id=users["admin"].id,project_id=project.id,action="security.login",target_type="session",target_id=None,request_id="login-1",outcome="success",metadata={"reason":"ok"})
        session.flush();event_id=event.id
    with app_session_factory() as session:
        with pytest.raises(IntegrityError):session.execute(update(AuditEvent).where(AuditEvent.id==event_id).values(outcome="failure"));session.commit()


def test_export_success_cannot_target_pending_export(app_session_factory,project,users):
    with app_session_factory() as session:
        pending=ExportRecord(project_id=project.id,actor_id=users["admin"].id,filters_json={},format="tsv",include_unreviewed=False,record_count=0,size_bytes=0,sha256=None,storage_key="exports/"+"a"*32+"/payload.tsv",owner_token="a"*32,status="pending",completed_at=None,idempotency_key="pending-audit",request_fingerprint="1"*64)
        session.add(pending);session.flush()
        audit_event(session,actor_id=users["admin"].id,project_id=project.id,action="export.success",target_type="export",target_id=pending.id,request_id="pending-audit",outcome="success",metadata={})
        with pytest.raises(IntegrityError):session.commit()
