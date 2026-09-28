from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, current_user
from app.auth.models import User
from app.models.models import ModelConfig
from app.models.crypto import ModelSecretError
from app.models.schemas import (
    EnabledModelResponse, ModelConfigCreate, ModelConfigResponse, ModelConfigUpdate, PriceVersionCreate,
    PriceVersionResponse, SecretRotate, UsagePage,
)
from app.models.service import aggregate_usage, create_model_config, create_price_version, rotate_model_secret
from app.projects.models import ProjectMembership


router = APIRouter(tags=["models"])


def _admin(user: User) -> None:
    if not any(role.name == "admin" for role in user.roles):
        raise HTTPException(status_code=403, detail="administrator required")


def _get_config(session: Session, model_id: UUID) -> ModelConfig:
    config = session.get(ModelConfig, model_id)
    if config is None:
        raise HTTPException(status_code=404, detail="model configuration not found")
    return config


@router.get("/api/models", response_model=list[EnabledModelResponse])
def list_enabled_models(
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[EnabledModelResponse]:
    granted = {
        permission.code for role in user.roles for permission in role.permissions
    }
    if "records:create" not in granted:
        raise HTTPException(status_code=403, detail="permission denied")
    return [
        EnabledModelResponse.model_validate(value)
        for value in session.scalars(
            select(ModelConfig)
            .where(ModelConfig.is_enabled.is_(True))
            .order_by(ModelConfig.name, ModelConfig.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.post("/api/admin/models", response_model=ModelConfigResponse, status_code=201)
def create_config(payload: ModelConfigCreate, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> ModelConfigResponse:
    _admin(user)
    try:
        config = create_model_config(session, **payload.model_dump())
    except ModelSecretError:
        session.rollback()
        raise HTTPException(status_code=503, detail="model credential is unavailable") from None
    except (IntegrityError, ValueError):
        session.rollback()
        raise HTTPException(status_code=409, detail="model configuration conflicts") from None
    return ModelConfigResponse.model_validate(config)


@router.get("/api/admin/models", response_model=list[ModelConfigResponse])
def list_configs(session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)],
                 offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
                 limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[ModelConfigResponse]:
    _admin(user)
    return [ModelConfigResponse.model_validate(value) for value in session.scalars(select(ModelConfig).order_by(ModelConfig.created_at, ModelConfig.id).offset(offset).limit(limit))]


@router.get("/api/admin/models/{model_id}", response_model=ModelConfigResponse)
def get_config(model_id: UUID, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> ModelConfigResponse:
    _admin(user)
    return ModelConfigResponse.model_validate(_get_config(session, model_id))


@router.patch("/api/admin/models/{model_id}", response_model=ModelConfigResponse)
def update_config(model_id: UUID, payload: ModelConfigUpdate, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> ModelConfigResponse:
    _admin(user)
    config = _get_config(session, model_id)
    values = payload.model_dump(exclude_unset=True)
    if "allowed_hosts" in values:
        values["allowed_hosts"] = list(values["allowed_hosts"])
    # Validate the complete security policy, including unchanged values.
    candidate = ModelConfigCreate(
        name=values.get("name", config.name), provider=config.provider,
        endpoint=values.get("endpoint", config.endpoint), model_name=values.get("model_name", config.model_name),
        api_key="masked-not-used", allowed_hosts=values.get("allowed_hosts", config.allowed_hosts),
        allow_private_network=values.get("allow_private_network", config.allow_private_network),
        allow_insecure_http=values.get("allow_insecure_http", config.allow_insecure_http),
        provider_supports_idempotency=values.get("provider_supports_idempotency", config.provider_supports_idempotency),
        is_enabled=values.get("is_enabled", config.is_enabled),
    )
    for key in values:
        setattr(config, key, getattr(candidate, key))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="model configuration conflicts") from None
    session.refresh(config)
    return ModelConfigResponse.model_validate(config)


@router.delete("/api/admin/models/{model_id}", status_code=204)
def delete_config(model_id: UUID, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> None:
    _admin(user)
    config = _get_config(session, model_id)
    session.delete(config)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="model configuration has historical references; disable it instead") from None


@router.post("/api/admin/models/{model_id}/secret", response_model=ModelConfigResponse)
def rotate_secret(model_id: UUID, payload: SecretRotate, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> ModelConfigResponse:
    _admin(user)
    config = _get_config(session, model_id)
    try:
        rotate_model_secret(session, config, payload.api_key, payload.key_id)
    except ModelSecretError:
        session.rollback()
        raise HTTPException(status_code=503, detail="model credential is unavailable") from None
    session.refresh(config)
    return ModelConfigResponse.model_validate(config)


@router.post("/api/admin/models/{model_id}/prices", response_model=PriceVersionResponse, status_code=201)
def create_price(model_id: UUID, payload: PriceVersionCreate, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> PriceVersionResponse:
    _admin(user); _get_config(session, model_id)
    try:
        price = create_price_version(session, model_config_id=model_id, **payload.model_dump())
    except (IntegrityError, ValueError):
        session.rollback()
        raise HTTPException(status_code=409, detail="price interval conflicts") from None
    return PriceVersionResponse.model_validate(price)


@router.get("/api/usage", response_model=UsagePage)
def usage(session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)],
          group_by: Literal["user","project","batch","document","model","purpose","status"] = "model",
          user_id: UUID | None = None, project_id: UUID | None = None, batch_id: UUID | None = None,
          document_id: UUID | None = None, model_config_id: UUID | None = None,
          purpose: Annotated[str | None, Query(max_length=64)] = None,
          status: Literal["success","failure","retry","cache_hit"] | None = None,
          start: datetime | None = None, end: datetime | None = None,
          offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
          limit: Annotated[int, Query(ge=1, le=500)] = 50) -> UsagePage:
    is_admin = any(role.name == "admin" for role in user.roles)
    allowed = None if is_admin else list(session.scalars(select(ProjectMembership.project_id).where(ProjectMembership.user_id == user.id)))
    if not is_admin and project_id is not None and project_id not in set(allowed or []):
        raise HTTPException(status_code=403, detail="project access denied")
    items, total = aggregate_usage(session, project_ids=allowed, group_by=group_by, user_id=user_id,
        project_id=project_id, batch_id=batch_id, document_id=document_id, model_config_id=model_config_id,
        purpose=purpose, status=status, start=start, end=end, offset=offset, limit=limit)
    return UsagePage(items=items, total=total, offset=offset, limit=limit)
