from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import tempfile
import sys


class ProcessRunnerError(RuntimeError):
    """Raised before execution when the isolation contract is violated."""


class ProcessCleanupError(ProcessRunnerError):
    """Raised with fixed text when owned process resources cannot be reaped."""


class ProcessIsolationError(ProcessRunnerError):
    """Raised when fail-closed process containment cannot be established."""


_LAUNCHER_CODE = r"""
import json, os, subprocess, sys
from pathlib import Path
if sys.stdin.buffer.read(1) != b"G":
    raise SystemExit(125)
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
payload = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
if config.get("version") != 1:
    raise SystemExit(126)
child = subprocess.Popen(payload["argv"], shell=False, cwd=payload["cwd"], env=payload["env"])
raise SystemExit(child.wait())
"""


if os.name == "nt":
    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", ctypes.c_ulong),
            ("CreationTimeLow", ctypes.c_ulong),
            ("CreationTimeHigh", ctypes.c_ulong),
            ("LastAccessTimeLow", ctypes.c_ulong),
            ("LastAccessTimeHigh", ctypes.c_ulong),
            ("LastWriteTimeLow", ctypes.c_ulong),
            ("LastWriteTimeHigh", ctypes.c_ulong),
            ("VolumeSerialNumber", ctypes.c_ulong),
            ("FileSizeHigh", ctypes.c_ulong),
            ("FileSizeLow", ctypes.c_ulong),
            ("NumberOfLinks", ctypes.c_ulong),
            ("FileIndexHigh", ctypes.c_ulong),
            ("FileIndexLow", ctypes.c_ulong),
        ]


class _WindowsJob:
    _KILL_ON_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        if os.name != "nt":
            raise ProcessRunnerError("process isolation is unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self._kernel32.SetInformationJobObject.restype = ctypes.c_int
        self._kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        self._closed = False
        if not self._handle:
            raise ProcessRunnerError("process isolation is unavailable")
        information = _JobExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = self._KILL_ON_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self.close()
            raise ProcessRunnerError("process isolation is unavailable")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ProcessIsolationError("process isolation is unavailable")

    def close(self) -> bool:
        if self._closed:
            return True
        if not self._handle or not self._kernel32.CloseHandle(self._handle):
            return False
        self._closed = True
        self._handle = None
        return True

    def terminate(self) -> bool:
        if self._closed or not self._handle:
            return True
        self._kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._kernel32.TerminateJobObject.restype = ctypes.c_int
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def force_close(self) -> bool:
        if self._closed:
            return True
        if not self._handle or not self._kernel32.CloseHandle(self._handle):
            return False
        self._closed = True
        self._handle = None
        return True

    def __del__(self) -> None:
        try:
            self.force_close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(slots=True)
class StageBudget:
    remaining_files: int
    remaining_bytes: int

    def consume_file(self) -> None:
        if self.remaining_files <= 0:
            raise ProcessRunnerError("source exceeds copy safety limit")
        self.remaining_files -= 1

    def consume_bytes(self, amount: int) -> None:
        if amount < 0 or amount > self.remaining_bytes:
            raise ProcessRunnerError("source exceeds copy safety limit")
        self.remaining_bytes -= amount


_SYSTEM_ENV = frozenset(
    {"COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"}
)
_LEGACY_ENV = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHONPATH",
    }
)
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)((?:\\[\"']|[\"'])?(?:authorization|proxy-authorization)"
        r"(?:\\[\"']|[\"'])?\s*[:=]\s*(?:\\[\"']|[\"'])?\s*"
        r"(?:(?:bearer|basic)\s+)?)[^\s,;'\"}\\]+"
    ),
    re.compile(
        r"(?i)((?:\\[\"']|[\"'])?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password)(?:\\[\"']|[\"'])?\s*[:=]\s*"
        r"(?:\\[\"']|[\"'])?\s*)[^\s,;'\"}\\]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_.-]{4,}\b"),
)


def _redact(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _bounded_utf8(value: str, limit: int, truncated: bool) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if not truncated and len(encoded) <= limit:
        return value, False
    marker = b"\n...[TRUNCATED]"
    budget = max(0, limit - len(marker))
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    suffix = marker[: max(0, limit - len(prefix.encode("utf-8")))].decode("ascii")
    return prefix + suffix, True


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._retained = bytearray()
        self._truncated = False
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self._limit - len(self._retained)
            if remaining > 0:
                self._retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self._truncated = True

    def result(self) -> tuple[str, bool]:
        with self._lock:
            raw = bytes(self._retained)
            truncated = self._truncated
        text = _redact(raw.decode("utf-8", errors="replace"))
        return _bounded_utf8(text, self._limit, truncated)


class ProcessRunner:
    def __init__(
        self,
        *,
        allowed_work_root: Path,
        allowed_source_roots: Sequence[Path] = (),
        timeout_seconds: float = 900,
        max_output_bytes: int | None = None,
        max_output_chars: int | None = None,
        shutdown_grace_seconds: float = 2.0,
        max_copy_files: int = 50_000,
        max_copy_bytes: int = 2 * 1024 * 1024 * 1024,
        env_allowlist: Sequence[str] = (),
    ) -> None:
        self.allowed_work_root = Path(allowed_work_root).resolve()
        self.allowed_source_roots = tuple(Path(root).resolve() for root in allowed_source_roots)
        self.timeout_seconds = timeout_seconds
        if max_output_bytes is not None and max_output_chars is not None:
            raise ValueError("configure only one output limit")
        self.max_output_bytes = max_output_bytes or max_output_chars or 64_000
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.max_copy_files = max_copy_files
        self.max_copy_bytes = max_copy_bytes
        self.env_allowlist = _SYSTEM_ENV | _LEGACY_ENV | frozenset(env_allowlist)
        self._stage_roots: dict[
            Path, tuple[Path, tuple[int, int, int], bytes]
        ] = {}
        self._sealed_stages: dict[Path, tuple[bytes, dict[str, tuple[int, int, int, int, str]]]] = {}
        if (
            timeout_seconds <= 0
            or self.max_output_bytes <= 0
            or shutdown_grace_seconds <= 0
            or max_copy_files <= 0
            or max_copy_bytes <= 0
        ):
            raise ValueError("timeouts and safety limits must be positive")

    def _work_dir(self, cwd: Path) -> Path:
        resolved = Path(cwd).resolve()
        if resolved != self.allowed_work_root and self.allowed_work_root not in resolved.parents:
            raise ProcessRunnerError("cwd is outside allowed work root")
        if not resolved.is_dir():
            raise ProcessRunnerError("cwd must be an existing directory below allowed work root")
        return resolved

    def new_stage_budget(self) -> StageBudget:
        return StageBudget(
            remaining_files=self.max_copy_files,
            remaining_bytes=self.max_copy_bytes,
        )

    def _environment(self, supplied: Mapping[str, str], work_dir: Path) -> dict[str, str]:
        clean = {key: value for key, value in os.environ.items() if key.upper() in _SYSTEM_ENV}
        for key, value in supplied.items():
            normalized = str(key).upper()
            if normalized in self.env_allowlist:
                clean[normalized] = str(value)
        process_home = work_dir / ".process-env"
        process_home.mkdir(parents=True, exist_ok=True)
        for key in ("TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE"):
            clean[key] = str(process_home)
        return clean

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            raise ProcessRunnerError("configured read-only source does not exist") from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    @classmethod
    def _assert_no_link_components(cls, path: Path) -> None:
        for component in (path, *path.parents):
            if component == Path(component.anchor):
                break
            if component.exists() and cls._is_link_or_reparse(component):
                raise ProcessRunnerError("source links and reparse points are forbidden")

    @staticmethod
    def _safe_stage_relative(value: str) -> tuple[str, ...]:
        raw = Path(value)
        if raw.is_absolute() or not value or "\0" in value or ".." in raw.parts:
            raise ProcessRunnerError("stage destination is invalid")
        parts = tuple(part for part in raw.parts if part not in ("", "."))
        if not parts:
            raise ProcessRunnerError("stage destination is invalid")
        return parts

    def _stage_destination(self, cwd: Path, relative: str) -> Path:
        destination_dir = Path(cwd).resolve()
        if destination_dir != self.allowed_work_root and self.allowed_work_root not in destination_dir.parents:
            raise ProcessRunnerError("destination is outside allowed work root")
        destination_dir.mkdir(parents=True, exist_ok=True)
        stage_root = self._private_stage_root(destination_dir)
        destination = stage_root.joinpath(*self._safe_stage_relative(relative)).resolve()
        if stage_root not in destination.parents:
            raise ProcessRunnerError("stage destination is invalid")
        self._ensure_private_directory_chain(stage_root, destination.parent)
        return destination

    @classmethod
    def _ensure_private_directory_chain(cls, root: Path, parent: Path) -> None:
        parts = parent.relative_to(root).parts
        if os.name == "nt":
            current = root
            for part in parts:
                current = current / part
                if current.exists():
                    cls._directory_identity(current)
                else:
                    os.mkdir(current)
                    cls._directory_identity(current)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(root, flags)
        try:
            for part in parts:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError:
            raise ProcessRunnerError("stage destination identity changed") from None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    @classmethod
    def _directory_identity(cls, path: Path) -> tuple[int, int, int]:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_void_p,
            ]
            kernel32.CreateFileW.restype = ctypes.c_void_p
            kernel32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.GetFileInformationByHandle.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.CreateFileW(
                str(path),
                0x80,
                0x1 | 0x2 | 0x4,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            if not handle or handle == ctypes.c_void_p(-1).value:
                raise ProcessRunnerError("directory identity changed")
            information = _ByHandleFileInformation()
            try:
                if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
                    raise ProcessRunnerError("directory identity changed")
            finally:
                kernel32.CloseHandle(handle)
            if information.FileAttributes & 0x400 or not information.FileAttributes & 0x10:
                raise ProcessRunnerError("directory identity changed")
            file_index = (information.FileIndexHigh << 32) | information.FileIndexLow
            return information.VolumeSerialNumber, file_index, information.FileAttributes
        try:
            metadata = path.lstat()
        except OSError:
            raise ProcessRunnerError("directory identity changed") from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            raise ProcessRunnerError("directory identity changed")
        return metadata.st_dev, metadata.st_ino, attributes

    @classmethod
    def _verify_directory_identities(
        cls, records: Sequence[tuple[Path, tuple[int, int, int]]]
    ) -> bool:
        try:
            return all(cls._directory_identity(path) == identity for path, identity in records)
        except ProcessRunnerError:
            return False

    def _private_stage_root(self, cwd: Path) -> Path:
        existing = self._stage_roots.get(cwd)
        if existing is not None:
            path, identity, security = existing
            if (
                path.resolve() != path
                or not self._verify_directory_identities([(path, identity)])
                or self._directory_security_token(path) != security
            ):
                raise ProcessRunnerError("stage destination identity changed")
            return path
        path = Path(tempfile.mkdtemp(prefix=".stage-", dir=cwd)).absolute()
        self._secure_private_directory(path)
        if path.resolve().parent != cwd or self._is_link_or_reparse(path):
            shutil.rmtree(path, ignore_errors=True)
            raise ProcessRunnerError("stage destination identity changed")
        identity = self._directory_identity(path)
        self._stage_roots[cwd] = (path, identity, self._directory_security_token(path))
        return path

    def _discard_stage_root(self, cwd: Path) -> None:
        key = Path(cwd).resolve()
        existing = self._stage_roots.pop(key, None)
        if existing is not None:
            self._sealed_stages.pop(existing[0], None)
            self._remove_private_tree(existing[0])

    @classmethod
    def _remove_private_tree(cls, root: Path) -> bool:
        def remove(path: Path) -> None:
            metadata = path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            is_reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if stat.S_ISDIR(metadata.st_mode) and not is_reparse:
                with os.scandir(path) as entries:
                    children = [Path(entry.path) for entry in entries]
                for child in children:
                    remove(child)
                path.chmod(0o700)
                path.rmdir()
                return
            if not stat.S_ISLNK(metadata.st_mode):
                path.chmod(0o600)
            path.unlink()

        try:
            if root.exists() or root.is_symlink():
                remove(root)
            return True
        except OSError:
            return False

    def discard_stage(self) -> bool:
        cleanup_ok = True
        for cwd, record in list(self._stage_roots.items()):
            root = record[0]
            if self._remove_private_tree(root):
                self._stage_roots.pop(cwd, None)
                self._sealed_stages.pop(root, None)
            else:
                cleanup_ok = False
        return cleanup_ok

    @staticmethod
    def _secure_private_directory(path: Path) -> None:
        try:
            path.chmod(0o700)
            if os.name != "nt":
                return
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            descriptor = ctypes.c_void_p()
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
            ]
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
            if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;FA;;;OW)(A;;FA;;;SY)", 1, ctypes.byref(descriptor), None
            ):
                raise OSError
            try:
                advapi32.SetFileSecurityW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_void_p]
                advapi32.SetFileSecurityW.restype = ctypes.c_int
                if not advapi32.SetFileSecurityW(str(path), 0x4, descriptor):
                    raise OSError
            finally:
                kernel32.LocalFree(descriptor)
        except OSError:
            raise ProcessIsolationError("private directory security is unavailable") from None

    @staticmethod
    def _directory_security_token(path: Path) -> bytes:
        try:
            if os.name != "nt":
                metadata = path.stat()
                if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                    raise ProcessIsolationError("private directory security is unavailable")
                return f"{metadata.st_uid}:{metadata.st_mode & 0o777}".encode()
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            needed = ctypes.c_ulong()
            advapi32.GetFileSecurityW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            advapi32.GetFileSecurityW.restype = ctypes.c_int
            advapi32.GetFileSecurityW(str(path), 0x5, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise OSError
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetFileSecurityW(
                str(path), 0x5, buffer, needed.value, ctypes.byref(needed)
            ):
                raise OSError
            return hashlib.sha256(buffer.raw[: needed.value]).digest()
        except OSError:
            raise ProcessIsolationError("private directory security is unavailable") from None

    def seal_staging(self) -> None:
        try:
            for _, (root, identity, security) in list(self._stage_roots.items()):
                if not self._verify_directory_identities([(root, identity)]):
                    raise ProcessIsolationError("stage integrity verification failed")
                if self._directory_security_token(root) != security:
                    raise ProcessIsolationError("stage integrity verification failed")
                entries: dict[str, tuple[int, int, int, int, str]] = {}
                for path in sorted(root.rglob("*")):
                    if path.is_dir():
                        self._directory_identity(path)
                        continue
                    if path.name == ".stage-manifest.json":
                        continue
                    path.chmod(0o400)
                    descriptor = self._open_verified_source(path)
                    try:
                        digest = hashlib.sha256()
                        metadata = os.fstat(descriptor)
                        while chunk := os.read(descriptor, 65_536):
                            digest.update(chunk)
                    finally:
                        os.close(descriptor)
                    relative = path.relative_to(root).as_posix()
                    entries[relative] = (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        getattr(metadata, "st_file_attributes", 0),
                        digest.hexdigest(),
                    )
                manifest_bytes = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
                manifest = root / ".stage-manifest.json"
                temp = root / ".stage-manifest.tmp"
                with temp.open("xb") as handle:
                    handle.write(manifest_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, manifest)
                manifest.chmod(0o400)
                self._sealed_stages[root] = (manifest_bytes, entries)
        except Exception:
            self.discard_stage()
            raise ProcessIsolationError("stage integrity verification failed") from None

    def _verify_staging(self) -> None:
        if self._stage_roots and not self._sealed_stages:
            self.seal_staging()
        for _, (root, identity, security) in self._stage_roots.items():
            sealed = self._sealed_stages.get(root)
            if (
                sealed is None
                or not self._verify_directory_identities([(root, identity)])
                or self._directory_security_token(root) != security
            ):
                raise ProcessIsolationError("stage integrity verification failed")
            manifest_bytes, entries = sealed
            manifest = root / ".stage-manifest.json"
            try:
                if manifest.read_bytes() != manifest_bytes:
                    raise ProcessIsolationError("stage integrity verification failed")
                actual_paths = {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file() and path.name != ".stage-manifest.json"
                }
                if actual_paths != set(entries):
                    raise ProcessIsolationError("stage integrity verification failed")
                for relative, expected in entries.items():
                    path = root.joinpath(*PurePosixPath(relative).parts)
                    descriptor = self._open_verified_source(path)
                    digest = hashlib.sha256()
                    try:
                        metadata = os.fstat(descriptor)
                        while chunk := os.read(descriptor, 65_536):
                            digest.update(chunk)
                    finally:
                        os.close(descriptor)
                    actual = (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        getattr(metadata, "st_file_attributes", 0),
                        digest.hexdigest(),
                    )
                    if actual != expected:
                        raise ProcessIsolationError("stage integrity verification failed")
            except ProcessIsolationError:
                raise
            except (OSError, MemoryError):
                raise ProcessIsolationError("stage integrity verification failed") from None

    def _target_identity_records(self, destination: Path) -> list[tuple[Path, tuple[int, int, int]]]:
        records: list[tuple[Path, tuple[int, int, int]]] = []
        current = destination.parent
        while self.allowed_work_root in current.parents:
            records.append((current, self._directory_identity(current)))
            if current.name.startswith(".stage-"):
                break
            current = current.parent
        return records

    def stage_source_document(
        self,
        source: Path,
        *,
        cwd: Path,
        stage_name: str | None = None,
        area: str = "inputs",
        budget: StageBudget | None = None,
    ) -> Path:
        original = Path(source).absolute()
        self._assert_no_link_components(original)
        source = self.validate_readonly_source(original)
        relative = str(Path(area) / stage_name / source.name) if stage_name else str(Path(area) / source.name)
        destination = self._stage_destination(cwd, relative)
        target_records = self._target_identity_records(destination)
        try:
            self._copy_verified_file(source, destination, budget or self.new_stage_budget())
            if not self._verify_directory_identities(target_records):
                raise ProcessRunnerError("stage destination identity changed")
        except ProcessRunnerError:
            self._discard_stage_root(cwd)
            raise
        return destination

    def validate_readonly_source(self, source: Path) -> Path:
        source = Path(source).resolve()
        if not any(source == root or root in source.parents for root in self.allowed_source_roots):
            raise ProcessRunnerError("source is outside configured read-only source roots")
        if not source.exists():
            raise ProcessRunnerError("configured read-only source does not exist")
        return source

    def stage_source_tree(
        self,
        source: Path,
        *,
        cwd: Path,
        name: str,
        area: str = "inputs",
        budget: StageBudget | None = None,
    ) -> Path:
        original = Path(source).absolute()
        self._assert_no_link_components(original)
        source = self.validate_readonly_source(original)
        if not source.is_dir():
            raise ProcessRunnerError("source tree must be a directory")
        destination = self._stage_destination(cwd, str(Path(area) / name))
        if destination.exists():
            raise ProcessRunnerError("stage destination already exists")
        destination.mkdir(parents=True)
        shared_budget = budget or self.new_stage_budget()
        source_records: list[tuple[Path, tuple[int, int, int]]] = []
        target_records = self._target_identity_records(destination)
        try:
            for current, directories, files in os.walk(source, followlinks=False):
                current_path = Path(current)
                source_records.append((current_path, self._directory_identity(current_path)))
                for entry_name in (*directories, *files):
                    if self._is_link_or_reparse(current_path / entry_name):
                        raise ProcessRunnerError("source links and reparse points are forbidden")
                relative_dir = current_path.relative_to(source)
                target_dir = destination / relative_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                for filename in files:
                    source_file = current_path / filename
                    self._copy_verified_file(
                        source_file,
                        target_dir / filename,
                        shared_budget,
                    )
            target_records.extend(self._target_identity_records(destination / "placeholder"))
            if not self._verify_directory_identities(source_records):
                raise ProcessRunnerError("source directory identity changed")
            if not self._verify_directory_identities(target_records):
                raise ProcessRunnerError("stage destination identity changed")
        except ProcessRunnerError:
            self._discard_stage_root(cwd)
            raise
        except OSError:
            self._discard_stage_root(cwd)
            raise ProcessRunnerError("source copy failed safety validation") from None
        return destination

    @classmethod
    def _open_verified_source(cls, source: Path) -> int:
        try:
            before = source.lstat()
            if not stat.S_ISREG(before.st_mode) or cls._is_link_or_reparse(source):
                raise ProcessRunnerError("source must be a regular non-link file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(source, flags)
            after = os.fstat(descriptor)
            after_attributes = getattr(after, "st_file_attributes", 0)
            if (
                not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or bool(after_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            ):
                os.close(descriptor)
                raise ProcessRunnerError("source identity changed during staging")
            return descriptor
        except ProcessRunnerError:
            raise
        except OSError:
            raise ProcessRunnerError("source copy failed safety validation") from None

    @classmethod
    def _copy_verified_file(
        cls,
        source: Path,
        destination: Path,
        budget: StageBudget,
    ) -> None:
        descriptor = cls._open_verified_source(source)
        try:
            budget.consume_file()
            with destination.open("xb") as output:
                while True:
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        break
                    budget.consume_bytes(len(chunk))
                    output.write(chunk)
        except ProcessRunnerError:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except OSError:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProcessRunnerError("source copy failed safety validation") from None
        finally:
            os.close(descriptor)

    @classmethod
    def _write_launcher_config(cls, directory: Path) -> Path:
        temporary = directory / "config.tmp"
        destination = directory / "config.json"
        descriptor = -1
        failure: BaseException | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            payload = json.dumps({"version": 1}, separators=(",", ":")).encode("ascii")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except BaseException as exc:
            failure = exc
        finally:
            if descriptor >= 0:
                open_descriptor = descriptor
                descriptor = -1
                try:
                    os.close(open_descriptor)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
        if failure is None:
            try:
                os.replace(temporary, destination)
                destination.chmod(0o600)
                return destination
            except BaseException as exc:
                failure = exc
        try:
            temporary.unlink(missing_ok=True)
        except BaseException:
            pass
        raise failure

    @staticmethod
    def _launcher_environment() -> dict[str, str]:
        interpreter = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        environment: dict[str, str] = {
            "PATH": str(interpreter.parent),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        return environment

    def _cleanup_failed_launch(
        self,
        process: subprocess.Popen[bytes] | None,
        job: _WindowsJob | None,
        drainers: Sequence[threading.Thread],
        launcher_dir: Path | None,
    ) -> bool:
        cleanup_ok = True
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                cleanup_ok = False
        if job is not None:
            try:
                cleanup_ok = job.force_close() and cleanup_ok
            except Exception:
                cleanup_ok = False
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    cleanup_ok = False
            try:
                if process.poll() is None:
                    process.wait(timeout=self.shutdown_grace_seconds)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_ok = False
            try:
                cleanup_ok = self._finish_drainers(
                    process,
                    drainers,
                    time.monotonic() + self.shutdown_grace_seconds,
                ) and cleanup_ok
            except Exception:
                cleanup_ok = False
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        self._close_pipe(pipe)
        if launcher_dir is not None:
            cleanup_ok = self._remove_private_tree(launcher_dir) and cleanup_ok
        return cleanup_ok

    def _cleanup_payload_failure(
        self,
        process: subprocess.Popen[bytes],
        job: _WindowsJob | None,
        drainers: Sequence[threading.Thread],
        launcher_dir: Path,
    ) -> bool:
        cleanup_ok = True
        try:
            cleanup_ok = self._terminate_owned_group(process, job) and cleanup_ok
        except BaseException:
            cleanup_ok = False
        try:
            if process.stdin is not None:
                process.stdin.close()
        except BaseException:
            cleanup_ok = False
        try:
            cleanup_ok = self._finish_drainers(
                process,
                drainers,
                time.monotonic() + self.shutdown_grace_seconds,
            ) and cleanup_ok
        except BaseException:
            cleanup_ok = False
        try:
            cleanup_ok = self._remove_private_tree(launcher_dir) and cleanup_ok
        except BaseException:
            cleanup_ok = False
        return cleanup_ok

    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
    ) -> ProcessResult:
        if isinstance(argv, (str, bytes)) or not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ProcessRunnerError("argv must be a non-empty sequence of strings")
        work_dir = self._work_dir(cwd)
        self._verify_staging()
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        launcher_dir: Path | None = None
        job: _WindowsJob | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            launcher_dir = Path(tempfile.mkdtemp(prefix=".process-launch-", dir=work_dir))
            self._secure_private_directory(launcher_dir)
            launcher_config = self._write_launcher_config(launcher_dir)
            job = _WindowsJob() if os.name == "nt" else None
        except Exception as exc:
            cleanup_ok = self._cleanup_failed_launch(None, job, (), launcher_dir)
            if not cleanup_ok:
                raise ProcessCleanupError("process cleanup could not be confirmed") from None
            if isinstance(exc, ProcessIsolationError):
                raise exc from None
            raise ProcessRunnerError("process could not be started") from None
        try:
            launcher_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
            process = subprocess.Popen(
                [str(launcher_python), "-I", "-S", "-c", _LAUNCHER_CODE, str(launcher_config)],
                shell=False,
                cwd=launcher_dir,
                env=self._launcher_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                **popen_kwargs,
            )
            if job is not None:
                job.assign(process)
        except Exception as exc:
            cleanup_ok = self._cleanup_failed_launch(process, job, (), launcher_dir)
            if not cleanup_ok:
                raise ProcessCleanupError("process cleanup could not be confirmed") from None
            if isinstance(exc, ProcessIsolationError):
                raise exc from None
            raise ProcessRunnerError("process could not be started") from None
        assert process is not None
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdout_capture = _BoundedCapture(self.max_output_bytes)
        stderr_capture = _BoundedCapture(self.max_output_bytes)
        drainers = [
            threading.Thread(
                target=self._drain,
                args=(process.stdout, stdout_capture),
                name=f"process-stdout-{process.pid}",
                daemon=False,
            ),
            threading.Thread(
                target=self._drain,
                args=(process.stderr, stderr_capture),
                name=f"process-stderr-{process.pid}",
                daemon=False,
            ),
        ]
        for thread in drainers:
            thread.start()
        payload_failure: BaseException | None = None
        cleanup_ok = True
        try:
            payload = json.dumps(
                {
                    "argv": list(argv),
                    "cwd": str(work_dir),
                    "env": self._environment(env, work_dir),
                },
                ensure_ascii=True,
            ).encode("utf-8")
            process.stdin.write(b"G" + payload + b"\n")
            process.stdin.flush()
            process.stdin.close()
        except BaseException as exc:
            payload_failure = exc
        finally:
            if payload_failure is not None:
                cleanup_ok = self._cleanup_payload_failure(
                    process, job, drainers, launcher_dir
                )
        if payload_failure is not None:
            if not cleanup_ok:
                raise ProcessCleanupError("process cleanup could not be confirmed") from None
            if isinstance(payload_failure, (KeyboardInterrupt, SystemExit)):
                raise payload_failure
            raise ProcessRunnerError("process could not be started") from None
        timed_out = False
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        deadline = time.monotonic() + self.shutdown_grace_seconds
        cleanup_ok = self._terminate_owned_group(process, job)
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                cleanup_ok = False
        cleanup_ok = self._finish_drainers(process, drainers, deadline) and cleanup_ok
        try:
            if not cleanup_ok:
                raise ProcessCleanupError("process cleanup could not be confirmed")
            stdout, stdout_truncated = stdout_capture.result()
            stderr, stderr_truncated = stderr_capture.result()
            return ProcessResult(
                exit_code=-1 if timed_out else (process.returncode if process.returncode is not None else -1),
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        finally:
            shutil.rmtree(launcher_dir, ignore_errors=True)

    @staticmethod
    def _drain(pipe: object, capture: _BoundedCapture) -> None:
        try:
            while True:
                chunk = pipe.read(65_536)  # type: ignore[attr-defined]
                if not chunk:
                    return
                capture.add(chunk)
        except (OSError, ValueError):
            return

    @staticmethod
    def _close_pipe(pipe: object) -> None:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    @classmethod
    def _finish_drainers(
        cls,
        process: subprocess.Popen[bytes],
        drainers: Sequence[threading.Thread],
        deadline: float,
    ) -> bool:
        for thread in drainers:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in drainers):
            return False
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                cls._close_pipe(pipe)
        return True

    @staticmethod
    def _terminate_owned_group(
        process: subprocess.Popen[bytes], job: _WindowsJob | None
    ) -> bool:
        if os.name == "nt":
            if job is None:
                return False
            if job.close():
                return True
            job.terminate()
            job.force_close()
            return False
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return True
        except OSError:
            return False
