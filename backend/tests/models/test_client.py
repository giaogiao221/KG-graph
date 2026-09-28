from __future__ import annotations

import csv
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.extraction.contracts import AdapterContext
from app.extraction.contracts import PermanentAdapterError, RetryableAdapterError
from app.extraction.native.common import ModelResult
from app.extraction.native.llm_text import LLMTextAdapter
from app.models.client import DatabaseModelClient
from app.models.models import ModelCall
from tests.models.test_usage import _usage_graph


class FakeProviderTransport:
    def __init__(self, owner: DatabaseModelClient): self.owner=owner; self.requests=0
    def extract(self, *, route, payload, idempotency_key, model_config_id):
        self.requests += 1
        call_id=self.owner._observe(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,
            metrics={"prompt_tokens":7,"completion_tokens":3,"total_tokens":10},error=None)
        return ModelResult(({"subject":"sample","property":"value","value":"1","evidence_text":payload},),
            {"prompt_tokens":7,"completion_tokens":3,"total_tokens":10,"model_calls":1},call_id)


def test_production_model_client_records_real_call_id_and_zero_token_cache_hit(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("EXTRACTION_CACHE_HMAC_KEY_ENV","TASK10_CACHE_KEY")
    monkeypatch.setenv("TASK10_CACHE_KEY","x"*40)
    with app_session_factory() as session:
        config,_price,scope=_usage_graph(session,project,users["operator"])
    client=DatabaseModelClient(app_session_factory,scope.step_id)
    transport=FakeProviderTransport(client); client._transport=transport
    document=tmp_path/"source.md"; document.write_text("sample has value 1",encoding="utf-8")
    context=AdapterContext(work_dir=tmp_path/"work",execution_idempotency_key="d"*64,
        step_id=scope.step_id,document_version_id=scope.document_version_id,document_path=document,
        model_config_id=config.id,persistent_cache_root=tmp_path/"cache")
    adapter=LLMTextAdapter(client=client)
    first=adapter.run(context,lambda _event:None)
    with (context.output_path("llm_text")/"candidates.schema59.tsv").open(encoding="utf-8",newline="") as source:
        first_call_id=next(csv.DictReader(source,delimiter="\t"))["模型调用ID"]
    second=adapter.run(context,lambda _event:None)
    assert transport.requests==1
    assert first.metrics["total_tokens"]==10 and second.metrics["total_tokens"]==0
    with app_session_factory() as session:
        calls=list(session.scalars(select(ModelCall).order_by(ModelCall.created_at)))
        assert [call.status for call in calls]==["success","cache_hit"]
        assert [call.total_tokens for call in calls]==[10,0]
        assert first_call_id==str(calls[0].id)
        cache_id=str(calls[1].id)
    with (context.output_path("llm_text")/"candidates.schema59.tsv").open(encoding="utf-8",newline="") as source:
        row=next(csv.DictReader(source,delimiter="\t"))
    assert row["模型调用ID"]==cache_id
    assert first_call_id != cache_id


def test_failure_and_retry_requests_are_recorded_separately(app_session_factory, project, users):
    with app_session_factory() as session:
        config,_price,scope=_usage_graph(session,project,users["operator"])
    client=DatabaseModelClient(app_session_factory,scope.step_id)
    retry_id=client._observe(route="llm_text",idempotency_key="e"*64,model_config_id=str(config.id),error=RetryableAdapterError("hidden provider body"))
    failure_id=client._observe(route="llm_text",idempotency_key="f"*64,model_config_id=str(config.id),error=PermanentAdapterError("hidden provider body"))
    with app_session_factory() as session:
        retry=session.get(ModelCall,UUID(retry_id)); failure=session.get(ModelCall,UUID(failure_id))
        assert (retry.status,retry.error_code,retry.total_tokens)==("retry","provider_unavailable",0)
        assert (failure.status,failure.error_code,failure.total_tokens)==("failure","provider_response_invalid",0)
        assert "hidden" not in str((retry.error_code,failure.error_code))
