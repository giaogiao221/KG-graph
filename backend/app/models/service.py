from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.crypto import decrypt_secret, encrypt_secret
from app.models.models import ModelCall, ModelConfig, ModelPriceVersion


COST_QUANTUM = Decimal("0.0000000001")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def calculate_cost(call: ModelCall, price: ModelPriceVersion) -> Decimal:
    if price.model_config_id != call.model_config_id:
        raise ValueError("price does not match model")
    called_at = _utc(call.called_at)
    if called_at < _utc(price.effective_from) or (price.effective_to is not None and called_at >= _utc(price.effective_to)):
        raise ValueError("price is not effective at call time")
    cost = (
        Decimal(call.prompt_tokens) * price.prompt_per_million
        + Decimal(call.completion_tokens) * price.completion_per_million
        + Decimal(call.cached_tokens) * price.cached_per_million
    ) / Decimal(1_000_000)
    return cost.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def create_model_config(session: Session, **values: object) -> ModelConfig:
    config = ModelConfig(
        name=values["name"], provider=values["provider"], endpoint=values["endpoint"],
        model_name=values["model_name"], encrypted_api_key=b"pending",
        allowed_hosts=list(values["allowed_hosts"]),
        allow_private_network=values["allow_private_network"],
        allow_insecure_http=values["allow_insecure_http"],
        provider_supports_idempotency=values["provider_supports_idempotency"],
        is_enabled=values["is_enabled"],
    )
    session.add(config)
    session.flush()
    config.encrypted_api_key = encrypt_secret(str(values["api_key"]), config_id=config.id, provider=config.provider)
    session.commit()
    session.refresh(config)
    return config


def rotate_model_secret(session: Session, config: ModelConfig, secret: str, key_id: str) -> None:
    config.encrypted_api_key = encrypt_secret(secret, config_id=config.id, provider=config.provider, key_id=key_id)
    config.key_id = key_id
    config.cipher_version = 1
    session.commit()


def resolved_secret(config: ModelConfig) -> str:
    if not config.is_enabled:
        from app.models.crypto import ModelSecretError
        raise ModelSecretError()
    return decrypt_secret(config.encrypted_api_key, config_id=config.id, provider=config.provider)


def create_price_version(session: Session, *, model_config_id: UUID, **values: object) -> ModelPriceVersion:
    start, end = values["effective_from"], values.get("effective_to")
    overlap = session.scalar(select(ModelPriceVersion.id).where(
        ModelPriceVersion.model_config_id == model_config_id,
        or_(ModelPriceVersion.effective_to.is_(None), ModelPriceVersion.effective_to > start),
        or_(end is None, ModelPriceVersion.effective_from < end) if end is not None else True,
    ).with_for_update())
    if overlap is not None:
        raise ValueError("price interval overlaps existing version")
    price = ModelPriceVersion(model_config_id=model_config_id, **values)
    session.add(price)
    session.commit()
    session.refresh(price)
    return price


def effective_price(session: Session, model_config_id: UUID, called_at: datetime) -> ModelPriceVersion | None:
    return session.scalar(select(ModelPriceVersion).where(
        ModelPriceVersion.model_config_id == model_config_id,
        ModelPriceVersion.effective_from <= called_at,
        or_(ModelPriceVersion.effective_to.is_(None), ModelPriceVersion.effective_to > called_at),
    ).order_by(ModelPriceVersion.effective_from.desc()).limit(1))


@dataclass(frozen=True, slots=True)
class CallScope:
    user_id: UUID
    project_id: UUID
    batch_id: UUID
    document_id: UUID
    document_version_id: UUID
    document_job_id: UUID
    step_id: UUID


def record_model_call(
    session: Session, *, scope: CallScope, model_config_id: UUID, purpose: str,
    route: Literal["llm_text", "llm_table"], status: Literal["success", "failure", "retry", "cache_hit"],
    idempotency_key: str, attempt: int, prompt_tokens: int = 0,
    completion_tokens: int = 0, cached_tokens: int = 0, error_code: str | None = None,
    called_at: datetime | None = None, completed_at: datetime | None = None,
) -> ModelCall:
    called_at = called_at or datetime.now(UTC)
    completed_at = completed_at or datetime.now(UTC)
    total = prompt_tokens + completion_tokens + cached_tokens
    price = effective_price(session, model_config_id, called_at)
    config = session.get(ModelConfig, model_config_id)
    if config is None:
        raise ValueError("model configuration not found")
    call = ModelCall(
        user_id=scope.user_id, project_id=scope.project_id, batch_id=scope.batch_id,
        document_id=scope.document_id, document_version_id=scope.document_version_id,
        document_job_id=scope.document_job_id, step_id=scope.step_id,
        model_config_id=model_config_id, price_version_id=price.id if price else None,
        provider=config.provider, model_name=config.model_name, endpoint=config.endpoint,
        purpose=purpose, route=route, status=status, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, cached_tokens=cached_tokens, total_tokens=total,
        cost=Decimal("0"), currency=price.currency if price else "CNY", error_code=error_code,
        idempotency_key=idempotency_key, attempt=attempt, called_at=called_at, completed_at=completed_at,
    )
    if price is not None:
        call.cost = calculate_cost(call, price)
    session.add(call)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(ModelCall).where(
            ModelCall.step_id == scope.step_id, ModelCall.idempotency_key == idempotency_key,
            ModelCall.attempt == attempt, ModelCall.status == status,
        ))
        if existing is None:
            raise
        return existing
    session.refresh(call)
    return call


def aggregate_usage(session: Session, *, project_ids: list[UUID] | None, group_by: str,
                    user_id: UUID | None = None, project_id: UUID | None = None,
                    batch_id: UUID | None = None, document_id: UUID | None = None,
                    model_config_id: UUID | None = None, purpose: str | None = None,
                    status: str | None = None, start: datetime | None = None,
                    end: datetime | None = None, offset: int = 0, limit: int = 50) -> tuple[list[dict[str, object]], int]:
    columns = {
        "user": ModelCall.user_id, "project": ModelCall.project_id,
        "batch": ModelCall.batch_id, "document": ModelCall.document_id,
        "model": ModelCall.model_config_id, "purpose": ModelCall.purpose,
        "status": ModelCall.status,
    }
    column = columns[group_by]
    conditions = []
    if project_ids is not None:
        if not project_ids:
            return [], 0
        conditions.append(ModelCall.project_id.in_(project_ids))
    for field, value in ((ModelCall.user_id,user_id),(ModelCall.project_id,project_id),(ModelCall.batch_id,batch_id),(ModelCall.document_id,document_id),(ModelCall.model_config_id,model_config_id),(ModelCall.purpose,purpose),(ModelCall.status,status)):
        if value is not None: conditions.append(field == value)
    if start is not None: conditions.append(ModelCall.called_at >= start)
    if end is not None: conditions.append(ModelCall.called_at < end)
    base = select(
        column.label("key"), func.count(ModelCall.id).label("calls"),
        func.sum(ModelCall.prompt_tokens).label("prompt_tokens"),
        func.sum(ModelCall.completion_tokens).label("completion_tokens"),
        func.sum(ModelCall.cached_tokens).label("cached_tokens"),
        func.sum(ModelCall.total_tokens).label("total_tokens"),
        func.sum(ModelCall.cost).label("cost"), ModelCall.currency.label("currency"),
    ).where(*conditions).group_by(column, ModelCall.currency)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(base.order_by(column, ModelCall.currency).offset(offset).limit(limit)).all()
    return [{"key": str(r.key), "calls": r.calls, "prompt_tokens": r.prompt_tokens or 0,
             "completion_tokens": r.completion_tokens or 0, "cached_tokens": r.cached_tokens or 0,
             "total_tokens": r.total_tokens or 0, "cost": r.cost or Decimal("0"), "currency": r.currency} for r in rows], int(total)
