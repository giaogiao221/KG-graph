from io import BytesIO
from uuid import uuid4

import pytest

from app.documents.storage import ExportOwnership, LocalStorage, StorageOwnershipError


class Broken(BytesIO):
    def read(self, *_args):
        if self.tell(): raise OSError("half write")
        return super().read(2)


def test_export_storage_atomically_publishes_and_cleans_half_write(tmp_path):
    storage = LocalStorage(tmp_path / "objects")
    export_id = uuid4(); token = uuid4().hex
    ownership = ExportOwnership(export_id, token, storage.export_storage_key(token, ".tsv"))
    storage.create_export_ownership(ownership)
    with pytest.raises(OSError): storage.save_owned_export(ownership, Broken(b"abcdef"))
    container = storage.root / "exports" / token
    assert not (container / "payload.tsv").exists()
    assert not list(container.glob("*.tmp"))
    stored = storage.save_owned_export(ownership, BytesIO(b"ok"))
    assert stored.storage_key == ownership.storage_key
    with pytest.raises(Exception): storage.save_owned_export(ownership, BytesIO(b"race"))


def test_export_storage_rejects_marker_tamper_and_symlink(tmp_path):
    storage = LocalStorage(tmp_path / "objects")
    export_id = uuid4(); token = uuid4().hex
    ownership = ExportOwnership(export_id, token, storage.export_storage_key(token, ".json"))
    storage.create_export_ownership(ownership)
    marker = storage.root / "exports" / token / ".owner.json"
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(StorageOwnershipError): storage.save_owned_export(ownership, BytesIO(b"[]"))
    marker.unlink()
    try: marker.symlink_to(tmp_path / "elsewhere")
    except OSError: pytest.skip("symlink unavailable")
    with pytest.raises(StorageOwnershipError): storage.open_owned_export(ownership)
