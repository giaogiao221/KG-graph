from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, require_permissions
from app.auth.models import User
from app.documents.models import Document, DocumentVersion
from app.documents.schemas import DocumentVersionResponse
from app.documents.service import append_document_version, create_document
from app.documents.storage import Storage, get_storage
from app.projects.service import get_member_project


router = APIRouter(prefix="/api/projects/{project_id}/documents", tags=["documents"])


def _is_admin(user: User) -> bool:
    return any(role.name == "admin" for role in user.roles)


def _response(version: DocumentVersion, project_id: UUID) -> DocumentVersionResponse:
    return DocumentVersionResponse.model_validate(
        {
            "id": version.id,
            "document_id": version.document_id,
            "project_id": project_id,
            "version_number": version.version_number,
            "original_filename": version.original_filename,
            "sha256": version.sha256,
            "size_bytes": version.size_bytes,
            "mime_type": version.mime_type,
            "is_extractable": version.is_extractable,
            "uploader_id": version.uploader_id,
            "created_at": version.created_at,
        }
    )


@router.post("", response_model=DocumentVersionResponse, status_code=201)
def upload_document(
    project_id: UUID,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
    storage: Annotated[Storage, Depends(get_storage)],
) -> DocumentVersionResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    return _response(
        create_document(
            session,
            project_id=project_id,
            upload=file,
            uploader_id=user.id,
            storage=storage,
        ),
        project_id,
    )


@router.get("", response_model=list[DocumentVersionResponse])
def list_documents(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[DocumentVersionResponse]:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    documents = list(
        session.scalars(
            select(Document).where(Document.project_id == project_id)
            .order_by(Document.created_at, Document.id).offset(offset).limit(limit)
        )
    )
    return [_response(document.versions[-1], project_id) for document in documents]


@router.post("/{document_id}/versions", response_model=DocumentVersionResponse, status_code=201)
def upload_document_version(
    project_id: UUID,
    document_id: UUID,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:edit"))],
    storage: Annotated[Storage, Depends(get_storage)],
) -> DocumentVersionResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    return _response(
        append_document_version(
            session,
            document_id=document_id,
            project_id=project_id,
            upload=file,
            uploader_id=user.id,
            storage=storage,
        ),
        project_id,
    )


@router.get("/{document_id}/versions", response_model=list[DocumentVersionResponse])
def list_document_versions(
    project_id: UUID,
    document_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[DocumentVersionResponse]:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    versions = list(
        session.scalars(
            select(DocumentVersion)
            .join(Document)
            .where(
                DocumentVersion.document_id == document_id,
                Document.project_id == project_id,
            )
            .order_by(DocumentVersion.version_number)
            .offset(offset).limit(limit)
        )
    )
    return [_response(version, project_id) for version in versions]
