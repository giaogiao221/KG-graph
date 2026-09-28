from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
from io import BytesIO
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.documents.storage import (
    ArtifactOwnership,
    LocalStorage,
    StorageCollisionError,
    StorageCompensationError,
    StorageOwnershipError,
)
from app.extraction.contracts import AdapterContext
from app.extraction.native.common import write_route_output
from app.jobs.models import JobArtifact, JobStep
from app.jobs import tasks as job_tasks


def _owned_key(token: str, suffix: str) -> str:
    return f"artifacts/{token}/payload{suffix}"


def test_storage_reserves_opaque_key_before_writing(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")

    key = storage.reserve_key(".json")

    assert not (storage.root / key).exists()
    stored = storage.save_with_key(key, BytesIO(b"artifact"))
    assert stored.storage_key == key
    assert stored.size_bytes == 8
    assert (storage.root / key).read_bytes() == b"artifact"


def test_artifact_reservation_uses_own_namespace_and_skips_existing_key(
    tmp_path: Path, monkeypatch
):
    storage = LocalStorage(tmp_path / "objects")
    collisions = iter(["1" * 32, "2" * 32])
    monkeypatch.setattr(
        "app.documents.storage.uuid4",
        lambda: type("FixedUUID", (), {"hex": next(collisions)})(),
    )
    (storage.root / f"artifact-{'1' * 32}.json").write_bytes(b"existing")

    key = storage.reserve_key(".json")

    assert key == f"artifact-{'2' * 32}.json"


def test_save_with_key_collision_never_deletes_existing_object(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    key = f"artifact-{'a' * 32}.json"
    target = storage.root / key
    target.write_bytes(b"original")

    with pytest.raises(StorageCollisionError, match="storage key collision"):
        storage.save_with_key(key, BytesIO(b"replacement"))

    assert target.read_bytes() == b"original"


def test_owned_artifact_directory_marker_and_delete_protocol(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(
        artifact_id=uuid4(),
        owner_token="a" * 32,
        storage_key=f"artifacts/{'a' * 32}/payload.json",
    )

    storage.create_artifact_ownership(ownership)
    storage.verify_artifact_ownership(ownership)
    stored = storage.save_owned_artifact(ownership, BytesIO(b"payload"))

    assert stored.storage_key == ownership.storage_key
    assert (storage.root / ownership.storage_key).read_bytes() == b"payload"
    storage.delete_owned_artifact(ownership)
    assert not (storage.root / f"artifacts/{'a' * 32}").exists()


def test_owned_artifact_directory_collision_preserves_every_existing_byte(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(
        artifact_id=uuid4(),
        owner_token="b" * 32,
        storage_key=f"artifacts/{'b' * 32}/payload.tsv",
    )
    container = storage.root / "artifacts" / ("b" * 32)
    container.mkdir(parents=True)
    marker = container / ".owner.json"
    payload = container / "payload.tsv"
    marker.write_bytes(b"foreign-marker")
    payload.write_bytes(b"foreign-payload")

    with pytest.raises(StorageCollisionError, match="storage key collision"):
        storage.create_artifact_ownership(ownership)

    assert marker.read_bytes() == b"foreign-marker"
    assert payload.read_bytes() == b"foreign-payload"


def test_owned_artifact_delete_rejects_marker_mismatch_without_touching_payload(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(
        artifact_id=uuid4(),
        owner_token="c" * 32,
        storage_key=f"artifacts/{'c' * 32}/payload.json",
    )
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    marker = storage.root / "artifacts" / ("c" * 32) / ".owner.json"
    marker.write_bytes(b"{}")

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.delete_owned_artifact(ownership)

    tombstone_payload = (
        storage.root / "artifacts" / ".cleanup" / ownership.owner_token / "payload.json"
    )
    assert tombstone_payload.read_bytes() == b"payload"


def test_owned_artifact_payload_collision_preserves_marker_and_foreign_payload(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(
        artifact_id=uuid4(),
        owner_token="4" * 32,
        storage_key=_owned_key("4" * 32, ".json"),
    )
    storage.create_artifact_ownership(ownership)
    marker = storage.root / "artifacts" / ("4" * 32) / ".owner.json"
    marker_before = marker.read_bytes()
    payload = storage.root / ownership.storage_key
    payload.write_bytes(b"foreign-payload")

    with pytest.raises(StorageCollisionError, match="storage key collision"):
        storage.save_owned_artifact(ownership, BytesIO(b"ours"))

    assert marker.read_bytes() == marker_before
    assert payload.read_bytes() == b"foreign-payload"


def test_owned_delete_payload_failure_keeps_marker_for_safe_retry(
    tmp_path: Path, monkeypatch
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(
        artifact_id=uuid4(),
        owner_token="5" * 32,
        storage_key=_owned_key("5" * 32, ".tsv"),
    )
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone_payload = tombstone / "payload.tsv"
    tombstone_marker = tombstone / ".owner.json"
    if os.name == "nt":
        api = storage._windows_cleanup_api()
        original_dispose = api["dispose"]
        calls = 0

        def fail_first_dispose(handle):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise StorageCompensationError("storage cleanup pending")
            return original_dispose(handle)

        api["dispose"] = fail_first_dispose
        monkeypatch.setattr(storage, "_windows_cleanup_api", lambda: api)
    else:
        original_unlink = os.unlink

        def fail_payload_unlink(path, *args, **kwargs):
            if path == "payload.tsv":
                raise PermissionError("busy")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fail_payload_unlink)
    with pytest.raises(StorageCompensationError, match="storage cleanup pending"):
        storage.delete_owned_artifact(ownership)

    assert tombstone_payload.read_bytes() == b"payload"
    assert tombstone_marker.read_bytes() == storage._marker_bytes(ownership)


def test_ordinary_document_save_and_delete_remain_unowned(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")

    stored = storage.save(BytesIO(b"document"), ".pdf")

    assert not stored.storage_key.startswith("artifacts/")
    storage.delete(stored.storage_key)
    assert not (storage.root / stored.storage_key).exists()


def test_owned_delete_renames_to_deterministic_tombstone_before_unlink(
    tmp_path: Path, monkeypatch
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "8" * 32, _owned_key("8" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    original = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    def reject_path_unlink(*_args, **_kwargs):
        raise AssertionError("owned cleanup must use handle-bound deletion")

    monkeypatch.setattr(Path, "unlink", reject_path_unlink)
    monkeypatch.setattr(os, "unlink", reject_path_unlink)
    storage.delete_owned_artifact(ownership)

    assert not original.exists()
    assert tombstone.is_dir() and list(tombstone.iterdir()) == []


def test_owned_delete_retries_after_payload_deleted_but_marker_remains(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "9" * 32, _owned_key("9" * 32, ".tsv"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    source = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.parent.mkdir()
    os.replace(source, tombstone)
    (tombstone / "payload.tsv").unlink()

    storage.delete_owned_artifact(ownership)

    assert tombstone.is_dir() and list(tombstone.iterdir()) == []


def test_owned_delete_retries_empty_tombstone_after_marker_deleted(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "0" * 32, _owned_key("0" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    source = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.parent.mkdir()
    os.replace(source, tombstone)
    (tombstone / ".owner.json").unlink()

    storage.delete_owned_artifact(ownership)

    assert tombstone.is_dir() and list(tombstone.iterdir()) == []


def test_owned_delete_rejects_markerless_tombstone_that_still_has_payload(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "1" * 32, _owned_key("1" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    source = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.parent.mkdir()
    os.replace(source, tombstone)
    (tombstone / ".owner.json").unlink()

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.delete_owned_artifact(ownership)

    assert (tombstone / "payload.json").read_bytes() == b"payload"


def test_owned_delete_tombstone_ignores_original_rebuilt_after_rename(
    tmp_path: Path, monkeypatch
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "2" * 32, _owned_key("2" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    original = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_bytes(b"protected")
    original_delete = storage._delete_tombstone

    def rebuild_then_delete(owner, target):
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(original), str(outside)],
                check=True,
                capture_output=True,
            )
        else:
            os.symlink(outside, original, target_is_directory=True)
        return original_delete(owner, target)

    monkeypatch.setattr(storage, "_delete_tombstone", rebuild_then_delete)

    storage.delete_owned_artifact(ownership)

    assert protected.read_bytes() == b"protected"
    assert original.exists()


def test_owned_delete_refuses_when_source_and_tombstone_both_exist(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "a" * 32, _owned_key("a" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"source"))
    source = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.mkdir(parents=True)
    (tombstone / ".owner.json").write_bytes(storage._marker_bytes(ownership))
    (tombstone / "payload.json").write_bytes(b"tombstone")

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.delete_owned_artifact(ownership)

    assert (source / "payload.json").read_bytes() == b"source"
    assert (tombstone / "payload.json").read_bytes() == b"tombstone"


def test_owned_delete_keeps_empty_tombstone_as_durable_completion_evidence(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "b" * 32, _owned_key("b" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token

    storage.delete_owned_artifact(ownership)
    storage.delete_owned_artifact(ownership)

    assert tombstone.is_dir()
    assert list(tombstone.iterdir()) == []


def test_owned_delete_fsyncs_empty_tombstone_before_success(tmp_path: Path):
    class ObserveDirectorySync(LocalStorage):
        synced: list[Path]

        def __init__(self, root):
            super().__init__(root)
            self.synced = []

        def _fsync_directory(self, path):
            self.synced.append(path)
            return super()._fsync_directory(path)

    storage = ObserveDirectorySync(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "c" * 32, _owned_key("c" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token

    storage.delete_owned_artifact(ownership)

    assert tombstone in storage.synced


def test_posix_delete_empty_tombstone_returns_without_rmdir(
    tmp_path: Path, monkeypatch
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "1" * 32, _owned_key("1" * 32, ".json"))
    opened = iter((10, 11))
    removed: list[tuple[object, object]] = []
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: next(opened))
    monkeypatch.setattr(os, "fstat", lambda _fd: SimpleNamespace(st_dev=1, st_ino=2))
    monkeypatch.setattr(os, "listdir", lambda _fd: [])
    monkeypatch.setattr(
        os, "rmdir", lambda name, *, dir_fd=None: removed.append((name, dir_fd))
    )
    monkeypatch.setattr(os, "close", lambda _fd: None)

    storage._delete_tombstone_posix(ownership, tmp_path / "unused")

    assert removed == []


def test_posix_finalize_is_only_stage_that_rmdirs_empty_tombstone(
    tmp_path: Path, monkeypatch
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "2" * 32, _owned_key("2" * 32, ".json"))
    opened = iter((20, 21))
    identity = SimpleNamespace(st_dev=3, st_ino=4)
    removed: list[tuple[object, object]] = []
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: next(opened))
    monkeypatch.setattr(os, "fstat", lambda _fd: identity)
    monkeypatch.setattr(os, "listdir", lambda _fd: [])
    monkeypatch.setattr(os, "stat", lambda *_args, **_kwargs: identity)
    monkeypatch.setattr(
        os, "rmdir", lambda name, *, dir_fd=None: removed.append((name, dir_fd))
    )
    monkeypatch.setattr(os, "close", lambda _fd: None)

    storage._finalize_tombstone_posix(ownership, tmp_path / "unused")

    assert removed == [(ownership.owner_token, 20)]


def test_finalize_owned_cleanup_removes_only_empty_tombstone(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "d" * 32, _owned_key("d" * 32, ".tsv"))
    storage.create_artifact_ownership(ownership)
    storage.delete_owned_artifact(ownership)
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token

    storage.finalize_owned_artifact_cleanup(ownership)

    assert not tombstone.exists()


@pytest.mark.parametrize("entry", [".owner.json", "payload.json", "foreign"])
def test_finalize_owned_cleanup_rejects_nonempty_tombstone(
    tmp_path: Path, entry: str
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "e" * 32, _owned_key("e" * 32, ".json"))
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.mkdir(parents=True)
    (tombstone / entry).write_bytes(b"do-not-delete")

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.finalize_owned_artifact_cleanup(ownership)

    assert (tombstone / entry).read_bytes() == b"do-not-delete"


def test_finalize_owned_cleanup_rejects_junction_and_preserves_external_bytes(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "f" * 32, _owned_key("f" * 32, ".json"))
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.parent.mkdir(parents=True)
    outside = tmp_path / "outside-finalize"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_bytes(b"protected")
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(tombstone), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        os.symlink(outside, tombstone, target_is_directory=True)

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.finalize_owned_artifact_cleanup(ownership)

    assert protected.read_bytes() == b"protected"


def test_owned_delete_rejects_tombstone_marker_mismatch(tmp_path: Path):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "3" * 32, _owned_key("3" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    source = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.parent.mkdir()
    os.rename(source, tombstone)
    (tombstone / ".owner.json").write_bytes(b"{}")

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.delete_owned_artifact(ownership)

    assert (tombstone / "payload.json").read_bytes() == b"payload"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle reparse contract")
@pytest.mark.parametrize("child_name", [".owner.json", "payload.json"])
def test_windows_owned_delete_rejects_child_reparse_handle_and_preserves_outside(
    tmp_path: Path, child_name: str
):
    storage = LocalStorage(tmp_path / "objects")
    ownership = ArtifactOwnership(uuid4(), "7" * 32, _owned_key("7" * 32, ".json"))
    storage.create_artifact_ownership(ownership)
    storage.save_owned_artifact(ownership, BytesIO(b"payload"))
    source = storage.root / "artifacts" / ownership.owner_token
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token
    tombstone.parent.mkdir()
    os.rename(source, tombstone)
    (tombstone / child_name).unlink()
    outside = tmp_path / f"outside-{child_name.replace('.', 'x')}"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_bytes(b"protected")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tombstone / child_name), str(outside)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        storage.delete_owned_artifact(ownership)

    assert protected.read_bytes() == b"protected"


def test_save_with_key_fsync_failure_removes_partial_object(tmp_path: Path, monkeypatch):
    storage = LocalStorage(tmp_path / "objects")
    key = "document.json"
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))

    with pytest.raises(OSError, match="fsync"):
        storage.save_with_key(key, BytesIO(b"payload"))

    assert not (storage.root / key).exists()


def test_worker_payload_fsync_failure_never_transitions_ready(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
):
    from tests.jobs.test_orchestration import _persisted_job

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    calls = 0
    original_fsync = os.fsync

    def fail_second_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("payload fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_fsync)
    with pytest.raises(OSError, match="payload fsync failed"):
        job_tasks._reserve_write_artifact(
            app_session_factory,
            LocalStorage(tmp_path / "objects"),
            step_ids["rule_text"],
            "manifest",
            ".json",
            b"{}",
        )

    with app_session_factory() as session:
        row = session.scalar(select(JobArtifact))
        assert row.status == "writing"
        assert row.size_bytes is None and row.sha256 is None


def test_worker_directory_fsync_failure_never_transitions_ready(
    app_session_factory, project, users, tmp_path: Path
):
    from tests.jobs.test_orchestration import _persisted_job

    class FailedDirectorySync(LocalStorage):
        def _fsync_directory(self, path):
            raise OSError("directory fsync failed")

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    with pytest.raises(OSError, match="directory fsync failed"):
        job_tasks._reserve_write_artifact(
            app_session_factory,
            FailedDirectorySync(tmp_path / "objects"),
            step_ids["rule_text"],
            "manifest",
            ".json",
            b"{}",
        )

    with app_session_factory() as session:
        row = session.scalar(select(JobArtifact))
        assert row.status == "writing"


def test_job_artifact_reserved_row_is_durable_before_storage_write(
    app_session_factory, project, users
):
    from tests.jobs.test_orchestration import _persisted_job

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        artifact = JobArtifact(
            step_id=step_ids["rule_text"],
            artifact_kind="manifest",
            owner_token="a" * 32,
            storage_key=_owned_key("a" * 32, ".json"),
            status="reserved",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id

    with app_session_factory() as session:
        persisted = session.get(JobArtifact, artifact_id)
        assert persisted is not None
        assert persisted.status == "reserved"
        assert persisted.size_bytes is None
        assert persisted.sha256 is None
        assert session.get(JobStep, persisted.step_id) is not None


def test_job_artifact_migration_is_separate_and_reversible() -> None:
    migration = (
        Path(__file__).parents[2] / "alembic/versions/0006_job_artifacts.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0005_step_leases"' in migration
    assert 'op.create_table(\n        "job_artifacts"' in migration
    assert "uq_job_artifact_storage_key" in migration
    assert "ck_job_artifact_status" in migration
    assert "reserved','writing','ready','referenced','expiring','expired','importing','import_cleanup','imported_cleanup','cleanup_failed','ownership_failed" in migration
    assert 'sa.Column("claimed_at", sa.DateTime(timezone=True))' in migration
    assert 'sa.Column("owner_token", sa.String(32), nullable=False)' in migration
    assert "writing" in migration
    assert "ownership_failed" in migration
    assert "uq_job_artifact_owner_token" in migration
    assert "ck_job_artifact_storage_key" in migration
    assert "prevent_job_artifact_owner_change" in migration
    assert 'server_default="1"' in migration
    assert 'sa.CheckConstraint("version >= 1", name="ck_job_artifact_version")' in migration
    assert "size_bytes IS NOT NULL" in migration
    assert "sha256 IS NOT NULL" in migration
    assert "claim_token IS NOT NULL AND claimed_at IS NOT NULL" in migration
    assert "claim_token IS NULL AND claimed_at IS NULL" in migration
    assert 'op.drop_table("job_artifacts")' in migration


def test_job_artifact_owner_token_is_database_immutable(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, _storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with pytest.raises(IntegrityError, match="job artifact owner is immutable"):
        with app_session_factory.begin() as session:
            row = session.scalar(
                select(JobArtifact).where(JobArtifact.step_id == step_id)
            )
            session.execute(
                update(JobArtifact)
                .where(JobArtifact.id == row.id)
                .values(
                    owner_token="7" * 32,
                    storage_key=_owned_key("7" * 32, ".json"),
                )
            )


def _referenced_artifacts(app_session_factory, project, users, tmp_path: Path):
    from tests.jobs.test_orchestration import _persisted_job

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        step = session.get(JobStep, step_ids["rule_text"])
        context = AdapterContext(
            work_dir=tmp_path / f"route-{uuid4().hex}",
            step_id=step.id,
            document_version_id=step.document_job.document_version_id,
            execution_idempotency_key=step.idempotency_key,
        )
    result = write_route_output(
        context,
        "rule_text",
        [{"subject": "A", "property": "p", "value": "1", "evidence_text": "e"}],
        lambda _event: None,
    )
    storage = LocalStorage(tmp_path / "objects")
    structured = job_tasks._structured_result(
        context, result, storage, app_session_factory
    )
    with app_session_factory.begin() as session:
        job_tasks._mark_artifacts_referenced(session, context.step_id, structured)
        step = session.get(JobStep, context.step_id)
        step.result_json = structured
    return context.step_id, structured, storage


def test_worker_artifacts_transition_ready_to_referenced_with_exact_result_refs(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )

    assert set(result["artifacts"]) == {"manifest", "candidate_tsv"}
    assert all(
        set(reference) == {"id", "key", "size", "sha256"}
        for reference in result["artifacts"].values()
    )
    with app_session_factory() as session:
        rows = tuple(
            session.scalars(
                select(JobArtifact).where(JobArtifact.step_id == step_id)
            )
        )
    assert len(rows) == 2
    assert {row.status for row in rows} == {"referenced"}
    assert all((storage.root / row.storage_key).is_file() for row in rows)


def test_storage_failure_leaves_committed_reserved_row_for_sweeper(
    app_session_factory, project, users, tmp_path: Path
):
    from tests.jobs.test_orchestration import _persisted_job

    class FailedWrite(LocalStorage):
        def save_with_key(self, key, stream):
            raise OSError("storage unavailable")

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)

    with pytest.raises(OSError, match="storage unavailable"):
        job_tasks._reserve_write_artifact(
            app_session_factory,
            FailedWrite(tmp_path / "objects"),
            step_ids["rule_text"],
            "manifest",
            ".json",
            b"{}",
        )

    with app_session_factory() as session:
        row = session.scalar(
            select(JobArtifact).where(
                JobArtifact.step_id == step_ids["rule_text"]
            )
        )
        assert row.status == "writing"
        assert row.size_bytes is None


def test_artifact_collision_terminal_row_never_authorizes_sweeper_delete(
    app_session_factory, project, users, tmp_path: Path
):
    from tests.jobs.test_orchestration import _persisted_job

    class CollidingStorage(LocalStorage):
        foreign_payload: Path | None = None

        def create_artifact_ownership(self, ownership):
            container = self.root / "artifacts" / ownership.owner_token
            container.mkdir(parents=True)
            (container / ".owner.json").write_bytes(b"foreign-marker")
            self.foreign_payload = self.root / ownership.storage_key
            self.foreign_payload.write_bytes(b"owned-by-another-writer")
            raise StorageCollisionError("storage key collision")

    storage = CollidingStorage(tmp_path / "objects")
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)

    with pytest.raises(StorageCollisionError, match="storage key collision"):
        job_tasks._reserve_write_artifact(
            app_session_factory,
            storage,
            step_ids["rule_text"],
            "manifest",
            ".json",
            b"ours",
        )
    with app_session_factory() as session:
        row = session.scalar(select(JobArtifact))
        assert row.status == "ownership_failed"
        assert row.size_bytes is None and row.sha256 is None

    job_tasks.sweep_job_artifacts(
        app_session_factory,
        storage,
        now=datetime.now(UTC) + timedelta(hours=1),
        grace=timedelta(0),
    )
    assert storage.foreign_payload is not None
    assert storage.foreign_payload.read_bytes() == b"owned-by-another-writer"


def test_commit_unknown_is_resolved_from_database_and_not_swept(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )

    # A client-side error after commit is resolved by reading durable state.
    with app_session_factory() as session:
        persisted = session.get(JobStep, step_id)
        assert persisted.result_json == result
        assert set(
            session.scalars(
                select(JobArtifact.status).where(JobArtifact.step_id == step_id)
            )
        ) == {"referenced"}
    assert job_tasks.sweep_job_artifacts(
        app_session_factory,
        storage,
        now=datetime.now(UTC),
        grace=timedelta(0),
    ) == 0


def test_stale_reserved_expires_without_deleting_unowned_existing_key(
    app_session_factory, project, users, tmp_path: Path
):
    from tests.jobs.test_orchestration import _persisted_job

    storage = LocalStorage(tmp_path / "objects")
    owner_token = "b" * 32
    key = _owned_key(owner_token, ".json")
    target = storage.root / key
    target.parent.mkdir(parents=True)
    target.write_bytes(b"pre-existing-owner")
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        row = JobArtifact(
            step_id=step_ids["rule_text"],
            artifact_kind="manifest",
            owner_token=owner_token,
            storage_key=key,
            status="reserved",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(row)
        session.flush()
        row.created_at = datetime.now(UTC) - timedelta(hours=1)
        artifact_id = row.id

    assert job_tasks.sweep_job_artifacts(
        app_session_factory,
        storage,
        now=datetime.now(UTC),
        grace=timedelta(0),
    ) == 0
    with app_session_factory() as session:
        row = session.get(JobArtifact, artifact_id)
        assert row.status == "expired"
        assert row.size_bytes is None and row.sha256 is None
        assert row.claim_token is None and row.claimed_at is None
    assert target.read_bytes() == b"pre-existing-owner"


@pytest.mark.parametrize("marker_state", ["missing", "mismatch"])
def test_stale_writing_invalid_marker_becomes_ownership_failed_without_delete(
    app_session_factory, project, users, tmp_path: Path, marker_state: str
):
    from tests.jobs.test_orchestration import _persisted_job

    storage = LocalStorage(tmp_path / "objects")
    token = "6" * 32
    key = _owned_key(token, ".json")
    container = storage.root / "artifacts" / token
    container.mkdir(parents=True)
    payload = storage.root / key
    payload.write_bytes(b"foreign-payload")
    if marker_state == "mismatch":
        (container / ".owner.json").write_bytes(b"{}")
    old = datetime.now(UTC) - timedelta(hours=1)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        row = JobArtifact(
            step_id=step_ids["rule_text"],
            artifact_kind="manifest",
            owner_token=token,
            storage_key=key,
            status="writing",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(row)
        session.flush()
        row.updated_at = old
        artifact_id = row.id

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), grace=timedelta(0)
    ) == 0
    with app_session_factory() as session:
        persisted = session.get(JobArtifact, artifact_id)
        assert persisted.status == "ownership_failed"
        assert persisted.claim_token is None and persisted.claimed_at is None
    tombstone_payload = (
        storage.root / "artifacts" / ".cleanup" / token / "payload.json"
    )
    assert tombstone_payload.read_bytes() == b"foreign-payload"


def test_sweeper_winning_writer_race_prevents_ready_transition(
    app_session_factory, project, users, tmp_path: Path
):
    from tests.jobs.test_orchestration import _persisted_job

    class SweepBeforePayload(LocalStorage):
        def save_owned_artifact(self, ownership, stream):
            job_tasks.sweep_job_artifacts(
                app_session_factory,
                self,
                now=datetime.now(UTC) + timedelta(hours=1),
                grace=timedelta(0),
            )
            return super().save_owned_artifact(ownership, stream)

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    storage = SweepBeforePayload(tmp_path / "objects")

    with pytest.raises(StorageOwnershipError, match="artifact ownership is invalid"):
        job_tasks._reserve_write_artifact(
            app_session_factory,
            storage,
            step_ids["rule_text"],
            "manifest",
            ".json",
            b"{}",
        )

    with app_session_factory() as session:
        row = session.scalar(select(JobArtifact))
        assert row.status == "expired"
        assert row.size_bytes is None and row.sha256 is None
    assert not (storage.root / row.storage_key).exists()


@pytest.mark.parametrize("initial", ["reserved", "ready"])
def test_crash_before_reference_is_eventually_swept(
    app_session_factory, project, users, tmp_path: Path, initial: str
):
    from tests.jobs.test_orchestration import _persisted_job

    storage = LocalStorage(tmp_path / "objects")
    owner_token = "9" * 32
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        row = JobArtifact(
            step_id=step_ids["rule_text"],
            artifact_kind="manifest",
            owner_token=owner_token,
            storage_key=_owned_key(owner_token, ".json"),
            status="reserved",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(row)
        session.flush()
        artifact_id = row.id
    if initial == "ready":
        ownership = ArtifactOwnership(artifact_id, owner_token, row.storage_key)
        storage.create_artifact_ownership(ownership)
        stored = storage.save_owned_artifact(ownership, BytesIO(b"{}"))
        with app_session_factory.begin() as session:
            persisted = session.get(JobArtifact, artifact_id)
            persisted.status = "ready"
            persisted.size_bytes = stored.size_bytes
            persisted.sha256 = stored.sha256
    with app_session_factory.begin() as session:
        persisted = session.get(JobArtifact, artifact_id)
        persisted.created_at = datetime.now(UTC) - timedelta(hours=1)

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), grace=timedelta(0)
    ) == (0 if initial == "reserved" else 1)
    with app_session_factory() as session:
        assert session.get(JobArtifact, artifact_id).status == "expired"
    assert not (storage.root / row.storage_key).exists()


def test_delete_failure_is_retryable_cleanup_failed(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        rows = tuple(
            session.scalars(select(JobArtifact).where(JobArtifact.step_id == step_id))
        )
        rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        artifact_id = rows[0].id

    class FailedDelete(LocalStorage):
        def delete_owned_artifact(self, ownership):
            raise OSError("delete failed")

    assert job_tasks.sweep_job_artifacts(
        app_session_factory,
        FailedDelete(storage.root),
        now=datetime.now(UTC),
        limit=1,
    ) == 0
    with app_session_factory() as session:
        assert session.get(JobArtifact, artifact_id).status == "cleanup_failed"

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=1
    ) == 1
    with app_session_factory() as session:
        assert session.get(JobArtifact, artifact_id).status == "expired"


def test_cleanup_kill_after_empty_tombstone_retries_to_terminal_and_finalizes(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        row = session.scalar(select(JobArtifact).where(JobArtifact.step_id == step_id))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        artifact_id, token = row.id, row.owner_token

    class KillAfterCleanup(LocalStorage):
        def delete_owned_artifact(self, ownership):
            super().delete_owned_artifact(ownership)
            raise OSError("killed before db cas")

    job_tasks.sweep_job_artifacts(
        app_session_factory, KillAfterCleanup(storage.root),
        now=datetime.now(UTC), limit=1
    )
    tombstone = storage.root / "artifacts" / ".cleanup" / token
    with app_session_factory() as session:
        assert session.get(JobArtifact, artifact_id).status == "cleanup_failed"
    assert tombstone.is_dir() and list(tombstone.iterdir()) == []

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=1
    ) == 1
    with app_session_factory() as session:
        assert session.get(JobArtifact, artifact_id).status == "expired"
    assert not tombstone.exists()


def test_terminal_finalize_kill_is_recovered_by_next_sweep(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        row = session.scalar(select(JobArtifact).where(JobArtifact.step_id == step_id))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        artifact_id, token = row.id, row.owner_token

    class KillFinalize(LocalStorage):
        def finalize_owned_artifact_cleanup(self, ownership):
            raise OSError("killed after db terminal")

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, KillFinalize(storage.root),
        now=datetime.now(UTC), limit=1
    ) == 1
    tombstone = storage.root / "artifacts" / ".cleanup" / token
    with app_session_factory() as session:
        assert session.get(JobArtifact, artifact_id).status == "expired"
    assert tombstone.is_dir() and list(tombstone.iterdir()) == []

    job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=1
    )
    assert not tombstone.exists()


def test_claim_tokens_prevent_two_workers_claiming_same_artifact(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, _storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        for row in session.scalars(
            select(JobArtifact).where(JobArtifact.step_id == step_id)
        ):
            row.expires_at = now - timedelta(seconds=1)

    first = job_tasks._claim_artifact_cleanup(
        app_session_factory, now=now, grace=timedelta(0)
    )
    second = job_tasks._claim_artifact_cleanup(
        app_session_factory, now=now, grace=timedelta(0)
    )
    assert first is not None and second is not None
    assert first[0] != second[0]
    assert first[1] != second[1]


def test_stale_expiring_claim_is_recovered_after_worker_crash(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        row = session.scalar(
            select(JobArtifact).where(JobArtifact.step_id == step_id)
        )
        row.status = "expiring"
        row.claim_token = "expire-abandoned-worker"
        row.claimed_at = now - timedelta(minutes=10)
        row.updated_at = now - timedelta(minutes=10)
        artifact_id = row.id

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=now, limit=1
    ) == 1
    with app_session_factory() as session:
        row = session.get(JobArtifact, artifact_id)
        assert row.status == "expired"
        assert row.claim_token is None


def test_import_claim_abort_complete_and_result_json_is_immutable(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    before = __import__("copy").deepcopy(result)

    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        assert len(claim.artifact_ids) == 2
    assert job_tasks.sweep_job_artifacts(
        app_session_factory,
        storage,
        now=datetime.now(UTC),
    ) == 0
    with app_session_factory.begin() as session:
        assert job_tasks.abort_artifact_import(session, claim) == 2
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
    assert job_tasks.complete_artifact_import(
        app_session_factory, claim, storage
    ) == 2
    with app_session_factory() as session:
        step = session.get(JobStep, step_id)
        assert step.result_json == before
        assert set(
            session.scalars(
                select(JobArtifact.status).where(JobArtifact.step_id == step_id)
            )
        ) == {"imported_cleanup"}


def test_import_claim_uses_only_exact_result_artifacts_and_ignores_history(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, result, _storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        session.add_all(
            [
            JobArtifact(
                step_id=step_id,
                artifact_kind="manifest",
                owner_token="f" * 32,
                storage_key=_owned_key("f" * 32, ".json"),
                status="reserved",
                expires_at=now + timedelta(days=30),
            ),
            JobArtifact(
                step_id=step_id,
                artifact_kind="manifest",
                owner_token="e" * 32,
                storage_key=_owned_key("e" * 32, ".json"),
                status="ready",
                size_bytes=1,
                sha256="e" * 64,
                expires_at=now + timedelta(days=30),
            ),
            JobArtifact(
                step_id=step_id,
                artifact_kind="candidate_tsv",
                owner_token="d" * 32,
                storage_key=_owned_key("d" * 32, ".tsv"),
                status="expired",
                size_bytes=1,
                sha256="d" * 64,
                expires_at=now - timedelta(days=1),
            ),
            ]
        )

    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id, now=now)

    expected_ids = {
        __import__("uuid").UUID(reference["id"])
        for reference in result["artifacts"].values()
    }
    assert set(claim.artifact_ids) == expected_ids
    assert claim.token.startswith("import-")
    assert claim.expires_at > now
    with app_session_factory() as session:
        rows = tuple(
            session.scalars(select(JobArtifact).where(JobArtifact.step_id == step_id))
        )
        assert {row.status for row in rows} == {
            "reserved",
            "ready",
            "expired",
            "importing",
        }
        assert {
            row.claim_token for row in rows if row.status == "importing"
        } == {claim.token}


@pytest.mark.parametrize(
    "corruption", ["id", "key", "kind", "size", "sha256", "status", "step"]
)
def test_import_claim_rejects_result_and_row_identity_mismatch(
    app_session_factory, project, users, tmp_path: Path, corruption: str
):
    step_id, result, _storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    manifest_id = __import__("uuid").UUID(result["artifacts"]["manifest"]["id"])
    with app_session_factory.begin() as session:
        row = session.get(JobArtifact, manifest_id)
        if corruption == "id":
            result["artifacts"]["manifest"]["id"] = str(uuid4())
            session.get(JobStep, step_id).result_json = result
        elif corruption == "key":
            result["artifacts"]["manifest"]["key"] = "artifact-wrong.json"
            session.get(JobStep, step_id).result_json = result
        elif corruption == "kind":
            row.artifact_kind = "candidate_tsv"
        elif corruption == "size":
            row.size_bytes += 1
        elif corruption == "sha256":
            row.sha256 = "0" * 64
        elif corruption == "status":
            row.status = "ready"
        else:
            from tests.jobs.test_orchestration import _persisted_job

            _, other_steps = _persisted_job(
                session, project.id, users["operator"].id
            )
            row.step_id = other_steps["rule_text"]

    with app_session_factory.begin() as session:
        with pytest.raises(
            job_tasks.PermanentAdapterError, match="job artifacts are not importable"
        ):
            job_tasks.claim_artifacts_for_import(session, step_id)


def test_import_abort_and_complete_reject_wrong_owner_token(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
    wrong = replace(claim, token="import-wrong-owner")

    with app_session_factory.begin() as session:
        with pytest.raises(
            job_tasks.PermanentAdapterError, match="artifact import claim is invalid"
        ):
            job_tasks.abort_artifact_import(session, wrong)
    with pytest.raises(
        job_tasks.PermanentAdapterError, match="artifact import claim is invalid"
    ):
        job_tasks.complete_artifact_import(app_session_factory, wrong, storage)

    with app_session_factory() as session:
        assert set(
            session.scalars(
                select(JobArtifact.status).where(JobArtifact.step_id == step_id)
            )
        ) == {"importing"}
    assert all(
        (storage.root / reference["key"]).is_file()
        for reference in _result["artifacts"].values()
    )


def test_complete_deletes_outside_transaction_and_is_idempotent(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)

    class ObserveCommittedClaim(LocalStorage):
        def delete_owned_artifact(self, ownership):
            with app_session_factory() as session:
                assert set(
                    session.scalars(
                        select(JobArtifact.status).where(
                            JobArtifact.id.in_(claim.artifact_ids)
                        )
                    )
                ) <= {"import_cleanup", "cleanup_failed", "imported_cleanup"}
            return super().delete_owned_artifact(ownership)

    observed = ObserveCommittedClaim(storage.root)
    assert job_tasks.complete_artifact_import(
        app_session_factory, claim, observed
    ) == 2
    assert job_tasks.complete_artifact_import(
        app_session_factory, claim, observed
    ) == 2


def test_import_delete_failure_retains_claim_and_retries(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)

    class FailOneDelete(LocalStorage):
        failed = False

        def delete_owned_artifact(self, ownership):
            if not self.failed:
                self.failed = True
                raise OSError("delete failed")
            return super().delete_owned_artifact(ownership)

    assert job_tasks.complete_artifact_import(
        app_session_factory, claim, FailOneDelete(storage.root)
    ) == 1
    with app_session_factory() as session:
        failed = session.scalar(
            select(JobArtifact).where(JobArtifact.status == "cleanup_failed")
        )
        assert failed.claim_token == claim.token
        assert failed.claimed_at is not None

    assert job_tasks.complete_artifact_import(
        app_session_factory, claim, storage
    ) == 2
    with app_session_factory() as session:
        assert set(
            session.scalars(
                select(JobArtifact.status).where(JobArtifact.step_id == step_id)
            )
        ) == {"imported_cleanup"}


@pytest.mark.parametrize("objects_exist", [True, False])
def test_sweeper_recovers_stale_importing_by_storage_state(
    app_session_factory, project, users, tmp_path: Path, objects_exist: bool
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        for row in session.scalars(
            select(JobArtifact).where(JobArtifact.id.in_(claim.artifact_ids))
        ):
            row.claimed_at = datetime.now(UTC) - timedelta(minutes=10)
    if not objects_exist:
        for reference in result["artifacts"].values():
            storage.delete(reference["key"])

    job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=10
    )

    expected = "referenced" if objects_exist else "imported_cleanup"
    with app_session_factory() as session:
        rows = tuple(
            session.scalars(
                select(JobArtifact).where(JobArtifact.id.in_(claim.artifact_ids))
            )
        )
        assert {row.status for row in rows} == {expected}
        assert all(row.claim_token is None and row.claimed_at is None for row in rows)


def test_stale_importing_missing_payload_runs_owned_cleanup_without_marker_leak(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        rows = tuple(
            session.scalars(
                select(JobArtifact).where(JobArtifact.id.in_(claim.artifact_ids))
            )
        )
        for row in rows:
            row.claimed_at = datetime.now(UTC) - timedelta(minutes=10)
    missing = rows[0]
    (storage.root / missing.storage_key).unlink()

    job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=10
    )

    with app_session_factory() as session:
        persisted = session.get(JobArtifact, missing.id)
        assert persisted.status == "imported_cleanup"
    assert not (storage.root / "artifacts" / missing.owner_token).exists()
    assert not (
        storage.root / "artifacts" / ".cleanup" / missing.owner_token
    ).exists()


def test_stale_importing_missing_payload_cleanup_failure_is_retryable(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        rows = tuple(
            session.scalars(
                select(JobArtifact).where(JobArtifact.id.in_(claim.artifact_ids))
            )
        )
        for row in rows:
            row.claimed_at = now - timedelta(minutes=10)
    missing = rows[0]
    (storage.root / missing.storage_key).unlink()

    class FailedCleanup(LocalStorage):
        def delete_owned_artifact(self, ownership):
            if ownership.artifact_id == missing.id:
                raise OSError("cleanup failed")
            return super().delete_owned_artifact(ownership)

    job_tasks.sweep_job_artifacts(
        app_session_factory, FailedCleanup(storage.root), now=now, limit=10
    )
    with app_session_factory() as session:
        persisted = session.get(JobArtifact, missing.id)
        assert persisted.status == "cleanup_failed"
        assert persisted.claim_token.startswith("import-recovery-")

    job_tasks.sweep_job_artifacts(
        app_session_factory,
        storage,
        now=now + timedelta(minutes=3),
        limit=10,
    )
    with app_session_factory() as session:
        assert session.get(JobArtifact, missing.id).status == "imported_cleanup"


def test_stale_importing_empty_tombstone_completes_after_db_cas_kill(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        row = session.get(JobArtifact, claim.artifact_ids[0])
        row.claimed_at = datetime.now(UTC) - timedelta(minutes=10)
        ownership = ArtifactOwnership(row.id, row.owner_token, row.storage_key)
    storage.delete_owned_artifact(ownership)
    tombstone = storage.root / "artifacts" / ".cleanup" / ownership.owner_token

    job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=10
    )

    with app_session_factory() as session:
        assert session.get(JobArtifact, ownership.artifact_id).status == "imported_cleanup"
    assert not tombstone.exists()


@pytest.mark.parametrize("initial", ["import_cleanup", "cleanup_failed"])
def test_sweeper_finishes_stale_irreversible_import_cleanup(
    app_session_factory, project, users, tmp_path: Path, initial: str
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    old = datetime.now(UTC) - timedelta(minutes=10)
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        session.execute(
            __import__("sqlalchemy").update(JobArtifact)
            .where(JobArtifact.id.in_(claim.artifact_ids))
            .values(status=initial, claimed_at=old)
        )

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, storage, now=datetime.now(UTC), limit=10
    ) == 2
    with app_session_factory() as session:
        rows = tuple(
            session.scalars(
                select(JobArtifact).where(JobArtifact.id.in_(claim.artifact_ids))
            )
        )
        assert {row.status for row in rows} == {"imported_cleanup"}
        assert all(row.claim_token is None and row.claimed_at is None for row in rows)
    assert all(
        not (storage.root / reference["key"]).exists()
        for reference in result["artifacts"].values()
    )


def test_stale_import_cleanup_failure_can_be_taken_over_again(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, _result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
        session.execute(
            __import__("sqlalchemy").update(JobArtifact)
            .where(JobArtifact.id.in_(claim.artifact_ids))
            .values(
                status="import_cleanup",
                claimed_at=now - timedelta(minutes=10),
            )
        )

    class FailedDelete(LocalStorage):
        def delete_owned_artifact(self, ownership):
            raise OSError("delete failed")

    assert job_tasks.sweep_job_artifacts(
        app_session_factory, FailedDelete(storage.root), now=now, limit=10
    ) == 0
    with app_session_factory() as session:
        failed = tuple(
            session.scalars(
                select(JobArtifact).where(JobArtifact.id.in_(claim.artifact_ids))
            )
        )
        assert {row.status for row in failed} == {"cleanup_failed"}
        assert all(
            row.claim_token.startswith("import-cleanup-recovery-")
            for row in failed
        )

    assert job_tasks.sweep_job_artifacts(
        app_session_factory,
        storage,
        now=now + timedelta(minutes=3),
        limit=10,
    ) == 2


def test_fenced_old_complete_cannot_delete_reopened_artifacts(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    with app_session_factory.begin() as session:
        claim = job_tasks.claim_artifacts_for_import(session, step_id)
    deleted: list[str] = []

    class TrackDelete(LocalStorage):
        def delete_owned_artifact(self, ownership):
            deleted.append(ownership.storage_key)
            return super().delete_owned_artifact(ownership)

    original_fence = job_tasks._fence_import_cleanup

    def stale_recovery_wins(factory, stale_claim, *, now):
        with factory.begin() as session:
            session.execute(
                __import__("sqlalchemy").update(JobArtifact)
                .where(JobArtifact.id.in_(stale_claim.artifact_ids))
                .values(claimed_at=now - timedelta(minutes=10))
            )
        job_tasks.sweep_job_artifacts(
            factory, storage, now=now, limit=10
        )
        return original_fence(factory, stale_claim, now=now)

    monkeypatch.setattr(job_tasks, "_fence_import_cleanup", stale_recovery_wins)
    with pytest.raises(
        job_tasks.PermanentAdapterError, match="artifact import claim is invalid"
    ):
        job_tasks.complete_artifact_import(
            app_session_factory, claim, TrackDelete(storage.root)
        )

    assert deleted == []
    with app_session_factory() as session:
        assert set(
            session.scalars(
                select(JobArtifact.status).where(JobArtifact.id.in_(claim.artifact_ids))
            )
        ) == {"referenced"}
    assert all(
        (storage.root / reference["key"]).is_file()
        for reference in result["artifacts"].values()
    )


def test_restore_accepts_referenced_and_importing_but_rejects_ready(
    app_session_factory, project, users, tmp_path: Path
):
    step_id, result, storage = _referenced_artifacts(
        app_session_factory, project, users, tmp_path
    )
    assert len(
        job_tasks._restore_route_artifacts(
            tmp_path / "merge-referenced", [result], storage, app_session_factory
        )
    ) == 1
    with app_session_factory.begin() as session:
        job_tasks.claim_artifacts_for_import(session, step_id)
    assert len(
        job_tasks._restore_route_artifacts(
            tmp_path / "merge-importing", [result], storage, app_session_factory
        )
    ) == 1
    with app_session_factory.begin() as session:
        session.execute(
            __import__("sqlalchemy").update(JobArtifact)
            .where(JobArtifact.step_id == step_id)
            .values(status="ready", claim_token=None, claimed_at=None)
        )
    with pytest.raises(job_tasks.PermanentAdapterError):
        job_tasks._restore_route_artifacts(
            tmp_path / "merge-ready", [result], storage, app_session_factory
        )
