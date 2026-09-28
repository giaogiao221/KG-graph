from __future__ import annotations

from typing import Annotated, Iterator
from uuid import UUID
import hashlib
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, current_user
from app.auth.models import User
from app.core.audit import audit_event
from app.documents.storage import Storage, get_storage
from app.exports.models import ExportRecord
from app.exports.schemas import ExportCreate, ExportPage, ExportResponse
from app.exports.service import ExportConflict, ExportDenied, ExportError, ExportLimit, create_export, discard_pending_export, export_ownership, verify_export_content
from app.projects.service import get_member_project


router = APIRouter(prefix="/api/projects/{project_id}/exports", tags=["exports"])


def _admin(user: User) -> bool: return any(role.name == "admin" for role in user.roles)
def _can_export(user: User) -> bool: return any(p.code == "exports:create" for role in user.roles for p in role.permissions)


def _verified_stream(storage: Storage, value: ExportRecord):
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    digest = hashlib.sha256(); size = 0
    try:
        with storage.open_owned_export(export_ownership(value)) as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > value.size_bytes: raise ValueError
                digest.update(chunk); spool.write(chunk)
        if size != value.size_bytes or digest.hexdigest() != value.sha256: raise ValueError
        spool.seek(0); return spool
    except Exception:
        spool.close()
        raise ValueError("storage object is invalid") from None


def _record_failure(session: Session, *, user: User, project_id: UUID,
                    request_id: str, format: str, denied: bool = False) -> None:
    session.rollback()
    audit_event(
        session, actor_id=user.id, project_id=project_id,
        action="export.denied" if denied else "export.failure",
        target_type="export", target_id=None, request_id=request_id,
        outcome="denied" if denied else "failure",
        metadata={"reason": "permission_denied" if denied else "export_failed", "format": format},
    )
    session.commit()


@router.get("", response_model=ExportPage)
def list_exports(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ExportPage:
    if not _can_export(user):
        raise HTTPException(status_code=403, detail="export permission required")
    get_member_project(session, project_id, user.id, admin=_admin(user))
    conditions = (ExportRecord.project_id == project_id,)
    total = session.scalar(
        select(func.count()).select_from(ExportRecord).where(*conditions)
    ) or 0
    items = list(session.scalars(
        select(ExportRecord).where(*conditions)
        .order_by(ExportRecord.created_at.desc(), ExportRecord.id)
        .offset(offset).limit(limit)
    ))
    return ExportPage(items=items, total=total, offset=offset, limit=limit)


@router.post("", response_model=ExportResponse, status_code=201)
def add_export(project_id: UUID, payload: ExportCreate,
               session: Annotated[Session, Depends(auth_session)],
               user: Annotated[User, Depends(current_user)],
               storage: Annotated[Storage, Depends(get_storage)]) -> ExportRecord:
    value: ExportRecord | None = None
    try:
        value = create_export(session, project_id, payload.filters.model_dump(exclude_none=True), payload.format, payload.include_unreviewed, user.id, payload.idempotency_key, storage)
        session.commit(); session.refresh(value); return value
    except ExportDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ExportConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ExportLimit as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except ExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        session.rollback()
        committed = session.scalar(select(ExportRecord).where(
            ExportRecord.project_id == project_id,
            ExportRecord.actor_id == user.id,
            ExportRecord.idempotency_key == payload.idempotency_key,
        ))
        if committed is not None:
            return committed
        if value is not None:
            try: discard_pending_export(session,storage,value)
            except Exception: pass
        _record_failure(session, user=user, project_id=project_id, request_id=payload.idempotency_key, format=payload.format)
        raise HTTPException(status_code=500, detail="导出失败") from None


@router.get("/{export_id}/download")
def download_export(project_id: UUID, export_id: UUID,
                    session: Annotated[Session, Depends(auth_session)],
                    user: Annotated[User, Depends(current_user)],
                    storage: Annotated[Storage, Depends(get_storage)]):
    value = session.scalar(select(ExportRecord).where(ExportRecord.id == export_id, ExportRecord.project_id == project_id))
    if value is None: raise HTTPException(status_code=404, detail="导出记录不存在")
    try:
        if not _can_export(user): raise HTTPException(status_code=403, detail="无导出权限")
        get_member_project(session, project_id, user.id, admin=_admin(user))
        if not verify_export_content(session, value): raise ValueError
        stream = _verified_stream(storage, value)
    except (HTTPException, ValueError) as exc:
        session.rollback()
        audit_event(session, actor_id=user.id, project_id=project_id, action="export.download", target_type="export", target_id=value.id, request_id=str(value.id), outcome="failure", metadata={"export_id": str(value.id), "reason": "download_failed", "format": value.format})
        session.commit()
        if isinstance(exc, HTTPException): raise exc
        raise HTTPException(status_code=404, detail="导出文件不可用") from None
    audit_event(session, actor_id=user.id, project_id=project_id, action="export.download", target_type="export", target_id=value.id, request_id=str(value.id), outcome="success", metadata={"export_id": str(value.id), "format": value.format})
    session.commit()
    def chunks() -> Iterator[bytes]:
        try:
            while data := stream.read(1024 * 1024): yield data
        finally: stream.close()
    media = {"tsv": "text/tab-separated-values", "csv": "text/csv", "json": "application/json"}[value.format]
    return StreamingResponse(chunks(), media_type=media, headers={"Content-Disposition": f'attachment; filename="export-{value.id}.{value.format}"'})
