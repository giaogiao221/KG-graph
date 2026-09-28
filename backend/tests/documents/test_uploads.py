import hashlib
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import Headers, UploadFile

from app.documents.models import Document, DocumentVersion
from app.documents import service as document_service
from app.documents.service import append_document_version, create_document
from app.documents.storage import (
    LocalStorage,
    StorageCompensationError,
    UploadTooLargeError,
    get_storage,
    safe_object_path,
)
from app.projects.models import Project


def test_upload_hashes_content_and_ignores_unsafe_filename(
    client_factory, project: Project
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("../../unsafe.md", b"# Safe", "text/markdown")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["sha256"] == hashlib.sha256(b"# Safe").hexdigest()
    assert body["original_filename"] == "../../unsafe.md"
    assert body["is_extractable"] is True
    assert body["version_number"] == 1
    assert all(part not in body for part in ("path", "absolute_path", "storage_key"))


def test_non_markdown_is_retained_without_extractable_claim(
    client_factory, project: Project
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("notes.txt", b"plain", "text/plain")},
    )

    assert response.status_code == 201
    assert response.json()["is_extractable"] is False


def test_upload_requires_project_membership(
    client_factory, project: Project
) -> None:
    response = client_factory("viewer").post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("safe.md", b"# Nope", "text/markdown")},
    )

    assert response.status_code == 403


def test_admin_can_upload_to_foreign_project_without_membership(
    client_factory, foreign_project: Project
) -> None:
    response = client_factory("admin").post(
        f"/api/projects/{foreign_project.id}/documents",
        files={"file": ("admin.md", b"admin", "text/markdown")},
    )

    assert response.status_code == 201


def test_new_version_appends_without_mutating_existing_version(
    client_factory,
    project: Project,
    app_session_factory: sessionmaker[Session],
) -> None:
    client = client_factory("operator")
    created = client.post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("one.md", b"one", "text/markdown")},
    ).json()
    response = client.post(
        f"/api/projects/{project.id}/documents/{created['document_id']}/versions",
        files={"file": ("two.md", b"two", "text/markdown")},
    )

    assert response.status_code == 201
    assert response.json()["version_number"] == 2
    with app_session_factory() as session:
        versions = list(
            session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == UUID(created["document_id"]))
                .order_by(DocumentVersion.version_number)
            )
        )
        assert [version.sha256 for version in versions] == [
            hashlib.sha256(b"one").hexdigest(),
            hashlib.sha256(b"two").hexdigest(),
        ]


def test_document_version_rows_are_immutable(
    client_factory,
    project: Project,
    app_session_factory: sessionmaker[Session],
) -> None:
    created = client_factory("operator").post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("one.md", b"one", "text/markdown")},
    ).json()

    with app_session_factory() as session:
        version = session.get(DocumentVersion, UUID(created["id"]))
        assert version is not None
        version.original_filename = "changed.md"
        with pytest.raises(ValueError, match="immutable"):
            session.commit()


def test_local_storage_streams_content_to_uuid_key(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    stored = storage.save(BytesIO(b"streamed"), ".md")

    assert stored.sha256 == hashlib.sha256(b"streamed").hexdigest()
    assert stored.size_bytes == 8
    assert stored.storage_key.endswith(".md")
    assert (tmp_path / stored.storage_key).read_bytes() == b"streamed"

    storage.delete(stored.storage_key)
    assert not (tmp_path / stored.storage_key).exists()


@pytest.mark.parametrize(
    "key", ["../escape", "nested/../../escape", str(Path.cwd().anchor + "escape")]
)
def test_storage_rejects_keys_that_escape_root(tmp_path, key: str) -> None:
    with pytest.raises(ValueError, match="escapes root"):
        safe_object_path(tmp_path, key)


def test_upload_over_limit_removes_partial_object(tmp_path) -> None:
    storage = LocalStorage(tmp_path, max_bytes=4)

    with pytest.raises(UploadTooLargeError):
        storage.save(BytesIO(b"12345"), ".md")

    assert list(tmp_path.iterdir()) == []


def test_storage_open_read_and_verified_copy_reject_tampering(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    stored = storage.save(BytesIO(b"immutable artifact"), ".json")

    with storage.open_read(stored.storage_key) as source:
        assert source.read() == b"immutable artifact"
    destination = tmp_path / "work" / "artifact.json"
    destination.parent.mkdir()
    storage.copy_to(
        stored.storage_key,
        destination,
        expected_size=stored.size_bytes,
        expected_sha256=stored.sha256,
        max_bytes=1024,
    )
    assert destination.read_bytes() == b"immutable artifact"

    safe_object_path(storage.root, stored.storage_key).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="storage object is invalid"):
        storage.copy_to(
            stored.storage_key,
            destination,
            expected_size=stored.size_bytes,
            expected_sha256=stored.sha256,
            max_bytes=1024,
        )


def test_upload_over_limit_returns_413(
    client_factory, project: Project, tmp_path: Path
) -> None:
    client_factory.app.dependency_overrides[get_storage] = lambda: LocalStorage(
        tmp_path / "limited", max_bytes=4
    )

    response = client_factory("operator").post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("large.md", b"12345", "text/markdown")},
    )

    assert response.status_code == 413
    assert "path" not in response.text.lower()
    assert list((tmp_path / "limited").iterdir()) == []


def _upload(filename: str = "failure.md", content: bytes = b"failure") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "text/markdown"}),
    )


@pytest.mark.parametrize("failing_method", ["flush", "commit"])
def test_database_failure_removes_saved_object_and_hides_path(
    app_session_factory: sessionmaker[Session],
    project: Project,
    users,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_method: str,
) -> None:
    storage = LocalStorage(tmp_path)
    with app_session_factory() as session:
        monkeypatch.setattr(
            session,
            failing_method,
            lambda: (_ for _ in ()).throw(RuntimeError("C:\\secret\\database")),
        )
        with pytest.raises(HTTPException) as exc_info:
            create_document(
                session,
                project_id=project.id,
                upload=_upload(),
                uploader_id=users["operator"].id,
                storage=storage,
            )

    assert exc_info.value.status_code == 500
    assert "secret" not in str(exc_info.value.detail).lower()
    assert list(tmp_path.iterdir()) == []


def test_version_conflict_removes_saved_object_and_returns_409(
    app_session_factory: sessionmaker[Session],
    project: Project,
    users,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path)
    with app_session_factory() as session:
        first = create_document(
            session,
            project_id=project.id,
            upload=_upload("one.md", b"one"),
            uploader_id=users["operator"].id,
            storage=storage,
        )
        existing_key = first.storage_key
        monkeypatch.setattr(
            session,
            "commit",
            lambda: (_ for _ in ()).throw(IntegrityError("insert", {}, Exception())),
        )
        with pytest.raises(HTTPException) as exc_info:
            append_document_version(
                session,
                document_id=first.document_id,
                project_id=project.id,
                upload=_upload("two.md", b"two"),
                uploader_id=users["operator"].id,
                storage=storage,
            )

    assert exc_info.value.status_code == 409
    assert {path.name for path in tmp_path.iterdir()} == {existing_key}


def test_version_construction_failure_after_save_is_compensated(
    app_session_factory: sessionmaker[Session],
    project: Project,
    users,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path)
    monkeypatch.setattr(
        document_service,
        "DocumentVersion",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("construction failed")),
    )

    with app_session_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            create_document(
                session,
                project_id=project.id,
                upload=_upload(),
                uploader_id=users["operator"].id,
                storage=storage,
            )

    assert exc_info.value.status_code == 500
    assert list(tmp_path.iterdir()) == []


def test_append_failure_before_commit_is_compensated(
    app_session_factory: sessionmaker[Session],
    project: Project,
    users,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path)
    with app_session_factory() as session:
        first = create_document(
            session,
            project_id=project.id,
            upload=_upload("one.md", b"one"),
            uploader_id=users["operator"].id,
            storage=storage,
        )
        existing_key = first.storage_key
        monkeypatch.setattr(
            session,
            "add",
            lambda _value: (_ for _ in ()).throw(RuntimeError("add failed")),
        )
        with pytest.raises(HTTPException) as exc_info:
            append_document_version(
                session,
                document_id=first.document_id,
                project_id=project.id,
                upload=_upload("two.md", b"two"),
                uploader_id=users["operator"].id,
                storage=storage,
            )

    assert exc_info.value.status_code == 500
    assert {path.name for path in tmp_path.iterdir()} == {existing_key}


def test_append_allocator_failure_after_save_is_compensated(
    app_session_factory: sessionmaker[Session],
    project: Project,
    users,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path)
    with app_session_factory() as session:
        first = create_document(
            session,
            project_id=project.id,
            upload=_upload("one.md", b"one"),
            uploader_id=users["operator"].id,
            storage=storage,
        )
        existing_key = first.storage_key
        monkeypatch.setattr(
            session,
            "scalar",
            lambda _statement: (_ for _ in ()).throw(RuntimeError("allocator failed")),
        )
        with pytest.raises(HTTPException) as exc_info:
            append_document_version(
                session,
                document_id=first.document_id,
                project_id=project.id,
                upload=_upload("two.md", b"two"),
                uploader_id=users["operator"].id,
                storage=storage,
            )

    assert exc_info.value.status_code == 500
    assert {path.name for path in tmp_path.iterdir()} == {existing_key}


def test_delete_failure_is_journaled_and_retry_clears_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorage(tmp_path)
    stored = storage.save(BytesIO(b"pending"), ".md")
    object_path = tmp_path / stored.storage_key
    original_unlink = Path.unlink

    def fail_object_unlink(path: Path, *args, **kwargs) -> None:
        if path == object_path:
            raise OSError("C:\\sensitive\\host-path")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_object_unlink)
    with pytest.raises(StorageCompensationError, match="pending"):
        storage.delete(stored.storage_key)

    journal = tmp_path / ".pending-cleanup.jsonl"
    journal_text = journal.read_text(encoding="utf-8")
    assert stored.storage_key in journal_text
    assert "delete_failed" in journal_text
    assert "sensitive" not in journal_text
    assert str(tmp_path) not in journal_text

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert storage.retry_pending_cleanup() == 1
    assert not object_path.exists()
    assert not journal.exists()


def test_pending_cleanup_journal_sanitizes_untrusted_reason(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    stored = storage.save(BytesIO(b"pending"), ".md")

    storage.record_pending_cleanup(
        stored.storage_key, "C:\\sensitive\\host-path: permission denied"
    )

    journal_text = (tmp_path / ".pending-cleanup.jsonl").read_text(encoding="utf-8")
    assert "sensitive" not in journal_text
    assert "cleanup_failed" in journal_text


def test_atomic_version_allocator_returns_distinct_numbers(
    app_session_factory: sessionmaker[Session], project: Project, users, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path)
    with app_session_factory() as session:
        first = create_document(
            session,
            project_id=project.id,
            upload=_upload("one.md", b"one"),
            uploader_id=users["operator"].id,
            storage=storage,
        )
        second = append_document_version(
            session,
            document_id=first.document_id,
            project_id=project.id,
            upload=_upload("two.md", b"two"),
            uploader_id=users["operator"].id,
            storage=storage,
        )
        third = append_document_version(
            session,
            document_id=first.document_id,
            project_id=project.id,
            upload=_upload("three.md", b"three"),
            uploader_id=users["operator"].id,
            storage=storage,
        )

    assert [second.version_number, third.version_number] == [2, 3]


def test_database_triggers_block_raw_version_update_and_delete(
    app_session_factory: sessionmaker[Session],
    project: Project,
    users,
    tmp_path: Path,
) -> None:
    with app_session_factory() as session:
        version = create_document(
            session,
            project_id=project.id,
            upload=_upload(),
            uploader_id=users["operator"].id,
            storage=LocalStorage(tmp_path),
        )
        with pytest.raises(DBAPIError):
            session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == version.id)
                .values(original_filename="changed.md")
            )
        session.rollback()
        with pytest.raises(DBAPIError):
            session.execute(delete(DocumentVersion).where(DocumentVersion.id == version.id))


def test_offline_migration_installs_and_removes_immutability_trigger() -> None:
    backend_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment["EXTRACTION_DATABASE_URL"] = (
        "postgresql+psycopg://offline:offline@localhost/extraction"
    )
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    downgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade",
            "0002_projects_documents:0001_core_tables",
            "--sql",
        ],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert upgrade.returncode == 0, upgrade.stderr
    assert "prevent_document_version_mutation" in upgrade.stdout
    assert downgrade.returncode == 0, downgrade.stderr
    assert "DROP FUNCTION" in downgrade.stdout
