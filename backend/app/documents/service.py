from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.documents.models import Document, DocumentVersion
from app.documents.storage import (
    Storage,
    StorageCompensationError,
    StoredObject,
    UploadTooLargeError,
)


MARKDOWN_SUFFIXES = {".md", ".markdown"}


def _new_version(
    *,
    document: Document | None,
    document_id: UUID | None,
    upload: UploadFile,
    uploader_id: UUID,
    version_number: int,
    stored: StoredObject,
) -> DocumentVersion:
    original_filename = upload.filename or "upload"
    suffix = Path(original_filename).suffix.lower()
    values = dict(
        uploader_id=uploader_id,
        version_number=version_number,
        original_filename=original_filename,
        storage_key=stored.storage_key,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        mime_type=upload.content_type or "application/octet-stream",
        is_extractable=suffix in MARKDOWN_SUFFIXES,
    )
    if document is not None:
        return DocumentVersion(document=document, **values)
    return DocumentVersion(document_id=document_id, **values)


def _discard(storage: Storage, key: str) -> None:
    try:
        storage.delete(key)
    except StorageCompensationError:
        raise
    except Exception:
        try:
            storage.record_pending_cleanup(key, "delete_failed")
        except Exception:
            raise StorageCompensationError(
                "storage cleanup could not be journaled"
            ) from None
        raise StorageCompensationError("storage cleanup pending") from None


def _rollback(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        pass


def _finish_failure(
    session: Session,
    storage: Storage,
    stored: StoredObject | None,
    failure: HTTPException,
) -> None:
    _rollback(session)
    if stored is not None:
        try:
            _discard(storage, stored.storage_key)
        except StorageCompensationError:
            raise HTTPException(
                status_code=500, detail="storage cleanup pending"
            ) from None
    raise failure


def _failure_for(exception: Exception) -> HTTPException:
    if isinstance(exception, UploadTooLargeError):
        return HTTPException(status_code=413, detail="upload exceeds size limit")
    if isinstance(exception, IntegrityError):
        return HTTPException(status_code=409, detail="document version conflict")
    if isinstance(exception, HTTPException):
        return exception
    return HTTPException(status_code=500, detail="document persistence failed")


def _persist_create(
    session: Session,
    *,
    project_id: UUID,
    upload: UploadFile,
    uploader_id: UUID,
    storage: Storage,
) -> DocumentVersion:
    stored: StoredObject | None = None
    committed = False
    failure: HTTPException | None = None
    version: DocumentVersion | None = None
    try:
        suffix = Path(upload.filename or "upload").suffix.lower()
        stored = storage.save(upload.file, suffix)
        document = Document(project_id=project_id, created_by_id=uploader_id)
        session.add(document)
        version = _new_version(
            document=document,
            document_id=None,
            upload=upload,
            uploader_id=uploader_id,
            version_number=1,
            stored=stored,
        )
        session.add(version)
        session.flush()
        session.commit()
        committed = True
    except Exception as exception:
        failure = _failure_for(exception)
    finally:
        if not committed:
            assert failure is not None
            _finish_failure(session, storage, stored, failure)
    assert version is not None
    session.refresh(version)
    return version


def create_document(
    session: Session,
    *,
    project_id: UUID,
    upload: UploadFile,
    uploader_id: UUID,
    storage: Storage,
) -> DocumentVersion:
    return _persist_create(
        session,
        project_id=project_id,
        upload=upload,
        uploader_id=uploader_id,
        storage=storage,
    )


def append_document_version(
    session: Session,
    *,
    document_id: UUID,
    project_id: UUID,
    upload: UploadFile,
    uploader_id: UUID,
    storage: Storage,
) -> DocumentVersion:
    stored: StoredObject | None = None
    committed = False
    failure: HTTPException | None = None
    version: DocumentVersion | None = None
    try:
        stored = storage.save(upload.file, Path(upload.filename or "upload").suffix.lower())
        next_number = session.scalar(
            update(Document)
            .where(Document.id == document_id, Document.project_id == project_id)
            .values(next_version_number=Document.next_version_number + 1)
            .returning(Document.next_version_number)
        )
        if next_number is None:
            raise HTTPException(status_code=404, detail="document not found")
        version = _new_version(
            document=None,
            document_id=document_id,
            upload=upload,
            uploader_id=uploader_id,
            version_number=next_number - 1,
            stored=stored,
        )
        session.add(version)
        session.flush()
        session.commit()
        committed = True
    except Exception as exception:
        failure = _failure_for(exception)
    finally:
        if not committed:
            assert failure is not None
            _finish_failure(session, storage, stored, failure)
    assert version is not None
    session.refresh(version)
    return version
