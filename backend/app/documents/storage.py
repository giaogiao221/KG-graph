from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Lock
from typing import BinaryIO, Protocol
from uuid import UUID, uuid4

from app.core.settings import get_settings


@dataclass(frozen=True, slots=True)
class StoredObject:
    storage_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactOwnership:
    artifact_id: UUID
    owner_token: str
    storage_key: str


@dataclass(frozen=True, slots=True)
class ExportOwnership:
    export_id: UUID
    owner_token: str
    storage_key: str


class UploadTooLargeError(ValueError):
    pass


class StorageCompensationError(RuntimeError):
    pass


class StorageCollisionError(RuntimeError):
    pass


class StorageOwnershipError(RuntimeError):
    pass


class Storage(Protocol):
    def save(self, stream: BinaryIO, suffix: str) -> StoredObject: ...

    def reserve_key(self, suffix: str) -> str: ...

    def save_with_key(self, key: str, stream: BinaryIO) -> StoredObject: ...

    def artifact_storage_key(self, owner_token: str, suffix: str) -> str: ...

    def create_artifact_ownership(self, ownership: ArtifactOwnership) -> None: ...

    def verify_artifact_ownership(self, ownership: ArtifactOwnership) -> None: ...

    def save_owned_artifact(
        self, ownership: ArtifactOwnership, stream: BinaryIO
    ) -> StoredObject: ...

    def delete_owned_artifact(self, ownership: ArtifactOwnership) -> None: ...

    def finalize_owned_artifact_cleanup(
        self, ownership: ArtifactOwnership
    ) -> None: ...

    def delete(self, key: str) -> None: ...

    def open_read(self, key: str) -> BinaryIO: ...

    def copy_to(
        self,
        key: str,
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str,
        max_bytes: int,
    ) -> None: ...

    def record_pending_cleanup(self, key: str, reason: str) -> None: ...

    def retry_pending_cleanup(self) -> int: ...

    def export_storage_key(self, owner_token: str, suffix: str) -> str: ...

    def create_export_ownership(self, ownership: ExportOwnership) -> None: ...

    def verify_export_ownership(self, ownership: ExportOwnership) -> None: ...

    def save_owned_export(self, ownership: ExportOwnership, stream: BinaryIO) -> StoredObject: ...

    def open_owned_export(self, ownership: ExportOwnership) -> BinaryIO: ...

    def delete_owned_export(self, ownership: ExportOwnership) -> None: ...

    def cleanup_owned_export(self, ownership: ExportOwnership) -> None: ...



def safe_object_path(root: Path, key: str) -> Path:
    posix, windows = PurePosixPath(key), PureWindowsPath(key)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("storage path escapes root")
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if not parts or ".." in parts or ".." in windows.parts:
        raise ValueError("storage path escapes root")
    absolute_root = root.absolute()
    target = absolute_root.joinpath(*parts)
    current = absolute_root
    try:
        for component in (absolute_root, *(absolute_root.joinpath(*parts[:index]) for index in range(1, len(parts) + 1))):
            current = component
            if not current.exists() and not current.is_symlink():
                continue
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("storage path contains link")
    except OSError:
        raise ValueError("storage path is invalid") from None
    return target


def _safe_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,15}", suffix):
        return ""
    return suffix


class LocalStorage:
    _journal_name = ".pending-cleanup.jsonl"

    def __init__(self, root: Path, max_bytes: int = 25 * 1024 * 1024):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.root = root.absolute()
        self.max_bytes = max_bytes
        self._journal_lock = Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._artifact_root = safe_object_path(self.root, "artifacts")
        self._cleanup_root = safe_object_path(self.root, "artifacts/.cleanup")

    def save(self, stream: BinaryIO, suffix: str) -> StoredObject:
        storage_key = self._reserve_unique_key("", suffix)
        return self.save_with_key(storage_key, stream)

    def reserve_key(self, suffix: str) -> str:
        return self._reserve_unique_key("artifact-", suffix)

    def artifact_storage_key(self, owner_token: str, suffix: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", owner_token):
            raise ValueError("artifact ownership is invalid")
        safe_suffix = _safe_suffix(suffix)
        if not safe_suffix:
            raise ValueError("artifact ownership is invalid")
        key = f"artifacts/{owner_token}/payload{safe_suffix}"
        safe_object_path(self.root, key)
        return key

    def export_storage_key(self, owner_token: str, suffix: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", owner_token):
            raise ValueError("export ownership is invalid")
        safe_suffix = _safe_suffix(suffix)
        if not safe_suffix:
            raise ValueError("export ownership is invalid")
        key = f"exports/{owner_token}/payload{safe_suffix}"
        safe_object_path(self.root, key)
        return key

    @staticmethod
    def _export_marker_bytes(ownership: ExportOwnership) -> bytes:
        return json.dumps({
            "export_id": str(ownership.export_id),
            "owner_token": ownership.owner_token,
            "schema": "export-owner-v1",
            "storage_key": ownership.storage_key,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _export_paths(self, ownership: ExportOwnership) -> tuple[Path, Path, Path]:
        prefix = f"exports/{ownership.owner_token}/payload."
        if (
            not re.fullmatch(r"[0-9a-f]{32}", ownership.owner_token)
            or not ownership.storage_key.startswith(prefix)
            or PurePosixPath(ownership.storage_key).parent
            != PurePosixPath("exports") / ownership.owner_token
        ):
            raise StorageOwnershipError("export ownership is invalid")
        payload = safe_object_path(self.root, ownership.storage_key)
        container = payload.parent
        marker = safe_object_path(self.root, f"exports/{ownership.owner_token}/.owner.json")
        return container, marker, payload

    def create_export_ownership(self, ownership: ExportOwnership) -> None:
        container, marker, _payload = self._export_paths(ownership)
        export_root = safe_object_path(self.root, "exports")
        try:
            created_root = not export_root.exists()
            export_root.mkdir(exist_ok=True)
            if created_root: self._fsync_directory(self.root)
            container.mkdir()
            with marker.open("xb") as handle:
                handle.write(self._export_marker_bytes(ownership)); handle.flush(); os.fsync(handle.fileno())
            self._fsync_directory(container); self._fsync_directory(export_root)
        except FileExistsError:
            raise StorageCollisionError("storage key collision") from None
        except Exception:
            try: marker.unlink(missing_ok=True); container.rmdir()
            except OSError: pass
            raise

    def verify_export_ownership(self, ownership: ExportOwnership) -> None:
        try:
            _container, marker, _payload = self._export_paths(ownership)
            expected = self._export_marker_bytes(ownership)
            info = marker.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                    or info.st_size != len(expected) or marker.read_bytes() != expected):
                raise ValueError
        except (OSError, ValueError):
            raise StorageOwnershipError("export ownership is invalid") from None

    def save_owned_export(self, ownership: ExportOwnership, stream: BinaryIO) -> StoredObject:
        container, _marker, payload = self._export_paths(ownership)
        self.verify_export_ownership(ownership)
        temporary = safe_object_path(self.root, f"exports/{ownership.owner_token}/.payload-{uuid4().hex}.tmp")
        digest = hashlib.sha256(); size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes: raise UploadTooLargeError("upload exceeds configured size limit")
                    digest.update(chunk); output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            self._rename_directory_noreplace(temporary, payload)
            self._fsync_directory(container)
        except Exception:
            try: temporary.unlink(missing_ok=True)
            except OSError:
                try: self.record_pending_cleanup(temporary.relative_to(self.root).as_posix(), "cleanup_failed")
                except Exception: pass
            raise
        return StoredObject(ownership.storage_key, digest.hexdigest(), size)

    def open_owned_export(self, ownership: ExportOwnership) -> BinaryIO:
        self.verify_export_ownership(ownership)
        return self.open_read(ownership.storage_key)

    def delete_owned_export(self, ownership: ExportOwnership) -> None:
        try:
            self._delete_owned_export_once(ownership)
        except StorageOwnershipError:
            raise
        except OSError:
            raise StorageCompensationError("storage cleanup pending") from None

    def _delete_owned_export_once(self, ownership: ExportOwnership) -> None:
        container, marker, payload = self._export_paths(ownership)
        self.verify_export_ownership(ownership)
        for child in container.iterdir():
            if child.name in {marker.name,payload.name}:continue
            if re.fullmatch(r"\.payload-[0-9a-f]{32}\.tmp",child.name) and child.is_file() and not child.is_symlink():child.unlink()
            else:raise StorageOwnershipError("export ownership is invalid")
        payload.unlink(missing_ok=True); marker.unlink(); container.rmdir()
        self._fsync_directory(safe_object_path(self.root, "exports"))

    def cleanup_owned_export(self, ownership: ExportOwnership) -> None:
        container, _marker, _payload=self._export_paths(ownership)
        if not self._entry_exists(container):return
        self.delete_owned_export(ownership)


    @staticmethod
    def _marker_bytes(ownership: ArtifactOwnership) -> bytes:
        value = {
            "artifact_id": str(ownership.artifact_id),
            "owner_token": ownership.owner_token,
            "schema": "job-artifact-owner-v1",
            "storage_key": ownership.storage_key,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def _ownership_paths(
        self, ownership: ArtifactOwnership
    ) -> tuple[Path, Path, Path]:
        self._validate_ownership(ownership)
        payload = safe_object_path(self.root, ownership.storage_key)
        container = payload.parent
        marker = safe_object_path(
            self.root, f"artifacts/{ownership.owner_token}/.owner.json"
        )
        return container, marker, payload

    @staticmethod
    def _validate_ownership(ownership: ArtifactOwnership) -> None:
        expected_prefix = f"artifacts/{ownership.owner_token}/payload."
        if (
            not re.fullmatch(r"[0-9a-f]{32}", ownership.owner_token)
            or not ownership.storage_key.startswith(expected_prefix)
            or PurePosixPath(ownership.storage_key).parent
            != PurePosixPath("artifacts") / ownership.owner_token
        ):
            raise StorageOwnershipError("artifact ownership is invalid")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            handle = create_file(
                str(path),
                0x80000000,
                0x1 | 0x2 | 0x4,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            invalid = wintypes.HANDLE(-1).value
            if handle == invalid:
                raise ctypes.WinError()
            try:
                if not kernel32.FlushFileBuffers(handle):
                    error = ctypes.get_last_error()
                    if error not in {1, 5, 6, 50}:  # explicitly unsupported for dirs
                        raise ctypes.WinError(error)
            finally:
                kernel32.CloseHandle(handle)
            return
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(path, flags)
            os.fsync(descriptor)
        except OSError:
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def create_artifact_ownership(self, ownership: ArtifactOwnership) -> None:
        container, marker, _payload = self._ownership_paths(ownership)
        try:
            created_root = not self._artifact_root.exists()
            self._artifact_root.mkdir(exist_ok=True)
        except FileExistsError:
            raise StorageCollisionError("storage key collision") from None
        if created_root:
            self._fsync_directory(self.root)
        try:
            container.mkdir()
        except FileExistsError:
            raise StorageCollisionError("storage key collision") from None
        try:
            with marker.open("xb") as handle:
                handle.write(self._marker_bytes(ownership))
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_directory(container)
            self._fsync_directory(self._artifact_root)
        except FileExistsError:
            raise StorageCollisionError("storage key collision") from None
        except Exception:
            try:
                marker.unlink(missing_ok=True)
                container.rmdir()
            except OSError:
                pass
            raise

    def verify_artifact_ownership(self, ownership: ArtifactOwnership) -> None:
        _container, marker, _payload = self._ownership_paths(ownership)
        expected = self._marker_bytes(ownership)
        try:
            info = marker.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_size != len(expected)
                or marker.read_bytes() != expected
            ):
                raise ValueError
        except (OSError, ValueError):
            raise StorageOwnershipError("artifact ownership is invalid") from None

    def save_owned_artifact(
        self, ownership: ArtifactOwnership, stream: BinaryIO
    ) -> StoredObject:
        self.verify_artifact_ownership(ownership)
        return self.save_with_key(ownership.storage_key, stream)

    def delete_owned_artifact(self, ownership: ArtifactOwnership) -> None:
        self._validate_ownership(ownership)
        tombstone = self._tombstone_path(ownership)
        try:
            source_exists = self._entry_exists(
                self.root / "artifacts" / ownership.owner_token
            )
            tombstone_exists = self._entry_exists(tombstone)
            if source_exists and tombstone_exists:
                raise StorageOwnershipError("artifact ownership is invalid")
            if not tombstone_exists:
                container, _marker, _payload = self._ownership_paths(ownership)
                created_cleanup = not self._cleanup_root.exists()
                self._cleanup_root.mkdir(exist_ok=True)
                if created_cleanup:
                    self._fsync_directory(self._artifact_root)
                try:
                    self._rename_directory_noreplace(container, tombstone)
                except FileNotFoundError:
                    if not tombstone.exists():
                        raise StorageOwnershipError(
                            "artifact ownership is invalid"
                        ) from None
                except FileExistsError:
                    if not tombstone.exists():
                        raise
                self._fsync_directory(self._artifact_root)
                self._fsync_directory(self._cleanup_root)
            self._delete_tombstone(ownership, tombstone)
            self._fsync_directory(tombstone)
            self._fsync_directory(self._cleanup_root)
            self._fsync_directory(self._artifact_root)
        except StorageOwnershipError:
            raise
        except StorageCollisionError:
            raise
        except OSError:
            raise StorageCompensationError("storage cleanup pending") from None

    @staticmethod
    def _entry_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _tombstone_path(self, ownership: ArtifactOwnership) -> Path:
        try:
            cleanup_root = safe_object_path(self.root, "artifacts/.cleanup")
        except ValueError:
            raise StorageOwnershipError("artifact ownership is invalid") from None
        return cleanup_root / ownership.owner_token

    def finalize_owned_artifact_cleanup(
        self, ownership: ArtifactOwnership
    ) -> None:
        self._validate_ownership(ownership)
        tombstone = self._tombstone_path(ownership)
        if not self._entry_exists(tombstone):
            return
        try:
            if os.name == "nt":
                self._finalize_tombstone_windows(tombstone)
            else:
                self._finalize_tombstone_posix(ownership, tombstone)
            self._fsync_directory(self._cleanup_root)
            self._fsync_directory(self._artifact_root)
        except StorageOwnershipError:
            raise
        except OSError:
            raise StorageCompensationError("storage cleanup pending") from None

    @staticmethod
    def _rename_directory_noreplace(source: Path, target: Path) -> None:
        if os.name == "nt":
            os.rename(source, target)  # MoveFile semantics never replace a target.
            return
        import ctypes
        import errno

        libc = ctypes.CDLL(None, use_errno=True)
        source_bytes, target_bytes = os.fsencode(source), os.fsencode(target)
        if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
            if libc.renameat2(-100, source_bytes, -100, target_bytes, 1) == 0:
                return
        elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
            if libc.renamex_np(source_bytes, target_bytes, 4) == 0:
                return
        else:
            raise StorageCompensationError("storage cleanup pending")
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), target)
        raise OSError(error, os.strerror(error), source)

    def _delete_tombstone(
        self, ownership: ArtifactOwnership, tombstone: Path
    ) -> None:
        if os.name == "nt":
            self._delete_tombstone_windows(ownership, tombstone)
        else:
            self._delete_tombstone_posix(ownership, tombstone)

    def _delete_tombstone_posix(
        self, ownership: ArtifactOwnership, tombstone: Path
    ) -> None:
        parent_fd = directory_fd = marker_fd = -1
        try:
            parent_fd = os.open(
                self._cleanup_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            directory_fd = os.open(
                ownership.owner_token,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(directory_fd)
            names = os.listdir(directory_fd)
            if ".owner.json" not in names:
                if names:
                    raise StorageOwnershipError("artifact ownership is invalid")
                return
            marker_fd = os.open(
                ".owner.json",
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            marker_info = os.fstat(marker_fd)
            expected = self._marker_bytes(ownership)
            if not stat.S_ISREG(marker_info.st_mode):
                raise StorageOwnershipError("artifact ownership is invalid")
            actual = b""
            while chunk := os.read(marker_fd, 4096):
                actual += chunk
                if len(actual) > len(expected):
                    break
            if actual != expected:
                raise StorageOwnershipError("artifact ownership is invalid")
            payload_name = PurePosixPath(ownership.storage_key).name
            try:
                os.unlink(payload_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.unlink(".owner.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
            current = os.stat(
                ownership.owner_token, dir_fd=parent_fd, follow_symlinks=False
            )
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise StorageOwnershipError("artifact ownership is invalid")
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
            if directory_fd >= 0:
                os.close(directory_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def _delete_tombstone_windows(
        self, ownership: ArtifactOwnership, tombstone: Path
    ) -> None:
        api = self._windows_cleanup_api()
        directory_handle = api["open"](tombstone, directory=True, read=True)
        marker_handle = payload_handle = None
        try:
            api["reject_reparse"](directory_handle)
            marker = tombstone / ".owner.json"
            payload = tombstone / PurePosixPath(ownership.storage_key).name
            marker_handle = api["open"](
                marker, directory=False, read=True, missing_ok=True
            )
            if marker_handle is None:
                if any(tombstone.iterdir()):
                    raise StorageOwnershipError("artifact ownership is invalid")
                return
            api["reject_reparse"](marker_handle)
            if api["read"](marker_handle) != self._marker_bytes(ownership):
                raise StorageOwnershipError("artifact ownership is invalid")
            payload_handle = api["open"](
                payload, directory=False, read=False, missing_ok=True
            )
            if payload_handle is not None:
                api["reject_reparse"](payload_handle)
                api["dispose"](payload_handle)
                api["close"](payload_handle)
                payload_handle = None
            api["dispose"](marker_handle)
            api["close"](marker_handle)
            marker_handle = None
            if any(tombstone.iterdir()):
                raise StorageOwnershipError("artifact ownership is invalid")
        finally:
            if payload_handle is not None:
                api["close"](payload_handle)
            if marker_handle is not None:
                api["close"](marker_handle)
            api["close"](directory_handle)

    @staticmethod
    def _windows_cleanup_api() -> dict[str, object]:
        import ctypes
        from ctypes import wintypes

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            ]

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        get_info.restype = wintypes.BOOL
        get_size = kernel32.GetFileSizeEx
        get_size.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong))
        get_size.restype = wintypes.BOOL
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        read_file.restype = wintypes.BOOL
        set_info = kernel32.SetFileInformationByHandle
        set_info.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        set_info.restype = wintypes.BOOL
        invalid = wintypes.HANDLE(-1).value

        def open_handle(
            path: Path, *, directory: bool, read: bool, missing_ok: bool = False
        ):
            access = 0x00010000 | 0x80 | (0x80000000 if read else 0)
            # BACKUP_SEMANTICS lets us open a directory-shaped reparse point even
            # when it appears where a marker/payload file should be.
            flags = 0x00200000 | 0x02000000
            handle = create_file(str(path), access, 0x1 | 0x2, None, 3, flags, None)
            if handle == invalid:
                error = ctypes.get_last_error()
                if missing_ok and error in {2, 3}:
                    return None
                raise StorageCompensationError("storage cleanup pending")
            return handle

        def checked_close(handle) -> None:
            if not close_handle(handle):
                raise StorageCompensationError("storage cleanup pending")

        def reject_reparse(handle) -> None:
            info = FileAttributeTagInfo()
            if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise StorageCompensationError("storage cleanup pending")
            if info.FileAttributes & 0x400:
                raise StorageOwnershipError("artifact ownership is invalid")

        def read_exact(handle) -> bytes:
            size = ctypes.c_longlong()
            if not get_size(handle, ctypes.byref(size)) or size.value < 0 or size.value > 4096:
                raise StorageOwnershipError("artifact ownership is invalid")
            buffer = ctypes.create_string_buffer(max(1, size.value))
            count = wintypes.DWORD()
            if not read_file(handle, buffer, size.value, ctypes.byref(count), None):
                raise StorageCompensationError("storage cleanup pending")
            if count.value != size.value:
                raise StorageOwnershipError("artifact ownership is invalid")
            return buffer.raw[: size.value]

        def dispose(handle) -> None:
            disposition = FileDispositionInfo(True)
            if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise StorageCompensationError("storage cleanup pending")

        return {
            "open": open_handle,
            "close": checked_close,
            "reject_reparse": reject_reparse,
            "read": read_exact,
            "dispose": dispose,
        }

    def _finalize_tombstone_posix(
        self, ownership: ArtifactOwnership, tombstone: Path
    ) -> None:
        parent_fd = directory_fd = -1
        try:
            parent_fd = os.open(
                self._cleanup_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            directory_fd = os.open(
                ownership.owner_token,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(directory_fd)
            if os.listdir(directory_fd):
                raise StorageOwnershipError("artifact ownership is invalid")
            current = os.stat(
                ownership.owner_token, dir_fd=parent_fd, follow_symlinks=False
            )
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise StorageOwnershipError("artifact ownership is invalid")
            os.rmdir(ownership.owner_token, dir_fd=parent_fd)
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def _finalize_tombstone_windows(self, tombstone: Path) -> None:
        api = self._windows_cleanup_api()
        directory_handle = api["open"](
            tombstone, directory=True, read=False, missing_ok=True
        )
        if directory_handle is None:
            return
        try:
            api["reject_reparse"](directory_handle)
            if any(tombstone.iterdir()):
                raise StorageOwnershipError("artifact ownership is invalid")
            api["dispose"](directory_handle)
        finally:
            api["close"](directory_handle)

    def _reserve_unique_key(self, prefix: str, suffix: str) -> str:
        for _ in range(100):
            storage_key = f"{prefix}{uuid4().hex}{_safe_suffix(suffix)}"
            if not safe_object_path(self.root, storage_key).exists():
                return storage_key
        raise StorageCollisionError("storage key collision")

    def save_with_key(self, key: str, stream: BinaryIO) -> StoredObject:
        storage_key = key
        target = safe_object_path(self.root, storage_key)
        digest = hashlib.sha256()
        size_bytes = 0
        created = False
        try:
            destination = target.open("xb")
            created = True
            with destination:
                while chunk := stream.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > self.max_bytes:
                        raise UploadTooLargeError("upload exceeds configured size limit")
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            self._fsync_directory(target.parent)
        except FileExistsError:
            raise StorageCollisionError("storage key collision") from None
        except Exception:
            if created:
                self.delete(storage_key)
            raise
        return StoredObject(
            storage_key=storage_key,
            sha256=digest.hexdigest(),
            size_bytes=size_bytes,
        )

    def _read_path(self, key: str) -> Path:
        target = safe_object_path(self.root, key)
        try:
            info = target.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or bool(
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
            ):
                raise ValueError
        except (OSError, ValueError):
            raise ValueError("storage object is invalid") from None
        return target

    def open_read(self, key: str) -> BinaryIO:
        descriptor = -1
        try:
            target = self._read_path(key)
            before = target.lstat()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(target, flags)
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or bool(
                    getattr(after, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
            ):
                raise ValueError
            stream = os.fdopen(descriptor, "rb")
            descriptor = -1
            return stream
        except (OSError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            raise ValueError("storage object is invalid") from None

    def copy_to(
        self,
        key: str,
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str,
        max_bytes: int,
    ) -> None:
        if (
            expected_size < 0
            or expected_size > max_bytes
            or max_bytes <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            raise ValueError("storage object is invalid")
        temporary = destination.with_name(destination.name + f".{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            if destination.is_symlink() or not destination.parent.is_dir():
                raise ValueError
            with self.open_read(key) as source, temporary.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes or size > expected_size:
                        raise ValueError
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise ValueError
            os.replace(temporary, destination)
        except (OSError, ValueError):
            temporary.unlink(missing_ok=True)
            raise ValueError("storage object is invalid") from None

    def delete(self, key: str) -> None:
        target = safe_object_path(self.root, key)
        try:
            target.unlink(missing_ok=True)
        except Exception:
            self.record_pending_cleanup(key, "delete_failed")
            raise StorageCompensationError("storage cleanup pending") from None

    def record_pending_cleanup(self, key: str, reason: str) -> None:
        safe_object_path(self.root, key)
        safe_reason = (
            reason if reason in {"delete_failed", "cleanup_failed"} else "cleanup_failed"
        )
        entry = {
            "key": key,
            "recorded_at": datetime.now(UTC).isoformat(),
            "reason": safe_reason,
        }
        journal = safe_object_path(self.root, self._journal_name)
        try:
            with self._journal_lock, journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            raise StorageCompensationError(
                "storage cleanup could not be journaled"
            ) from None

    def retry_pending_cleanup(self) -> int:
        journal = safe_object_path(self.root, self._journal_name)
        if not journal.exists():
            return 0
        cleaned = 0
        remaining: list[dict[str, str]] = []
        with self._journal_lock:
            try:
                entries = [
                    json.loads(line)
                    for line in journal.read_text(encoding="utf-8").splitlines()
                    if line
                ]
                for entry in entries:
                    key = entry["key"]
                    try:
                        safe_object_path(self.root, key).unlink(missing_ok=True)
                    except Exception:
                        remaining.append(entry)
                    else:
                        cleaned += 1
                if remaining:
                    temporary = safe_object_path(
                        self.root, f"{self._journal_name}.tmp"
                    )
                    with temporary.open("w", encoding="utf-8") as handle:
                        for entry in remaining:
                            handle.write(
                                json.dumps(entry, separators=(",", ":")) + "\n"
                            )
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary.replace(journal)
                else:
                    journal.unlink(missing_ok=True)
            except StorageCompensationError:
                raise
            except Exception:
                raise StorageCompensationError(
                    "pending storage cleanup retry failed"
                ) from None
        if remaining:
            raise StorageCompensationError("storage cleanup remains pending")
        return cleaned


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    return LocalStorage(settings.storage_root, max_bytes=settings.max_upload_bytes)
