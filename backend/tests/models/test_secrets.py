from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from app.models.crypto import ModelSecretError, decrypt_secret
from app.models.models import ModelConfig


def _payload(secret: str = "sk-never-reflect-this-value") -> dict[str, object]:
    return {"name":"secure-model","provider":"openai-compatible","endpoint":"https://models.example/v1/chat/completions",
            "model_name":"extractor","api_key":secret,"allowed_hosts":["models.example"],
            "allow_private_network":False,"allow_insecure_http":False,"provider_supports_idempotency":True,"is_enabled":True}


def test_admin_model_api_encrypts_and_masks_secret(client_factory, app_session_factory, monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"k"*32).decode())
    client = client_factory("admin"); secret = "sk-never-reflect-this-value"
    response = client.post("/api/admin/models", json=_payload(secret))
    assert response.status_code == 201
    assert response.json()["api_key"] == "********"
    assert secret not in response.text
    with app_session_factory() as session:
        config = session.scalar(select(ModelConfig))
        assert config is not None and secret.encode() not in config.encrypted_api_key
        assert decrypt_secret(config.encrypted_api_key, config_id=config.id, provider=config.provider) == secret
    assert secret not in client.get(f"/api/admin/models/{response.json()['id']}").text


def test_secret_is_never_reflected_on_validation_error(client_factory, monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"k"*32).decode())
    secret = "sk-secret-in-bad-request"
    payload = _payload(secret); payload["endpoint"] = secret
    response = client_factory("admin").post("/api/admin/models", json=payload)
    assert response.status_code == 422
    assert secret not in response.text


def test_wrong_master_key_has_fixed_safe_failure(client_factory, app_session_factory, monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"a"*32).decode())
    response = client_factory("admin").post("/api/admin/models", json=_payload())
    assert response.status_code == 201
    with app_session_factory() as session:
        config = session.scalar(select(ModelConfig))
        assert config is not None
        monkeypatch.setenv("EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"b"*32).decode())
        with pytest.raises(ModelSecretError, match="model credential is unavailable"):
            decrypt_secret(config.encrypted_api_key, config_id=config.id, provider=config.provider)


def test_non_admin_cannot_manage_models(client_factory, monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"k"*32).decode())
    assert client_factory("operator").post("/api/admin/models", json=_payload()).status_code == 403


def test_admin_model_list_has_bounded_stable_pagination(client_factory, monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"k"*32).decode())
    client=client_factory("admin")
    for number in range(2):
        payload=_payload(); payload["name"]=f"page-model-{number}"
        assert client.post("/api/admin/models",json=payload).status_code==201
    first=client.get("/api/admin/models?offset=0&limit=1")
    second=client.get("/api/admin/models?offset=1&limit=1")
    assert first.status_code==second.status_code==200
    assert len(first.json())==len(second.json())==1
    assert first.json()[0]["id"] != second.json()[0]["id"]
    assert client.get("/api/admin/models?limit=501").status_code==422
