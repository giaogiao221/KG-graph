from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PresetName = Literal["rule", "llm", "hybrid", "custom"]

_SAFE_THRESHOLD_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}\Z", re.ASCII)
_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "header",
    "password",
    "path",
    "secret",
    "token",
)


def _contains_sensitive_marker(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("sk-", "pk-")) or any(
        marker in lowered for marker in _SENSITIVE_MARKERS
    )


class RouteSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text_rule: bool = False
    text_llm: bool = False
    table_rule: bool = False
    table_llm: bool = False

    @model_validator(mode="after")
    def at_least_one(self) -> RouteSelection:
        if not any(self.model_dump().values()):
            raise ValueError("at least one extraction route must be enabled")
        return self


_PRESETS: dict[str, dict[str, bool]] = {
    "rule": {
        "text_rule": True,
        "text_llm": False,
        "table_rule": True,
        "table_llm": False,
    },
    "llm": {
        "text_rule": False,
        "text_llm": True,
        "table_rule": False,
        "table_llm": True,
    },
    "hybrid": {
        "text_rule": True,
        "text_llm": True,
        "table_rule": True,
        "table_llm": True,
    },
}


def resolve_preset(name: str) -> RouteSelection:
    try:
        selection = _PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown extraction preset: {name}") from None
    return RouteSelection.model_validate(selection)


class QualityStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    evidence_required: Literal[True] = True
    deduplicate: Literal[True] = True
    schema59_validation: Literal[True] = True
    fail_closed: bool = True
    minimum_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict_policy: Literal["manual_review"] = "manual_review"


class ProfileConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    preset: PresetName
    routes: RouteSelection | None = None
    model_config_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    thresholds: dict[str, float] = Field(default_factory=dict)
    concurrency: int = Field(default=1, ge=1, le=128)
    rule_version: UUID = Field(default=UUID(int=0))
    plugin_version: UUID = Field(default=UUID(int=0))
    prompt_version: UUID = Field(default=UUID(int=0))
    quality_strategy: QualityStrategy = Field(default_factory=QualityStrategy)

    @field_validator("thresholds")
    @classmethod
    def safe_threshold_keys(cls, value: dict[str, float]) -> dict[str, float]:
        if any(
            _SAFE_THRESHOLD_KEY.fullmatch(key) is None
            or _contains_sensitive_marker(key)
            for key in value
        ):
            raise ValueError("threshold keys must be safe identifiers")
        return value

    @model_validator(mode="after")
    def resolve_routes(self) -> ProfileConfiguration:
        if self.preset == "custom":
            if self.routes is None:
                raise ValueError("custom preset requires routes")
        else:
            self.routes = resolve_preset(self.preset)
        assert self.routes is not None
        if self.preset == "llm" and self.prompt_version == UUID(int=0):
            self.prompt_version = UUID("4d5b4f4c-88dd-4c86-a34b-0aba65429cf1")
        if self.routes.text_llm or self.routes.table_llm:
            if len(self.model_config_ids) != 1:
                raise ValueError("LLM extraction requires exactly one model configuration")
        return self


class ProfileCreate(ProfileConfiguration):
    name: str = Field(min_length=1, max_length=200)


class ProfileVersionCreate(ProfileConfiguration):
    pass


class ProfileVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    version_number: int
    snapshot: dict[str, object]
    snapshot_sha256: str
    created_by_id: UUID
    created_at: datetime


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    next_version_number: int
    created_at: datetime


class ProfileSummaryResponse(ProfileResponse):
    latest_version: ProfileVersionResponse
