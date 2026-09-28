from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.models import ModelCall, ModelPriceVersion
from app.models.models import ModelConfig
from app.models.service import CallScope, calculate_cost, create_price_version, record_model_call
from app.documents.models import Document, DocumentVersion
from app.jobs.service import create_batch
from app.profiles.models import ExtractionProfile, ProfileVersion
import hashlib


def test_cost_uses_price_effective_at_call_time():
    model_id = uuid4()
    call = ModelCall(model_config_id=model_id, called_at=datetime(2026,7,16,tzinfo=UTC),
                     prompt_tokens=1000, completion_tokens=500, cached_tokens=0)
    price = ModelPriceVersion(model_config_id=model_id, effective_from=datetime(2026,7,1,tzinfo=UTC), effective_to=None,
        prompt_per_million=Decimal("2"), completion_per_million=Decimal("8"), cached_per_million=Decimal("1"), currency="CNY")
    assert calculate_cost(call, price) == Decimal("0.0060000000")


def test_cached_tokens_use_separate_price():
    model_id = uuid4()
    call = ModelCall(model_config_id=model_id, called_at=datetime(2026,7,16,tzinfo=UTC), prompt_tokens=0, completion_tokens=0, cached_tokens=2500)
    price = ModelPriceVersion(model_config_id=model_id, effective_from=datetime(2026,7,1,tzinfo=UTC), effective_to=None,
        prompt_per_million=Decimal("9"), completion_per_million=Decimal("9"), cached_per_million=Decimal("1.2"), currency="CNY")
    assert calculate_cost(call, price) == Decimal("0.0030000000")


def test_price_outside_interval_is_rejected():
    model_id = uuid4()
    call = ModelCall(model_config_id=model_id, called_at=datetime(2026,6,30,tzinfo=UTC), prompt_tokens=0, completion_tokens=0, cached_tokens=0)
    price = ModelPriceVersion(model_config_id=model_id, effective_from=datetime(2026,7,1,tzinfo=UTC), effective_to=None,
        prompt_per_million=Decimal("1"), completion_per_million=Decimal("1"), cached_per_million=Decimal("1"), currency="CNY")
    with pytest.raises(ValueError, match="not effective"):
        calculate_cost(call, price)


def _usage_graph(session, project, user):
    profile = ExtractionProfile(project_id=project.id, name=f"usage-{uuid4()}")
    version = ProfileVersion(profile=profile, created_by_id=user.id, version_number=1,
        snapshot_json={"routes":{"text_llm":True},"plugin_version":"p","prompt_version":"q"}, snapshot_sha256="a"*64)
    document = Document(project_id=project.id, created_by_id=user.id)
    docv = DocumentVersion(document=document,uploader_id=user.id,version_number=1,original_filename="usage.md",
        storage_key=hashlib.sha256(uuid4().bytes).hexdigest(),sha256=hashlib.sha256(b"x").hexdigest(),size_bytes=1,mime_type="text/markdown",is_extractable=True)
    config = ModelConfig(name=f"model-{uuid4()}",provider="openai-compatible",endpoint="https://models.example/v1/chat/completions",
        model_name="extractor",encrypted_api_key=b"encrypted",allowed_hosts=["models.example"],allow_private_network=False,
        allow_insecure_http=False,provider_supports_idempotency=True,is_enabled=True)
    session.add_all([version,docv,config]); session.flush()
    batch=create_batch(session,version.id,[docv.id]); step=next(s for s in batch.document_jobs[0].steps if s.kind=="llm_text")
    price=create_price_version(session,model_config_id=config.id,effective_from=datetime(2026,1,1,tzinfo=UTC),effective_to=None,
        prompt_per_million=Decimal("2"),completion_per_million=Decimal("8"),cached_per_million=Decimal("1"),currency="CNY")
    scope=CallScope(user.id,project.id,batch.id,document.id,docv.id,batch.document_jobs[0].id,step.id)
    return config,price,scope


def test_model_call_is_idempotent_costed_and_immutable(app_session_factory, project, users):
    with app_session_factory() as session:
        config,_price,scope=_usage_graph(session,project,users["operator"])
        kwargs=dict(scope=scope,model_config_id=config.id,purpose="fact_extraction",route="llm_text",status="success",
            idempotency_key="b"*64,attempt=1,prompt_tokens=1000,completion_tokens=500,called_at=datetime(2026,7,1,tzinfo=UTC),completed_at=datetime(2026,7,1,tzinfo=UTC))
        first=record_model_call(session,**kwargs); second=record_model_call(session,**kwargs)
        assert second.id==first.id and first.cost==Decimal("0.0060000000")
        assert (first.provider,first.model_name,first.endpoint)==("openai-compatible","extractor","https://models.example/v1/chat/completions")
        config.provider="changed-provider"; config.model_name="changed-model"; config.endpoint="https://changed.example/v1"
        session.commit(); session.refresh(first)
        assert (first.provider,first.model_name,first.endpoint)==("openai-compatible","extractor","https://models.example/v1/chat/completions")
        first.error_code="changed"
        with pytest.raises(ValueError,match="immutable"):
            session.commit()


def test_usage_api_is_project_scoped_and_aggregated(app_session_factory, client_factory, project, users):
    with app_session_factory() as session:
        config,_price,scope=_usage_graph(session,project,users["operator"])
        record_model_call(session,scope=scope,model_config_id=config.id,purpose="fact_extraction",route="llm_text",status="success",
            idempotency_key="c"*64,attempt=1,prompt_tokens=10,completion_tokens=5,called_at=datetime(2026,7,1,tzinfo=UTC),completed_at=datetime(2026,7,1,tzinfo=UTC))
    member=client_factory("operator").get(f"/api/usage?project_id={project.id}&group_by=model")
    assert member.status_code==200
    assert member.json()["total"]==1 and member.json()["items"][0]["total_tokens"]==15
    assert client_factory("outsider").get(f"/api/usage?project_id={project.id}").status_code==403
    assert client_factory("admin").get(f"/api/usage?project_id={project.id}").json()["total"]==1


def test_overlapping_prices_rejected(app_session_factory, project, users):
    with app_session_factory() as session:
        config,_price,_scope=_usage_graph(session,project,users["operator"])
        with pytest.raises(ValueError,match="overlaps"):
            create_price_version(session,model_config_id=config.id,effective_from=datetime(2026,6,1,tzinfo=UTC),effective_to=datetime(2026,8,1,tzinfo=UTC),
                prompt_per_million=Decimal("1"),completion_per_million=Decimal("1"),cached_per_million=Decimal("1"),currency="CNY")


def test_usage_separates_same_group_key_by_currency(app_session_factory, project, users):
    from app.models.service import aggregate_usage
    with app_session_factory() as session:
        cny,_price,scope=_usage_graph(session,project,users["operator"])
        record_model_call(session,scope=scope,model_config_id=cny.id,purpose="fact_extraction",route="llm_text",status="success",
            idempotency_key="1"*64,attempt=1,prompt_tokens=1,called_at=datetime(2026,7,1,tzinfo=UTC),completed_at=datetime(2026,7,1,tzinfo=UTC))
        usd=ModelConfig(name=f"usd-{uuid4()}",provider="openai-compatible",endpoint="https://models.example/v1",model_name="extractor",
            encrypted_api_key=b"encrypted",allowed_hosts=["models.example"],allow_private_network=False,allow_insecure_http=False,provider_supports_idempotency=True,is_enabled=True)
        session.add(usd); session.commit()
        create_price_version(session,model_config_id=usd.id,effective_from=datetime(2026,1,1,tzinfo=UTC),effective_to=None,
            prompt_per_million=Decimal("3"),completion_per_million=Decimal("0"),cached_per_million=Decimal("0"),currency="USD")
        record_model_call(session,scope=scope,model_config_id=usd.id,purpose="fact_extraction",route="llm_text",status="success",
            idempotency_key="2"*64,attempt=1,prompt_tokens=1,called_at=datetime(2026,7,1,tzinfo=UTC),completed_at=datetime(2026,7,1,tzinfo=UTC))
        items,total=aggregate_usage(session,project_ids=None,group_by="purpose",project_id=project.id)
        assert total==2
        assert {(item["key"],item["currency"]) for item in items}=={("fact_extraction","CNY"),("fact_extraction","USD")}
