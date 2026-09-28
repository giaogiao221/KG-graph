from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.profiles.models import ExtractionProfile, ProfileVersion
from app.profiles.schemas import ProfileConfiguration
from app.models.models import ModelConfig
from app.prompts.models import PromptTemplate
from app.extraction.native.pure_llm_prompts import TABLE_PROMPT, TEXT_PROMPT


DEFAULT_PROMPT_TEMPLATE_ID = "4d5b4f4c-88dd-4c86-a34b-0aba65429cf1"


class ProfileModelError(ValueError):
    pass


def validate_model_references(
    session: Session, configuration: ProfileConfiguration, *, lock: bool = False
) -> None:
    routes = configuration.routes
    if routes is None or not (routes.text_llm or routes.table_llm):
        return
    statement = select(ModelConfig.id).where(
        ModelConfig.id.in_(configuration.model_config_ids),
        ModelConfig.is_enabled.is_(True),
    )
    if lock:
        statement = statement.with_for_update(read=True)
    found = set(session.scalars(statement))
    if found != set(configuration.model_config_ids):
        raise ProfileModelError("model configuration is unavailable")


def _snapshot(session: Session, configuration: ProfileConfiguration) -> tuple[dict[str, object], str]:
    value = configuration.model_dump(mode="json")
    # Pure LLM runs must remain reproducible after an administrator revises a
    # template. Store the selected template verbatim in the immutable profile
    # snapshot rather than resolving it again in a later worker process.
    if configuration.preset == "llm":
        prompt = session.get(PromptTemplate, configuration.prompt_version)
        if prompt is None and str(configuration.prompt_version) == DEFAULT_PROMPT_TEMPLATE_ID:
            value["prompt_snapshot"] = {
                "name": "默认事实抽取",
                "text_prompt": TEXT_PROMPT,
                "table_prompt": TABLE_PROMPT,
            }
        elif prompt is None or not prompt.is_enabled:
            raise ProfileModelError("prompt template is unavailable")
        else:
            value["prompt_snapshot"] = {
                "name": prompt.name,
                "text_prompt": prompt.text_prompt,
                "table_prompt": prompt.table_prompt,
            }
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    normalized = json.loads(canonical)
    return normalized, hashlib.sha256(canonical).hexdigest()


def create_profile(
    session: Session,
    *,
    project_id: UUID,
    name: str,
    configuration: ProfileConfiguration,
    created_by_id: UUID,
) -> ProfileVersion:
    validate_model_references(session, configuration)
    snapshot, digest = _snapshot(session, configuration)
    profile = ExtractionProfile(project_id=project_id, name=name)
    version = ProfileVersion(
        profile=profile,
        created_by_id=created_by_id,
        version_number=1,
        snapshot_json=snapshot,
        snapshot_sha256=digest,
    )
    session.add(version)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="profile name already exists in project"
        ) from None
    session.refresh(version)
    return version


def get_project_profile(
    session: Session, *, project_id: UUID, profile_id: UUID
) -> ExtractionProfile:
    profile = session.scalar(
        select(ExtractionProfile).where(
            ExtractionProfile.id == profile_id,
            ExtractionProfile.project_id == project_id,
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile


def create_profile_version(
    session: Session,
    *,
    profile: ExtractionProfile,
    configuration: ProfileConfiguration,
    created_by_id: UUID,
) -> ProfileVersion:
    validate_model_references(session, configuration)
    snapshot, digest = _snapshot(session, configuration)
    version_number = session.scalar(
        update(ExtractionProfile)
        .where(ExtractionProfile.id == profile.id)
        .values(next_version_number=ExtractionProfile.next_version_number + 1)
        .returning(ExtractionProfile.next_version_number - 1)
    )
    if version_number is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="profile not found")
    version = ProfileVersion(
        profile_id=profile.id,
        created_by_id=created_by_id,
        version_number=version_number,
        snapshot_json=snapshot,
        snapshot_sha256=digest,
    )
    session.add(version)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="profile version conflict") from None
    session.refresh(version)
    return version
