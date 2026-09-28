from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, TypeAlias
from uuid import UUID


QueueName: TypeAlias = Literal["rule", "llm"]
FailureKind: TypeAlias = Literal["retryable", "permanent"]


class AdapterError(RuntimeError):
    failure_kind: FailureKind


class RetryableAdapterError(AdapterError):
    failure_kind: FailureKind = "retryable"


class StageCleanupError(RetryableAdapterError):
    """Raised with fixed text when private staging cleanup cannot be confirmed."""


class PermanentAdapterError(AdapterError):
    failure_kind: FailureKind = "permanent"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: str
    processed: int | None = None
    total: int | None = None
    summary: str | None = None


ProgressSink: TypeAlias = Callable[[ProgressEvent], None]


def _relative_parts(value: str | Path) -> tuple[str, ...]:
    raw = str(value)
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute():
        raise PermanentAdapterError("path escapes isolated work directory")
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if not parts or ".." in parts or windows.drive or ".." in windows.parts:
        raise PermanentAdapterError("path escapes isolated work directory")
    return parts


@dataclass(frozen=True, slots=True)
class AdapterContext:
    work_dir: Path
    execution_idempotency_key: str = "standalone-adapter-execution"
    step_id: UUID | None = None
    document_version_id: UUID | None = None
    profile_snapshot: Mapping[str, object] = field(default_factory=dict)
    document_path: Path | None = None
    model_config_id: UUID | str | None = None
    route_result_manifests: tuple[Path, ...] = ()
    persistent_cache_root: Path | None = None
    # Legacy rule-table steps reuse the v105 engine TSV produced by the sibling
    # rule-text step instead of re-running the engine; the orchestrator stages
    # that combined script output here.
    shared_source_tsv: Path | None = None

    def __post_init__(self) -> None:
        if not self.execution_idempotency_key.strip():
            raise PermanentAdapterError("execution idempotency key is required")
        object.__setattr__(self, "work_dir", self.work_dir.resolve())
        if self.document_path is not None:
            # Preserve the final path component so the reader can reject a link;
            # resolve() here would erase that security-relevant information.
            object.__setattr__(self, "document_path", self.document_path.absolute())
        object.__setattr__(
            self,
            "route_result_manifests",
            tuple(path.absolute() for path in self.route_result_manifests),
        )
        if self.persistent_cache_root is not None:
            object.__setattr__(
                self, "persistent_cache_root", self.persistent_cache_root.absolute()
            )
        if self.shared_source_tsv is not None:
            object.__setattr__(
                self, "shared_source_tsv", self.shared_source_tsv.absolute()
            )

    def output_path(self, relative_path: str | Path) -> Path:
        target = self.work_dir.joinpath(*_relative_parts(relative_path)).resolve()
        if self.work_dir != target and self.work_dir not in target.parents:
            raise PermanentAdapterError("path escapes isolated work directory")
        return target


@dataclass(frozen=True, slots=True)
class AdapterResult:
    manifest_path: Path
    metrics: Mapping[str, int | float]


def validate_adapter_result(
    context: AdapterContext, result: AdapterResult
) -> AdapterResult:
    manifest = result.manifest_path.resolve()
    if context.work_dir not in manifest.parents or not manifest.is_file():
        raise PermanentAdapterError("adapter manifest is outside work directory")
    if not result.metrics:
        raise PermanentAdapterError("adapter metrics are required")
    for name, value in result.metrics.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or not math.isfinite(value)
        ):
            raise PermanentAdapterError("adapter metrics are invalid")
    return AdapterResult(manifest_path=manifest, metrics=dict(result.metrics))


class ExtractionAdapter(Protocol):
    """Adapters must forward ``context.execution_idempotency_key`` to providers.

    If a provider has no idempotency-key feature, the adapter must use this stable
    value as its local result-cache key before issuing an external side effect.
    """
    name: str
    version: str
    queue: QueueName

    def run(
        self, context: AdapterContext, emit_progress: ProgressSink
    ) -> AdapterResult: ...
