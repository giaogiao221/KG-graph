import pytest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.settings import Settings, get_settings
from app.main import create_app


def test_health_returns_service_status():
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "extraction-review-platform",
    }


def test_get_settings_returns_settings(monkeypatch):
    secret = "x" * 32
    monkeypatch.setenv("EXTRACTION_JWT_SECRET", f"  {secret}  ")
    get_settings.cache_clear()

    try:
        settings = get_settings()
        assert isinstance(settings, Settings)
        assert settings.jwt_secret.get_secret_value() == secret
        assert secret not in repr(settings.jwt_secret)
    finally:
        get_settings.cache_clear()


def test_get_settings_requires_jwt_secret_without_echoing_values(monkeypatch):
    monkeypatch.delenv("EXTRACTION_JWT_SECRET", raising=False)
    get_settings.cache_clear()

    try:
        with pytest.raises(ValidationError) as exc_info:
            get_settings()
        assert "jwt_secret" in str(exc_info.value)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("secret", ["   ", "short-secret-value"])
def test_get_settings_rejects_blank_or_short_jwt_secret_without_echoing_it(
    monkeypatch, secret
):
    monkeypatch.setenv("EXTRACTION_JWT_SECRET", secret)
    get_settings.cache_clear()

    try:
        with pytest.raises(ValidationError) as exc_info:
            get_settings()
        if secret.strip():
            assert secret not in str(exc_info.value)
    finally:
        get_settings.cache_clear()


def test_get_settings_loads_prefixed_environment_variable(monkeypatch):
    monkeypatch.setenv("EXTRACTION_ENVIRONMENT", "testing")
    monkeypatch.setenv("EXTRACTION_JWT_SECRET", "x" * 32)
    get_settings.cache_clear()

    try:
        assert get_settings().environment == "testing"
    finally:
        get_settings.cache_clear()
