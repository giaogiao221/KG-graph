from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from uuid import UUID
from datetime import UTC

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.documents.storage import LocalStorage, StoredObject
from app.facts.schema59 import FIELD
from app.facts.script_schema59 import SCRIPT_SCHEMA59_COLUMNS
from app.exports.service import ExportConflict, _snapshot_statement, claim_pending_export, create_export, finalize_export_record, reconcile_pending_exports
from app.exports.models import ExportItem, ExportRecord
from app.core.settings import get_settings
from app.core.audit import AuditEvent
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from app.auth.dependencies import auth_session
from app.facts.importer import import_facts
from app.facts.models import RawFact
from tests.facts.test_importer import _job, _row, _write
from app.reviews.service import claim_review_task, create_manual_fact, create_review_task, review_fact
from app.projects.models import ProjectMembership


def _manual(session, project, actor, key: str, subject: str):
    value = create_manual_fact(
        session, project.id, actor.id,
        {FIELD["subject"]: subject, FIELD["property"]: "属性", FIELD["value"]: "值", FIELD["evidence_text"]: f"证据-{subject}"},
        idempotency_key=key,
    )
    return value


def _review(session, version, actor, action: str):
    task = claim_review_task(session, create_review_task(session, version.root_id, actor.id).id, actor.id)
    return review_fact(
        session, version.root_id, action, {}, version.version_number, actor.id,
        review_task_id=task.id, expected_lease_version=task.lease_version,
        idempotency_key=f"{version.id}-{action}",
    )


def _read(storage: LocalStorage, key: str) -> bytes:
    with storage.open_read(key) as stream:
        return stream.read()


def test_export_history_list_is_project_scoped_bounded_and_hides_storage(
    client_factory, project, foreign_project
):
    reviewer = client_factory("reviewer")
    created = reviewer.post(
        f"/api/projects/{project.id}/exports",
        json={"format": "tsv", "include_unreviewed": False, "filters": {},
              "idempotency_key": "history-list"},
    )
    assert created.status_code == 201

    listed = reviewer.get(f"/api/projects/{project.id}/exports?limit=25")
    wrong = reviewer.get(f"/api/projects/{foreign_project.id}/exports")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == created.json()["id"]
    assert "storage" not in listed.text.lower()
    assert wrong.status_code in (403, 404)
    assert reviewer.get(f"/api/projects/{project.id}/exports?limit=501").status_code == 422


def test_default_tsv_exports_only_current_accepted_versions_in_canonical_order(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path / "objects")
    with app_session_factory() as session:
        approved = _review(session, _manual(session, project, users["admin"], "a", "乙"), users["admin"], "approve")
        rejected = _review(session, _manual(session, project, users["admin"], "b", "甲"), users["admin"], "reject")
        deleted = _review(session, _review(session, _manual(session, project, users["admin"], "c", "丙"), users["admin"], "approve"), users["admin"], "delete")
        session.commit()
        exported = create_export(session, project.id, {}, "tsv", False, users["reviewer"].id, "same", storage)
        session.commit()
        items = list(session.scalars(select(ExportItem).where(ExportItem.export_id == exported.id).order_by(ExportItem.ordinal)))
        assert [item.fact_version_id for item in items] == [approved.id]
        assert rejected.id not in [item.fact_version_id for item in items] and deleted.id not in [item.fact_version_id for item in items]
    payload = _read(storage, exported.storage_key).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(payload), delimiter="\t"))
    assert tuple(rows[0]) == SCRIPT_SCHEMA59_COLUMNS
    assert [row["主体名称"] for row in rows] == ["乙"]
    assert exported.sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_include_unreviewed_adds_raw_facts_with_no_versions_and_raw_provenance(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path / "objects")
    with app_session_factory() as session:
        steps = _job(session, project.id, users["operator"].id)
        row = _row(steps); row[FIELD["subject"]] = "零版本"
        import_facts(session, steps["validate"].id, _write(tmp_path / "raw-only.tsv", [row]))
        raw = session.scalar(select(RawFact))
        # A root may exist without any FactVersion and must still be exported.
        from app.reviews.service import ensure_raw_root
        ensure_raw_root(session, raw.id, users["admin"].id)
        session.commit()
        exported = create_export(session, project.id, {}, "json", True, users["admin"].id, "raw-zero", storage)
        session.commit()
        item = session.scalar(select(ExportItem).where(ExportItem.export_id == exported.id))
        assert item.source_kind == "raw" and item.raw_fact_id == raw.id and item.fact_version_id is None
    record = json.loads(_read(storage, exported.storage_key))[0]
    assert record["provenance"] == {
        "source": "raw", "raw_fact_id": str(raw.id), "version_id": None,
        "version_number": None, "action": None, "actor_id": None,
        "timestamp": (raw.created_at if raw.created_at.tzinfo else raw.created_at.replace(tzinfo=UTC)).isoformat(),
    }


def test_unreviewed_snapshot_is_one_union_all_statement(project) -> None:
    statement = _snapshot_statement(project.id, {}, True)
    sql = str(statement).upper()
    assert "UNION ALL" in sql
    assert sql.count("ORDER BY") == 1


def test_pending_registry_reconcile_cleans_owned_storage_but_never_completed(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4
    from app.documents.storage import ExportOwnership
    storage=LocalStorage(tmp_path/"objects")
    with app_session_factory() as session:
        pending_id=uuid4();token=uuid4().hex;key=storage.export_storage_key(token,".tsv")
        pending=ExportRecord(id=pending_id,project_id=project.id,actor_id=users["admin"].id,filters_json={},format="tsv",include_unreviewed=False,record_count=0,size_bytes=0,sha256=None,storage_key=key,owner_token=token,status="pending",completed_at=None,idempotency_key="orphan",request_fingerprint="1"*64,created_at=datetime.now(UTC)-timedelta(days=2))
        session.add(pending);session.commit()
        ownership=ExportOwnership(pending_id,token,key);storage.create_export_ownership(ownership);storage.save_owned_export(ownership,io.BytesIO(b"partial"))
        reconcile_pending_exports(session,storage,datetime.now(UTC)-timedelta(days=1))
        assert session.get(ExportRecord,pending_id) is None
        assert not (storage.root/"exports"/token).exists()
        complete=create_export(session,project.id,{},"tsv",False,users["admin"].id,"complete-safe",storage);session.commit()
        reconcile_pending_exports(session,storage,datetime.now(UTC)+timedelta(days=1))
        assert session.get(ExportRecord,complete.id).status=="completed"


def test_export_model_has_no_unbound_manifest_hash() -> None:
    assert "manifest_sha256" not in ExportRecord.__table__.c


def test_cleanup_claim_fences_finalize_and_only_one_worker_owns_token(
    app_session_factory,project,users
) -> None:
    from datetime import UTC,datetime,timedelta
    from uuid import uuid4
    with app_session_factory() as setup:
        value=ExportRecord(project_id=project.id,actor_id=users["admin"].id,filters_json={},format="tsv",include_unreviewed=False,record_count=0,size_bytes=0,sha256=None,storage_key="exports/"+"b"*32+"/payload.tsv",owner_token="b"*32,status="pending",completed_at=None,idempotency_key="fence",request_fingerprint="2"*64,created_at=datetime.now(UTC)-timedelta(days=2))
        setup.add(value);setup.commit();export_id=value.id
    first,second=app_session_factory(),app_session_factory()
    try:
        token1,token2=uuid4().hex,uuid4().hex;cutoff=datetime.now(UTC)-timedelta(days=1)
        assert claim_pending_export(first,export_id,cutoff,token1)
        assert not claim_pending_export(second,export_id,cutoff,token2)
        assert not finalize_export_record(second,export_id,0,0,"0"*64,datetime.now(UTC))
        claimed=second.get(ExportRecord,export_id)
        assert claimed.status=="cleanup_claimed" and claimed.cleanup_token==token1
    finally:first.close();second.close()


def test_cleanup_claim_commit_unknown_is_reconciled_from_durable_token(
    app_session_factory,project,users
) -> None:
    from datetime import UTC,datetime,timedelta
    from uuid import uuid4
    class CommitUnknownSession(Session):
        def commit(self):
            super().commit()
            raise RuntimeError("cleanup claim commit outcome unknown")
    with app_session_factory() as setup:
        value=ExportRecord(project_id=project.id,actor_id=users["admin"].id,filters_json={},format="tsv",include_unreviewed=False,record_count=0,size_bytes=0,sha256=None,storage_key="exports/"+"d"*32+"/payload.tsv",owner_token="d"*32,status="pending",completed_at=None,idempotency_key="claim-unknown",request_fingerprint="4"*64,created_at=datetime.now(UTC)-timedelta(days=2))
        setup.add(value);setup.commit();export_id=value.id
    factory=sessionmaker(bind=app_session_factory.kw["bind"],class_=CommitUnknownSession,expire_on_commit=False)
    token=uuid4().hex
    with factory() as claimant:
        assert claim_pending_export(claimant,export_id,datetime.now(UTC)-timedelta(days=1),token)
    with app_session_factory() as verify:
        durable=verify.get(ExportRecord,export_id)
        assert durable.status=="cleanup_claimed" and durable.cleanup_token==token


def test_completed_export_cannot_be_claimed_for_cleanup(app_session_factory,project,users,tmp_path):
    from datetime import UTC,datetime,timedelta
    storage=LocalStorage(tmp_path/"objects")
    with app_session_factory() as session:
        value=create_export(session,project.id,{},"tsv",False,users["admin"].id,"complete-fence",storage);session.commit()
        assert not claim_pending_export(session,value.id,datetime.now(UTC)+timedelta(days=1),"c"*32)
        ownership=__import__("app.documents.storage",fromlist=["ExportOwnership"]).ExportOwnership(value.id,value.owner_token,value.storage_key)
        with storage.open_owned_export(ownership) as stream:
            assert stream.read() is not None


def test_spreadsheet_exports_escape_formula_cells_but_json_is_lossless(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path / "objects")
    with app_session_factory() as session:
        _review(session, _manual(session, project, users["admin"], "formula", "  =2+2"), users["admin"], "approve")
        session.commit()
        csv_export = create_export(session, project.id, {}, "csv", False, users["admin"].id, "formula-csv", storage); session.commit()
        json_export = create_export(session, project.id, {}, "json", False, users["admin"].id, "formula-json", storage); session.commit()
    csv_row = next(csv.DictReader(io.StringIO(_read(storage, csv_export.storage_key).decode("utf-8-sig"))))
    assert csv_row["主体名称"] == "'  =2+2"
    assert json.loads(_read(storage, json_export.storage_key))[0]["row"]["主体名称"] == "  =2+2"


def test_export_hash_and_order_are_deterministic_and_record_is_immutable(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path / "objects")
    with app_session_factory() as session:
        for key, subject in (("z", "末"), ("a", "首")):
            _review(session, _manual(session, project, users["admin"], key, subject), users["admin"], "approve")
        session.commit()
        first = create_export(session, project.id, {}, "tsv", False, users["reviewer"].id, "det-1", storage)
        session.commit()
        second = create_export(session, project.id, {}, "tsv", False, users["reviewer"].id, "det-2", storage)
        session.commit()
        assert first.sha256 == second.sha256
        export_id = first.id
    with app_session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(ExportRecord).where(ExportRecord.id == export_id).values(record_count=99)); session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(delete(ExportRecord).where(ExportRecord.id == export_id)); session.commit()


def test_export_row_limit_and_digest_failure_cleanup(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXTRACTION_MAX_EXPORT_ROWS", "1"); get_settings.cache_clear()
    storage = LocalStorage(tmp_path / "objects")
    with app_session_factory() as session:
        for key in ("one", "two"):
            _review(session, _manual(session, project, users["admin"], key, key), users["admin"], "approve")
        session.commit()
        from app.exports.service import ExportLimit
        with pytest.raises(ExportLimit):
            create_export(session, project.id, {}, "tsv", False, users["reviewer"].id, "limited", storage)
        events=list(session.scalars(select(AuditEvent).where(AuditEvent.request_id=="limited").order_by(AuditEvent.created_at,AuditEvent.id)))
        assert [(e.action,e.outcome) for e in events]==[("export.request","requested"),("export.failure","failure")]
    assert not list((tmp_path / "objects").glob("*.tsv"))
    monkeypatch.setenv("EXTRACTION_MAX_EXPORT_ROWS", "10"); get_settings.cache_clear()

    class CorruptStorage(LocalStorage):
        def save_owned_export(self, ownership, stream):
            stored = super().save_owned_export(ownership, stream)
            return StoredObject(stored.storage_key, "0" * 64, stored.size_bytes)

    corrupt = CorruptStorage(tmp_path / "corrupt")
    with app_session_factory() as session:
        with pytest.raises(Exception, match="校验失败"):
            create_export(session, project.id, {}, "tsv", False, users["reviewer"].id, "corrupt", corrupt)
    assert not list((tmp_path / "corrupt").glob("*.tsv"))
    get_settings.cache_clear()


def test_csv_bom_quoting_json_provenance_and_deterministic_retry(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path / "objects")
    with app_session_factory() as session:
        version = _review(session, _manual(session, project, users["admin"], "csv", 'a,"b"'), users["admin"], "approve")
        session.commit()
        first = create_export(session, project.id, {"action": "approve"}, "csv", False, users["reviewer"].id, "csv-key", storage)
        session.commit()
        second = create_export(session, project.id, {"action": "approve"}, "csv", False, users["reviewer"].id, "csv-key", storage)
        assert first.id == second.id
        with pytest.raises(ExportConflict):
            create_export(session, project.id, {}, "json", False, users["reviewer"].id, "csv-key", storage)
        json_export = create_export(session, project.id, {}, "json", False, users["reviewer"].id, "json-key", storage)
        session.commit()
    csv_payload = _read(storage, first.storage_key)
    assert csv_payload.startswith(b"\xef\xbb\xbf") and b'"a,""b"""' in csv_payload
    record = json.loads(_read(storage, json_export.storage_key))[0]
    assert record["row"]["主体名称"] == 'a,"b"'
    assert record["provenance"] == {
        "source": "version", "root_id": str(version.root_id), "version_id": str(version.id),
        "version_number": version.version_number, "action": "approve",
        "actor_id": str(version.actor_id), "timestamp": version.created_at.isoformat(),
    }
    assert "storage_key" not in json.dumps(record)


def test_unreviewed_policy_limits_and_project_safe_download(
    app_session_factory, client_factory, project, foreign_project, users
) -> None:
    with app_session_factory.begin() as session:
        session.add(ProjectMembership(project_id=project.id, user_id=users["viewer"].id, role="member"))
    viewer = client_factory("viewer")
    denied = viewer.post(
        f"/api/projects/{project.id}/exports",
        json={"format": "tsv", "include_unreviewed": True, "idempotency_key": "v"},
    )
    assert denied.status_code == 403
    with app_session_factory() as session:
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.request_id == "v")))
        assert [(event.action, event.outcome) for event in events] == [("export.request", "requested"), ("export.denied", "denied")]
    admin = client_factory("admin")
    made = admin.post(
        f"/api/projects/{project.id}/exports",
        json={"format": "tsv", "include_unreviewed": True, "idempotency_key": "a"},
    )
    assert made.status_code == 201
    export_id = made.json()["id"]
    assert viewer.get(f"/api/projects/{foreign_project.id}/exports/{export_id}/download").status_code in {403, 404}
    response = viewer.get(f"/api/projects/{project.id}/exports/{export_id}/download")
    assert response.status_code == 200
    assert "storage_key" not in made.json()
    with app_session_factory() as session:
        events=list(session.scalars(select(AuditEvent).where(AuditEvent.request_id==export_id)))
        assert any((e.action,e.outcome)==("export.download","success") for e in events)


def test_nonmember_export_denial_has_request_and_denied_audit(
    app_session_factory, client_factory, project
) -> None:
    response=client_factory("outsider").post(f"/api/projects/{project.id}/exports",json={"format":"tsv","idempotency_key":"outsider-denied"})
    assert response.status_code==403
    with app_session_factory() as session:
        events=list(session.scalars(select(AuditEvent).where(AuditEvent.request_id=="outsider-denied").order_by(AuditEvent.created_at,AuditEvent.id)))
        assert [(e.action,e.outcome) for e in events]==[("export.request","requested"),("export.denied","denied")]


def test_commit_unknown_retry_does_not_create_failure_claim_or_second_artifact(
    app_session_factory, client_factory, project
) -> None:
    class CommitUnknownSession(Session):
        raised = False
        def commit(self):
            super().commit()
            if not self.raised:
                self.raised = True
                raise RuntimeError("commit outcome unknown")

    factory = sessionmaker(bind=app_session_factory.kw["bind"], class_=CommitUnknownSession, expire_on_commit=False)
    def dependency():
        with factory() as session: yield session
    client_factory.app.dependency_overrides[auth_session] = dependency
    response = client_factory("admin").post(
        f"/api/projects/{project.id}/exports",
        json={"format": "tsv", "idempotency_key": "commit-unknown"},
    )
    assert response.status_code == 201
    with app_session_factory() as session:
        exports = list(session.scalars(select(ExportRecord).where(ExportRecord.idempotency_key == "commit-unknown")))
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.request_id == "commit-unknown")))
        assert len(exports) == 1
        assert {event.action for event in events} == {"export.request", "export.success"}


def test_download_rejects_payload_whose_digest_no_longer_matches_record(
    app_session_factory, client_factory, project
) -> None:
    admin = client_factory("admin")
    made = admin.post(
        f"/api/projects/{project.id}/exports",
        json={"format": "tsv", "idempotency_key": "tamper"},
    )
    assert made.status_code == 201
    with app_session_factory() as session:
        value = session.scalar(select(ExportRecord).where(ExportRecord.id == UUID(made.json()["id"])))
        path = client_factory.storage.root / value.storage_key
    path.write_bytes(b"tampered")
    assert admin.get(f"/api/projects/{project.id}/exports/{made.json()['id']}/download").status_code == 404
    with app_session_factory() as session:
        events=list(session.scalars(select(AuditEvent).where(AuditEvent.request_id==made.json()["id"])))
        assert [(e.action,e.outcome) for e in events]==[("export.download","failure")]
