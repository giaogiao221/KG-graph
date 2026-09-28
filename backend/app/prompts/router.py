from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, current_user, require_permissions
from app.auth.models import User
from app.prompts.models import PromptTemplate
from app.prompts.schemas import PromptTemplateCreate, PromptTemplateResponse, PromptTemplateUpdate


router = APIRouter(tags=["prompt templates"])


def _admin(user: User) -> None:
    if not any(role.name == "admin" for role in user.roles):
        raise HTTPException(status_code=403, detail="administrator required")


def _template(session: Session, prompt_id: UUID) -> PromptTemplate:
    value = session.get(PromptTemplate, prompt_id)
    if value is None:
        raise HTTPException(status_code=404, detail="prompt template not found")
    return value


@router.get("/api/prompts", response_model=list[PromptTemplateResponse])
def list_enabled_prompts(
    session: Annotated[Session, Depends(auth_session)],
    _user: Annotated[User, Depends(require_permissions("records:create"))],
) -> list[PromptTemplateResponse]:
    return [PromptTemplateResponse.model_validate(item) for item in session.scalars(
        select(PromptTemplate).where(PromptTemplate.is_enabled.is_(True)).order_by(PromptTemplate.name, PromptTemplate.id)
    )]


@router.get("/api/admin/prompts", response_model=list[PromptTemplateResponse])
def list_prompts(
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PromptTemplateResponse]:
    _admin(user)
    return [PromptTemplateResponse.model_validate(item) for item in session.scalars(
        select(PromptTemplate).order_by(PromptTemplate.name, PromptTemplate.id).offset(offset).limit(limit)
    )]


@router.post("/api/admin/prompts", response_model=PromptTemplateResponse, status_code=201)
def create_prompt(payload: PromptTemplateCreate, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> PromptTemplateResponse:
    _admin(user)
    value = PromptTemplate(**payload.model_dump())
    session.add(value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="prompt template conflicts") from None
    session.refresh(value)
    return PromptTemplateResponse.model_validate(value)


@router.patch("/api/admin/prompts/{prompt_id}", response_model=PromptTemplateResponse)
def update_prompt(prompt_id: UUID, payload: PromptTemplateUpdate, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> PromptTemplateResponse:
    _admin(user)
    value = _template(session, prompt_id)
    for key, item in payload.model_dump(exclude_unset=True).items():
        setattr(value, key, item)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="prompt template conflicts") from None
    session.refresh(value)
    return PromptTemplateResponse.model_validate(value)


@router.delete("/api/admin/prompts/{prompt_id}", status_code=204)
def delete_prompt(prompt_id: UUID, session: Annotated[Session, Depends(auth_session)], user: Annotated[User, Depends(current_user)]) -> None:
    _admin(user)
    session.delete(_template(session, prompt_id))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="prompt template has historical references; disable it instead") from None
