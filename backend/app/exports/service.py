from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, JSON, String, Uuid, and_, cast, delete, exists, func, insert, literal, null, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.models import Role, User
from app.core.audit import AuditEvent, audit_event
from app.core.settings import get_settings
from app.documents.storage import ExportOwnership, Storage, StorageCompensationError, UploadTooLargeError
from app.exports.models import ExportItem, ExportRecord
from app.facts.models import RawFact
from app.facts.script_schema59 import SCRIPT_SCHEMA59_COLUMNS, platform_to_script_row
from app.projects.models import Project, ProjectMembership
from app.reviews.models import FactVersion, ReviewFactRoot


class ExportError(ValueError): pass
class ExportDenied(ExportError): pass
class ExportConflict(ExportError): pass
class ExportLimit(ExportError): pass
class CleanupClaimConflict(ExportConflict): pass


_ACCEPTED = frozenset({"approve", "modify_approve", "publish"})
_ACTIONS = frozenset({"approve", "modify_approve", "reject", "dispute", "manual_create", "publish"})


def _audit_commit(session: Session, *, actor_id: UUID, project_id: UUID, action: str,
                  request_id: str, outcome: str, target_id: UUID | None = None,
                  metadata: dict[str, object] | None = None) -> None:
    audit_event(session, actor_id=actor_id, project_id=project_id, action=action,
                target_type="export", target_id=target_id, request_id=request_id,
                outcome=outcome, metadata=metadata)
    session.commit()


def _permissions(session: Session, actor_id: UUID, project_id: UUID) -> set[str]:
    actor = session.scalar(select(User).options(selectinload(User.roles).selectinload(Role.permissions)).where(User.id == actor_id))
    if actor is None or actor.is_disabled or session.get(Project, project_id) is None:
        raise ExportDenied("无导出权限")
    admin = any(role.name == "admin" for role in actor.roles)
    member = session.scalar(select(ProjectMembership.id).where(ProjectMembership.project_id == project_id, ProjectMembership.user_id == actor_id))
    granted = {p.code for role in actor.roles for p in role.permissions}
    if (not admin and member is None) or "exports:create" not in granted: raise ExportDenied("无导出权限")
    return granted


def _normalize(filters: dict[str, object]) -> dict[str, str]:
    if not isinstance(filters, dict) or set(filters) - {"action", "document_id"}: raise ExportError("导出筛选条件无效")
    result: dict[str, str] = {}
    if (action := filters.get("action")) is not None:
        if action not in _ACTIONS: raise ExportError("导出筛选条件无效")
        result["action"] = str(action)
    if (document := filters.get("document_id")) is not None:
        try: result["document_id"] = str(UUID(str(document)))
        except ValueError: raise ExportError("导出筛选条件无效") from None
    return dict(sorted(result.items()))


def _fingerprint(project_id: UUID, actor_id: UUID, filters: dict[str, str], format: str, include: bool) -> str:
    raw = json.dumps({"project_id": str(project_id), "actor_id": str(actor_id), "filters": filters, "format": format, "include_unreviewed": include}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _snapshot_statement(project_id: UUID, filters: dict[str, str], include: bool):
    latest = select(FactVersion.root_id, func.max(FactVersion.version_number).label("n")).where(FactVersion.project_id == project_id).group_by(FactVersion.root_id).subquery()
    conditions = [FactVersion.project_id == project_id, FactVersion.is_tombstone.is_(False)]
    if not include: conditions.append(FactVersion.action.in_(_ACCEPTED))
    if "action" in filters: conditions.append(FactVersion.action == filters["action"])
    if "document_id" in filters: conditions.append(ReviewFactRoot.document_id == UUID(filters["document_id"]))
    versions=select(literal(0).label("source_order"),literal("version").label("source_kind"),FactVersion.id.label("source_id"),FactVersion.row_json.label("row_json"),FactVersion.root_id.label("root_id"),FactVersion.version_number.label("version_number"),FactVersion.action.label("action"),FactVersion.actor_id.label("actor_id"),FactVersion.created_at.label("timestamp")).join(latest,and_(FactVersion.root_id==latest.c.root_id,FactVersion.version_number==latest.c.n)).join(ReviewFactRoot).where(*conditions)
    has_version = exists(select(FactVersion.id).join(ReviewFactRoot, ReviewFactRoot.id == FactVersion.root_id).where(ReviewFactRoot.raw_fact_id == RawFact.id))
    raw_conditions=[RawFact.project_id==project_id,~has_version,literal(include)]
    if "document_id" in filters:raw_conditions.append(RawFact.document_id==UUID(filters["document_id"]))
    if "action" in filters:raw_conditions.append(literal(False))
    raws=select(literal(1).label("source_order"),literal("raw").label("source_kind"),RawFact.id.label("source_id"),RawFact.row_json.label("row_json"),cast(null(),Uuid).label("root_id"),cast(null(),Integer).label("version_number"),cast(null(),String(32)).label("action"),cast(null(),Uuid).label("actor_id"),RawFact.created_at.label("timestamp")).where(*raw_conditions)
    snapshot=union_all(versions,raws).subquery()
    return select(snapshot).order_by(snapshot.c.source_order,snapshot.c.source_id).execution_options(yield_per=500)


def _spreadsheet_safe(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _iso(value:datetime)->str:return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


class _HashingWriter(io.RawIOBase):
    def __init__(self, raw): self.raw, self.digest, self.size = raw, hashlib.sha256(), 0
    def writable(self): return True
    def write(self, data): self.digest.update(data); self.size += len(data); return self.raw.write(data)


def _write_export(session: Session, project_id: UUID, filters: dict[str, str], format: str,
                  include: bool, max_rows: int, max_bytes: int):
    output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    manifest = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    hashing = _HashingWriter(output)
    text = io.TextIOWrapper(hashing, encoding="utf-8-sig" if format == "csv" else "utf-8", newline="")
    count = 0
    try:
        writer = csv.writer(text, delimiter="\t" if format == "tsv" else ",", lineterminator="\n") if format != "json" else None
        if writer: writer.writerow(SCRIPT_SCHEMA59_COLUMNS)
        else: text.write("[")

        def emit(source: str, identity: UUID, row_json: dict[str, str], provenance: dict[str, object]):
            nonlocal count
            if count >= max_rows: raise ExportLimit("导出记录数量超过限制")
            projected = platform_to_script_row(row_json)
            original = [projected[c] for c in SCRIPT_SCHEMA59_COLUMNS]
            if writer: writer.writerow([_spreadsheet_safe(v) for v in original])
            else:
                if count: text.write(",")
                text.write(json.dumps({"row": dict(zip(SCRIPT_SCHEMA59_COLUMNS, original)), "provenance": provenance}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            line = json.dumps({"ordinal": count, "source_kind": source, "id": str(identity)}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            manifest.write(line); count += 1
            text.flush()
            if hashing.size > max_bytes: raise ExportLimit("导出文件大小超过限制")

        for item in session.execute(_snapshot_statement(project_id,filters,include)).mappings():
            if item["source_kind"]=="version":emit("version",item["source_id"],item["row_json"],{"source":"version","root_id":str(item["root_id"]),"version_id":str(item["source_id"]),"version_number":item["version_number"],"action":item["action"],"actor_id":str(item["actor_id"]),"timestamp":_iso(item["timestamp"])})
            else:emit("raw",item["source_id"],item["row_json"],{"source":"raw","raw_fact_id":str(item["source_id"]),"version_id":None,"version_number":None,"action":None,"actor_id":None,"timestamp":_iso(item["timestamp"])})
        if format == "json": text.write("]")
        text.flush()
        if hashing.size > max_bytes: raise ExportLimit("导出文件大小超过限制")
        text.detach(); output.seek(0); manifest.seek(0)
        return output, manifest, count, hashing.digest.hexdigest(), hashing.size
    except Exception:
        try: text.close()
        finally: output.close(); manifest.close()
        raise


def _insert_items(session: Session, export_id: UUID, project_id: UUID, manifest) -> None:
    batch: list[dict[str, object]] = []
    for line in manifest:
        item = json.loads(line)
        batch.append({"export_id": export_id, "ordinal": item["ordinal"], "project_id": project_id,
                      "source_kind": item["source_kind"],
                      "fact_version_id": UUID(item["id"]) if item["source_kind"] == "version" else None,
                      "raw_fact_id": UUID(item["id"]) if item["source_kind"] == "raw" else None})
        if len(batch) == 500: session.execute(insert(ExportItem), batch); batch.clear()
    if batch: session.execute(insert(ExportItem), batch)


def claim_pending_export(session:Session,export_id:UUID,cutoff:datetime,token:str,*,now:datetime|None=None)->bool:
    if len(token)!=32:return False
    now=now or datetime.now(UTC)
    stale=now-timedelta(minutes=15)
    try:
        result=session.execute(update(ExportRecord).where(ExportRecord.id==export_id,or_(and_(ExportRecord.status=="pending",ExportRecord.created_at<cutoff),and_(ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_claimed_at<stale))).values(status="cleanup_claimed",cleanup_token=token,cleanup_claimed_at=now).execution_options(synchronize_session=False))
        if result.rowcount!=1:session.rollback();return False
        session.commit();session.expire_all();return True
    except Exception:
        session.rollback();owned=session.scalar(select(ExportRecord.id).where(ExportRecord.id==export_id,ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_token==token))
        if owned is not None:return True
        raise


def finalize_export_record(session:Session,export_id:UUID,count:int,size:int,digest:str,completed_at:datetime)->bool:
    result=session.execute(update(ExportRecord).where(ExportRecord.id==export_id,ExportRecord.status=="pending").values(status="completed",record_count=count,size_bytes=size,sha256=digest,completed_at=completed_at).execution_options(synchronize_session=False))
    session.flush();return result.rowcount==1


def _lock_cleanup_claim(session:Session,export_id:UUID,token:str)->bool:
    """Revalidate the token and hold a database write lock through cleanup."""
    result=session.execute(update(ExportRecord).where(ExportRecord.id==export_id,ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_token==token).values(cleanup_claimed_at=datetime.now(UTC)).execution_options(synchronize_session=False))
    session.flush();return result.rowcount==1


def reconcile_pending_exports(session:Session,storage:Storage,cutoff:datetime)->int:
    if cutoff.tzinfo is None:cutoff=cutoff.replace(tzinfo=UTC)
    ids=list(session.scalars(select(ExportRecord.id).where(or_(and_(ExportRecord.status=="pending",ExportRecord.created_at<cutoff),and_(ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_claimed_at<datetime.now(UTC)-timedelta(minutes=15)))).order_by(ExportRecord.created_at,ExportRecord.id)))
    cleaned=0
    for export_id in ids:
        token=uuid4().hex
        if not claim_pending_export(session,export_id,cutoff,token):continue
        if not _lock_cleanup_claim(session,export_id,token):session.rollback();continue
        value=session.scalar(select(ExportRecord).where(ExportRecord.id==export_id,ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_token==token))
        if value is None:session.rollback();continue
        try:storage.cleanup_owned_export(export_ownership(value))
        except Exception:session.rollback();continue
        session.execute(delete(ExportItem).where(ExportItem.export_id==value.id))
        result=session.execute(delete(ExportRecord).where(ExportRecord.id==value.id,ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_token==token));session.commit();cleaned+=int(result.rowcount==1)
    return cleaned


def _discard_pending(session:Session,storage:Storage,value:ExportRecord)->bool:
    token=uuid4().hex
    if not claim_pending_export(session,value.id,datetime.now(UTC)+timedelta(seconds=1),token):return False
    if not _lock_cleanup_claim(session,value.id,token):session.rollback();return False
    value=session.scalar(select(ExportRecord).where(ExportRecord.id==value.id,ExportRecord.cleanup_token==token))
    if value is None:session.rollback();return False
    try:storage.cleanup_owned_export(export_ownership(value))
    except Exception:session.rollback();return False
    session.execute(delete(ExportItem).where(ExportItem.export_id==value.id))
    result=session.execute(delete(ExportRecord).where(ExportRecord.id==value.id,ExportRecord.status=="cleanup_claimed",ExportRecord.cleanup_token==token));session.commit();return result.rowcount==1


def discard_pending_export(session:Session,storage:Storage,value:ExportRecord)->bool:
    session.rollback();return _discard_pending(session,storage,value)


def create_export(session: Session, project_id: UUID, filters: dict[str, object], format: str,
                  include_unreviewed: bool, actor_id: UUID, idempotency_key: str, storage: Storage) -> ExportRecord:
    if format not in {"tsv", "csv", "json"} or not (1 <= len(idempotency_key) <= 128): raise ExportError("导出请求无效")
    reconcile_pending_exports(session,storage,datetime.now(UTC)-timedelta(days=1))
    normalized = _normalize(filters); fp = _fingerprint(project_id, actor_id, normalized, format, include_unreviewed)
    # Request is durable before authorization/generation starts.
    if session.get(User, actor_id) is not None and session.get(Project, project_id) is not None:
        try:
            _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.request", request_id=idempotency_key, outcome="requested", metadata={"format": format, "include_unreviewed": include_unreviewed, "filter_keys": list(normalized)})
        except Exception:
            session.rollback()
            durable = session.scalar(select(AuditEvent.id).where(
                AuditEvent.actor_id == actor_id, AuditEvent.project_id == project_id,
                AuditEvent.action == "export.request", AuditEvent.request_id == idempotency_key,
            ))
            if durable is None: raise
    try:
        granted = _permissions(session, actor_id, project_id)
        if include_unreviewed and "exports:unreviewed" not in granted: raise ExportDenied("未评审结果导出需要更高权限")
    except ExportDenied:
        session.rollback()
        if session.get(User, actor_id) is not None and session.get(Project, project_id) is not None:
            _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.denied", request_id=idempotency_key, outcome="denied", metadata={"reason": "permission_denied", "format": format})
        raise
    old = session.scalar(select(ExportRecord).where(ExportRecord.project_id == project_id, ExportRecord.actor_id == actor_id, ExportRecord.idempotency_key == idempotency_key))
    if old is not None:
        if old.request_fingerprint != fp:
            _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.failure", request_id=idempotency_key, outcome="failure", metadata={"reason": "idempotency_conflict", "format": format})
            raise ExportConflict("幂等键请求不一致")
        if old.status!="completed":raise ExportConflict("导出正在处理中")
        return old
    export_id, token = uuid4(), uuid4().hex
    ownership = ExportOwnership(export_id, token, storage.export_storage_key(token, "." + format))
    value = ExportRecord(id=export_id, project_id=project_id, actor_id=actor_id, filters_json=normalized,
        format=format, include_unreviewed=include_unreviewed, record_count=0, size_bytes=0,
        sha256=None, storage_key=ownership.storage_key, owner_token=token,
        status="pending", completed_at=None, idempotency_key=idempotency_key, request_fingerprint=fp)
    try:
        session.add(value); session.commit()
    except Exception:
        session.rollback()
        concurrent = session.scalar(select(ExportRecord).where(ExportRecord.project_id == project_id, ExportRecord.actor_id == actor_id, ExportRecord.idempotency_key == idempotency_key))
        if concurrent is not None and concurrent.request_fingerprint == fp and concurrent.status == "completed": return concurrent
        if concurrent is not None and concurrent.id==export_id and concurrent.request_fingerprint==fp and concurrent.status=="pending":value=concurrent
        else:
            _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.failure", request_id=idempotency_key, outcome="failure", metadata={"reason": "idempotency_conflict", "format": format})
            raise ExportConflict("导出请求冲突") from None
    output = manifest = None; ownership_created = False
    try:
        storage.create_export_ownership(ownership); ownership_created = True
        settings = get_settings()
        output, manifest, count, digest, size = _write_export(session, project_id, normalized, format, include_unreviewed, settings.max_export_rows, settings.max_export_bytes)
        stored = storage.save_owned_export(ownership, output)
        if stored.sha256 != digest or stored.size_bytes != size: raise ExportError("导出文件校验失败")
        _insert_items(session, export_id, project_id, manifest)
        if not finalize_export_record(session,value.id,count,size,digest,datetime.now(UTC)):
            raise CleanupClaimConflict("导出已进入清理流程")
        session.expire(value);value=session.get(ExportRecord,value.id)
        audit_event(session, actor_id=actor_id, project_id=project_id, action="export.success", target_type="export", target_id=value.id, request_id=idempotency_key, outcome="success", metadata={"export_id": str(value.id), "format": format, "record_count": count, "size_bytes": size, "sha256": digest})
        return value
    except CleanupClaimConflict:
        session.rollback()
        _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.failure", request_id=idempotency_key, outcome="failure", metadata={"reason":"cleanup_claimed","format":format})
        raise
    except (UploadTooLargeError, StorageCompensationError) as exc:
        session.rollback()
        _discard_pending(session,storage,value)
        _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.failure", request_id=idempotency_key, outcome="failure", metadata={"reason": "export_limit", "format": format})
        raise ExportLimit("导出文件大小超过限制") from exc
    except Exception:
        session.rollback()
        _discard_pending(session,storage,value)
        _audit_commit(session, actor_id=actor_id, project_id=project_id, action="export.failure", request_id=idempotency_key, outcome="failure", metadata={"reason": "export_failed", "format": format})
        raise
    finally:
        if output is not None: output.close()
        if manifest is not None: manifest.close()


def export_ownership(value: ExportRecord) -> ExportOwnership:
    return ExportOwnership(value.id, value.owner_token, value.storage_key)


def verify_export_content(session:Session,value:ExportRecord)->bool:
    raw=tempfile.SpooledTemporaryFile(max_size=1024*1024,mode="w+b");hashing=_HashingWriter(raw)
    text=io.TextIOWrapper(hashing,encoding="utf-8-sig" if value.format=="csv" else "utf-8",newline="")
    writer=csv.writer(text,delimiter="\t" if value.format=="tsv" else ",",lineterminator="\n") if value.format!="json" else None
    if writer:writer.writerow(SCRIPT_SCHEMA59_COLUMNS)
    else:text.write("[")
    statement=select(ExportItem,FactVersion,RawFact).outerjoin(FactVersion,ExportItem.fact_version_id==FactVersion.id).outerjoin(RawFact,ExportItem.raw_fact_id==RawFact.id).where(ExportItem.export_id==value.id).order_by(ExportItem.ordinal).execution_options(yield_per=500)
    count=0
    try:
        for item,version,source_raw in session.execute(statement):
            if item.ordinal!=count:return False
            source=version if item.source_kind=="version" else source_raw
            if source is None or source.project_id!=value.project_id:return False
            projected=platform_to_script_row(source.row_json)
            original=[projected[c] for c in SCRIPT_SCHEMA59_COLUMNS]
            if writer:writer.writerow([_spreadsheet_safe(v) for v in original])
            else:
                if count:text.write(",")
                provenance={"source":"version","root_id":str(version.root_id),"version_id":str(version.id),"version_number":version.version_number,"action":version.action,"actor_id":str(version.actor_id),"timestamp":_iso(version.created_at)} if version is not None else {"source":"raw","raw_fact_id":str(source_raw.id),"version_id":None,"version_number":None,"action":None,"actor_id":None,"timestamp":_iso(source_raw.created_at)}
                text.write(json.dumps({"row":dict(zip(SCRIPT_SCHEMA59_COLUMNS,original)),"provenance":provenance},ensure_ascii=False,sort_keys=True,separators=(",",":")))
            count+=1
        if value.format=="json":text.write("]")
        text.flush()
        return count==value.record_count and hashing.size==value.size_bytes and hashing.digest.hexdigest()==value.sha256
    finally:
        text.close();raw.close()
