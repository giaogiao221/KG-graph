from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import threading
from functools import wraps
from typing import Mapping, Sequence


MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_TSV_BYTES = 64 * 1024 * 1024
MAX_ROWS = 10_000
MAX_FIELD_LENGTH = 1_000_000
MAX_METRIC_VALUE = 2**63 - 1
MAX_TSV_ROWS = MAX_ROWS
MAX_TSV_FIELD_LENGTH = MAX_FIELD_LENGTH
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_COLUMNS = 128
MAX_TSV_LINE_BYTES = 1_000_000
MAX_RETAINED_CELL_BYTES = 8 * 1024 * 1024
_CSV_FIELD_LIMIT_LOCK = threading.Lock()


class LegacyParseError(ValueError):
    """A fixed-message parse failure that never includes input data or paths."""


@dataclass(frozen=True, slots=True)
class ParsedLegacyReport:
    status: str
    retryable_errors: int
    metrics: Mapping[str, int | float]
    outputs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ParsedTsv:
    headers: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]


def _fixed_parse_errors(message: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except LegacyParseError:
                raise
            except (
                MemoryError,
                RecursionError,
                OSError,
                UnicodeError,
                csv.Error,
                ValueError,
                TypeError,
            ):
                raise LegacyParseError(message) from None

        return wrapped

    return decorate


def _open_verified_fd(path: Path, limit: int) -> int:
    candidate = Path(path).absolute()
    try:
        before = candidate.lstat()
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            raise LegacyParseError("legacy input is unavailable")
        if before.st_size > limit:
            raise LegacyParseError("legacy input exceeds safety limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(candidate, flags)
        after = os.fstat(descriptor)
        after_attributes = getattr(after, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or bool(after_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            os.close(descriptor)
            raise LegacyParseError("legacy input is unavailable")
        if after.st_size > limit:
            os.close(descriptor)
            raise LegacyParseError("legacy input exceeds safety limit")
        return descriptor
    except LegacyParseError:
        raise
    except OSError:
        raise LegacyParseError("legacy input is unavailable") from None


def _read_limited(path: Path, limit: int) -> bytes:
    descriptor = _open_verified_fd(path, limit)
    payload = bytearray()
    try:
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - len(payload)))
            if not chunk:
                return bytes(payload)
            payload.extend(chunk)
            if len(payload) > limit:
                raise LegacyParseError("legacy input exceeds safety limit")
    except LegacyParseError:
        raise
    except (OSError, MemoryError):
        raise LegacyParseError("legacy input is unavailable") from None
    finally:
        os.close(descriptor)


def _reject_constant(_: str) -> None:
    raise LegacyParseError("legacy report is invalid")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise LegacyParseError("legacy report is invalid")
        value[key] = item
    return value


def _load_report(path: Path) -> dict[str, object]:
    try:
        payload = _read_limited(path, MAX_REPORT_BYTES)
        text = payload.decode("utf-8-sig", errors="strict")
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except LegacyParseError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
        MemoryError,
        OSError,
    ):
        raise LegacyParseError("legacy report is invalid") from None
    if not isinstance(value, dict):
        raise LegacyParseError("legacy report is invalid")
    _validate_json_complexity(value)
    return value


def _validate_json_complexity(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    try:
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
                raise LegacyParseError("legacy report is invalid")
            if isinstance(current, dict):
                for key, item in current.items():
                    if not isinstance(key, str) or len(key) > MAX_FIELD_LENGTH:
                        raise LegacyParseError("legacy report is invalid")
                    stack.append((item, depth + 1))
            elif isinstance(current, list):
                stack.extend((item, depth + 1) for item in current)
    except (MemoryError, RecursionError):
        raise LegacyParseError("legacy report is invalid") from None


def _iter_limited_lines(descriptor: int):
    buffer = bytearray()
    total = 0
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            if buffer:
                if len(buffer) > MAX_TSV_LINE_BYTES:
                    raise LegacyParseError("legacy TSV is invalid")
                yield bytes(buffer)
            return
        total += len(chunk)
        if total > MAX_TSV_BYTES:
            raise LegacyParseError("legacy input exceeds safety limit")
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) > MAX_TSV_LINE_BYTES:
                    raise LegacyParseError("legacy TSV is invalid")
                break
            if newline > MAX_TSV_LINE_BYTES:
                raise LegacyParseError("legacy TSV is invalid")
            line = bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
            yield line


class _DecodedPhysicalLines:
    def __init__(self, lines) -> None:
        self._lines = iter(lines)
        self._first = True

    def __iter__(self):
        return self

    def __next__(self) -> str:
        raw = next(self._lines)
        if raw.count(b"\t") + 1 > MAX_COLUMNS:
            raise LegacyParseError("legacy TSV is invalid")
        encoding = "utf-8-sig" if self._first else "utf-8"
        self._first = False
        return raw.decode(encoding, errors="strict")


def _strict_bool(container: Mapping[str, object], key: str) -> bool:
    value = container.get(key)
    if type(value) is not bool:
        raise LegacyParseError("legacy report is invalid")
    return value


def _strict_metric(
    container: Mapping[str, object], key: str, *, default: int = 0
) -> int:
    value = container.get(key, default)
    if type(value) is not int or not 0 <= value <= MAX_METRIC_VALUE:
        raise LegacyParseError("legacy report is invalid")
    return value


def _optional_mapping(container: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = container.get(key, {})
    if not isinstance(value, dict):
        raise LegacyParseError("legacy report is invalid")
    return value


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > MAX_FIELD_LENGTH:
        raise LegacyParseError("legacy path is invalid")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise LegacyParseError("legacy path is invalid")
    if ".." in posix.parts or ".." in windows.parts:
        raise LegacyParseError("legacy path is invalid")
    parts = tuple(part for part in windows.parts if part not in ("", "."))
    if not parts:
        raise LegacyParseError("legacy path is invalid")
    return PurePosixPath(*parts).as_posix()


def resolve_legacy_output(work_dir: Path, relative_path: str) -> Path:
    safe = _safe_relative(relative_path)
    root = Path(work_dir).resolve()
    target = root.joinpath(*PurePosixPath(safe).parts).resolve()
    if target != root and root not in target.parents:
        raise LegacyParseError("legacy path is invalid")
    return target


@_fixed_parse_errors("legacy report is invalid")
def parse_v105_report(path: Path) -> ParsedLegacyReport:
    report = _load_report(path)
    ok = _strict_bool(report, "ok")
    export = _optional_mapping(report, "schema59_merged_export")
    coverage = _optional_mapping(report, "coverage")
    rows = _strict_metric(export, "rows")
    accepted = _strict_metric(coverage, "accepted_rows", default=rows)
    return ParsedLegacyReport(
        status="completed" if ok else "permanent_failed",
        retryable_errors=0,
        metrics={"rows": rows, "accepted_rows": accepted},
        outputs={},
    )


@_fixed_parse_errors("legacy report is invalid")
def parse_v106_report(path: Path) -> ParsedLegacyReport:
    report = _load_report(path)
    ok = _strict_bool(report, "ok")
    retryable = _strict_metric(report, "llm_error_jobs")
    raw_status = report.get("status", "")
    if not isinstance(raw_status, str) or len(raw_status) > MAX_FIELD_LENGTH:
        raise LegacyParseError("legacy report is invalid")
    if retryable or raw_status == "complete_with_retryable_errors":
        status = "partial_success"
    elif ok or raw_status in {"complete", "planned"}:
        status = "completed"
    else:
        status = "permanent_failed"
    metric_names = ("final_rows", "manual_review_rows", "llm_jobs", "llm_error_jobs")
    metrics = {name: _strict_metric(report, name) for name in metric_names}
    raw_outputs = _optional_mapping(report, "outputs")
    outputs: dict[str, str] = {}
    for key, value in raw_outputs.items():
        if not isinstance(key, str) or not key or len(key) > MAX_FIELD_LENGTH:
            raise LegacyParseError("legacy report is invalid")
        outputs[key] = _safe_relative(value)
    return ParsedLegacyReport(
        status=status,
        retryable_errors=retryable,
        metrics=metrics,
        outputs=outputs,
    )


@_fixed_parse_errors("legacy TSV is invalid")
def parse_candidate_tsv(
    path: Path,
    *,
    expected_headers: Sequence[str] | None = None,
) -> ParsedTsv:
    descriptor = _open_verified_fd(path, MAX_TSV_BYTES)
    try:
        with _CSV_FIELD_LIMIT_LOCK:
            previous_limit = csv.field_size_limit()
            csv.field_size_limit(MAX_FIELD_LENGTH)
            try:
                reader = csv.reader(
                    _DecodedPhysicalLines(_iter_limited_lines(descriptor)),
                    delimiter="\t",
                    strict=True,
                )
                headers = next(reader)
                if not headers or any(not field or len(field) > MAX_FIELD_LENGTH for field in headers):
                    raise LegacyParseError("legacy TSV is invalid")
                if len(headers) > MAX_COLUMNS or len(set(headers)) != len(headers):
                    raise LegacyParseError("legacy TSV is invalid")
                if expected_headers is not None and tuple(headers) != tuple(expected_headers):
                    raise LegacyParseError("legacy TSV is invalid")
                rows: list[Mapping[str, str]] = []
                retained_cell_bytes = 0
                for index, values in enumerate(reader, start=1):
                    if index > MAX_ROWS or len(values) != len(headers):
                        raise LegacyParseError("legacy TSV is invalid")
                    if any(len(value) > MAX_FIELD_LENGTH for value in values):
                        raise LegacyParseError("legacy TSV is invalid")
                    retained_cell_bytes += sum(len(value.encode("utf-8")) + 16 for value in values)
                    if retained_cell_bytes > MAX_RETAINED_CELL_BYTES:
                        raise LegacyParseError("legacy TSV is invalid")
                    rows.append(dict(zip(headers, values, strict=True)))
            finally:
                csv.field_size_limit(previous_limit)
    except LegacyParseError:
        raise
    except (UnicodeDecodeError, csv.Error, StopIteration, OSError, MemoryError, RecursionError):
        raise LegacyParseError("legacy TSV is invalid") from None
    finally:
        os.close(descriptor)
    return ParsedTsv(headers=tuple(headers), rows=tuple(rows))
