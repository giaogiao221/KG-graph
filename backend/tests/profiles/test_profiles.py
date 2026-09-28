from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.profiles.models import ExtractionProfile, ProfileVersion
from app.profiles.schemas import ProfileVersionCreate, resolve_preset
from app.models.models import ModelConfig


RULE_VERSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PLUGIN_VERSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROMPT_VERSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _enabled_model(
    factory: sessionmaker[Session],
    model_id: str = "01234567-89ab-cdef-8123-456789abcdef",
) -> str:
    with factory.begin() as session:
        session.add(ModelConfig(
            id=UUID(model_id), name=f"test-model-{model_id}",
            provider="openai-compatible", endpoint="https://models.example/v1",
            model_name="extractor", encrypted_api_key=b"test-ciphertext",
            allowed_hosts=["models.example"], is_enabled=True,
        ))
    return model_id.lower()


class LeakyPayloadForTest(BaseModel):
    value: str

    @field_validator("value")
    @classmethod
    def reject_with_value_in_message(cls, value: str) -> str:
        raise ValueError(f"validator rejected user value: {value}")


async def leaky_validation_route(_payload: LeakyPayloadForTest) -> None:
    return None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rule", (True, False, True, False)),
        ("llm", (False, True, False, True)),
        ("hybrid", (True, True, True, True)),
    ],
)
def test_presets_map_to_four_independent_routes(name: str, expected: tuple[bool, ...]):
    route = resolve_preset(name)

    assert (
        route.text_rule,
        route.text_llm,
        route.table_rule,
        route.table_llm,
    ) == expected


def test_custom_profile_rejects_all_routes_disabled(client_factory, project) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "disabled", "preset": "custom", "routes": {}},
    )

    assert response.status_code == 422


def test_profile_version_snapshot_contains_complete_execution_configuration(
    client_factory, project, app_session_factory
) -> None:
    model_config_id = _enabled_model(app_session_factory)
    payload = {
        "name": "production",
        "preset": "custom",
        "routes": {
            "text_rule": True,
            "text_llm": False,
            "table_rule": False,
            "table_llm": True,
        },
        "model_config_ids": [model_config_id],
        "thresholds": {"confidence": 0.82, "table_gate": 0.7},
        "concurrency": 4,
        "rule_version": RULE_VERSION_ID,
        "plugin_version": PLUGIN_VERSION_ID,
        "prompt_version": PROMPT_VERSION_ID,
        "quality_strategy": {
            "evidence_required": True,
            "deduplicate": True,
            "schema59_validation": True,
            "fail_closed": True,
            "minimum_confidence": 0.76,
            "conflict_policy": "manual_review",
        },
    }

    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles", json=payload
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version_number"] == 1
    assert body["snapshot"] == {
        key: payload[key]
        for key in (
            "preset",
            "routes",
            "model_config_ids",
            "thresholds",
            "concurrency",
            "rule_version",
            "plugin_version",
            "prompt_version",
            "quality_strategy",
        )
    }


def test_saving_new_configuration_preserves_historical_snapshot(
    client_factory,
    project,
    app_session_factory: sessionmaker[Session],
) -> None:
    client = client_factory("operator")
    created = client.post(
        f"/api/projects/{project.id}/profiles",
        json={
            "name": "evolving",
            "preset": "rule",
            "thresholds": {"confidence": 0.5},
            "rule_version": RULE_VERSION_ID,
        },
    ).json()
    profile_id = created["profile_id"]
    original_snapshot = created["snapshot"]
    original_hash = created["snapshot_sha256"]
    model_config_id = _enabled_model(app_session_factory)

    response = client.post(
        f"/api/projects/{project.id}/profiles/{profile_id}/versions",
        json={
                "preset": "hybrid",
                "model_config_ids": [model_config_id],
            "thresholds": {"confidence": 0.95},
            "rule_version": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "prompt_version": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "quality_strategy": {
                "minimum_confidence": 0.91,
                "fail_closed": False,
            },
        },
    )

    assert response.status_code == 201
    assert response.json()["version_number"] == 2
    with app_session_factory() as session:
        versions = list(
            session.scalars(
                select(ProfileVersion)
                .where(ProfileVersion.profile_id == UUID(profile_id))
                .order_by(ProfileVersion.version_number)
            )
        )
    assert versions[0].snapshot_json == original_snapshot
    assert versions[0].snapshot_sha256 == original_hash
    assert versions[0].snapshot_json["rule_version"] == RULE_VERSION_ID
    assert versions[1].snapshot_json["rule_version"] == (
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    )
    assert versions[0].snapshot_json["quality_strategy"]["minimum_confidence"] == 0.0
    assert versions[1].snapshot_json["quality_strategy"] == {
        "evidence_required": True,
        "deduplicate": True,
        "schema59_validation": True,
        "fail_closed": False,
        "minimum_confidence": 0.91,
        "conflict_policy": "manual_review",
    }


def test_snapshot_hash_is_stable_for_equivalent_json_order(
    client_factory, project, app_session_factory
) -> None:
    client = client_factory("operator")
    model_ids = [_enabled_model(app_session_factory)]
    common = {
        "preset": "hybrid",
        "model_config_ids": model_ids,
        "thresholds": {"z": 0.2, "a": 0.1},
        "concurrency": 2,
        "rule_version": RULE_VERSION_ID,
        "plugin_version": PLUGIN_VERSION_ID,
        "prompt_version": PROMPT_VERSION_ID,
    }
    first = client.post(
        f"/api/projects/{project.id}/profiles", json={"name": "one", **common}
    ).json()
    reordered = {
        **common,
        "thresholds": {"a": 0.1, "z": 0.2},
    }
    second = client.post(
        f"/api/projects/{project.id}/profiles", json={"name": "two", **reordered}
    ).json()

    canonical = json.dumps(
        first["snapshot"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert first["snapshot_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert second["snapshot_sha256"] == first["snapshot_sha256"]


@pytest.mark.parametrize(
    "unsafe_configuration",
    [
        {"model_config_ids": ["sk-live-secret-value"]},
        {"model_config_ids": [r"C:\secrets\model.json"]},
        {"model_config_ids": ["/etc/extraction/model.json"]},
        {"model_config_ids": {"text_llm": "01234567-89ab-cdef-8123-456789abcdef"}},
        {"model_config_refs": {"text_llm": "arbitrary-value"}},
        {"authorization_header": "Bearer secret"},
    ],
)
def test_profile_rejects_unstructured_or_sensitive_model_configuration(
    client_factory, project, unsafe_configuration: dict[str, object]
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "unsafe", "preset": "llm", **unsafe_configuration},
    )

    assert response.status_code == 422
    response_text = response.text.lower()
    for forbidden in (
        "sk-live-secret-value",
        "c:\\secrets",
        "/etc/extraction",
        "arbitrary-value",
        "authorization_header",
        "bearer secret",
    ):
        assert forbidden not in response_text


def test_profile_accepts_opaque_model_config_uuid_and_serializes_canonically(
    client_factory, project, app_session_factory
) -> None:
    model_config_id = _enabled_model(app_session_factory).upper()

    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={
            "name": "opaque-reference",
            "preset": "llm",
            "model_config_ids": [model_config_id],
        },
    )

    assert response.status_code == 201
    snapshot = response.json()["snapshot"]
    assert snapshot["model_config_ids"] == [model_config_id.lower()]
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    assert "secret" not in serialized.lower()
    assert "header" not in serialized.lower()
    assert "path" not in serialized.lower()


@pytest.mark.parametrize(
    "quality_strategy",
    [
        {"minimum_confidence": -0.01},
        {"minimum_confidence": 1.01},
        {"evidence_required": False},
        {"deduplicate": False},
        {"schema59_validation": False},
        {"conflict_policy": "last_write_wins"},
        {"secret": "must-not-be-accepted"},
    ],
)
def test_quality_strategy_rejects_unsafe_or_unknown_values(
    client_factory, project, quality_strategy: dict[str, object]
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={
            "name": "unsafe-quality",
            "preset": "rule",
            "quality_strategy": quality_strategy,
        },
    )

    assert response.status_code == 422
    assert "must-not-be-accepted" not in response.text.lower()


@pytest.mark.parametrize(
    ("unsafe_configuration", "sensitive_fragment"),
    [
        ({"thresholds": {"api_key": 0.5}}, "api_key"),
        ({"thresholds": {"authorization_header": 0.5}}, "authorization_header"),
        ({"thresholds": {r"c:\\secrets\\gate": 0.5}}, r"c:\\secrets"),
        ({"thresholds": {"/etc/extraction/gate": 0.5}}, "/etc/extraction"),
        ({"rule_version": "Bearer top-secret"}, "top-secret"),
        ({"plugin_version": r"C:\\secrets\\plugin.json"}, r"c:\\secrets"),
        ({"prompt_version": "/etc/extraction/prompt.txt"}, "/etc/extraction"),
        ({"prompt_version": "prompt\nAuthorization: secret"}, "authorization"),
        ({"rule_version": "ghp_0123456789abcdefghijklmnopqrstuvwxyz"}, "ghp_"),
        ({"plugin_version": "AKIAIOSFODNN7EXAMPLE"}, "akia"),
        (
            {
                "prompt_version": (
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
                )
            },
            "eyjhbgcioi",
        ),
        ({"thresholds": {"a" * 33: 0.5}}, "a" * 33),
    ],
)
def test_free_form_configuration_fields_reject_sensitive_or_path_content(
    client_factory,
    project,
    app_session_factory: sessionmaker[Session],
    unsafe_configuration: dict[str, object],
    sensitive_fragment: str,
) -> None:
    with pytest.raises(ValidationError):
        ProfileVersionCreate.model_validate(
            {"preset": "rule", **unsafe_configuration}
        )

    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "must-not-persist", "preset": "rule", **unsafe_configuration},
    )

    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    assert body["detail"]
    assert all(set(error) <= {"loc", "type", "msg"} for error in body["detail"])
    assert all({"loc", "type", "msg"} <= set(error) for error in body["detail"])
    assert sensitive_fragment.lower() not in response.text.lower()
    with app_session_factory() as session:
        assert session.scalar(
            select(ExtractionProfile).where(
                ExtractionProfile.name == "must-not-persist"
            )
        ) is None


def test_uuid_version_references_and_safe_threshold_keys_are_preserved_in_snapshot(
    client_factory, project, app_session_factory
) -> None:
    model_config_id = _enabled_model(app_session_factory)
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={
            "name": "safe-identifiers",
            "preset": "hybrid",
            "model_config_ids": [model_config_id],
            "thresholds": {"confidence": 0.8, "table_gate": 0.65},
            "rule_version": RULE_VERSION_ID.upper(),
            "plugin_version": PLUGIN_VERSION_ID.upper(),
            "prompt_version": PROMPT_VERSION_ID.upper(),
        },
    )

    assert response.status_code == 201
    snapshot = response.json()["snapshot"]
    assert snapshot["thresholds"] == {"confidence": 0.8, "table_gate": 0.65}
    assert snapshot["rule_version"] == RULE_VERSION_ID
    assert snapshot["plugin_version"] == PLUGIN_VERSION_ID
    assert snapshot["prompt_version"] == PROMPT_VERSION_ID


def test_validation_errors_keep_safe_structure_without_input_or_context(
    client_factory, project
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={
            "name": "validation-shape",
            "preset": "rule",
            "rule_version": "Bearer do-not-echo-this-secret",
            "unexpected": {"header": "Authorization: do-not-echo-this-secret"},
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert all({"loc", "type", "msg"} <= set(error) for error in detail)
    assert all("input" not in error and "ctx" not in error for error in detail)
    assert "do-not-echo-this-secret" not in response.text


def test_validation_error_message_does_not_trust_validator_text(
    client_factory,
) -> None:
    client_factory.app.post("/api/test/leaky-validation")(leaky_validation_route)
    response = client_factory("operator").post(
        "/api/test/leaky-validation", json={"value": "do-not-echo-ghp_123456"}
    )

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["type"] == "value_error"
    assert error["msg"] == "invalid value"
    assert "do-not-echo-ghp_123456" not in response.text


def test_snapshot_configuration_rejects_non_finite_json_numbers() -> None:
    with pytest.raises(ValidationError):
        ProfileVersionCreate(
            preset="rule", thresholds={"confidence": float("nan")}
        )


def test_profile_access_is_project_scoped(client_factory, project) -> None:
    response = client_factory("outsider").post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "forbidden", "preset": "rule"},
    )

    assert response.status_code == 403


def test_profile_versions_are_immutable_at_database_boundary(
    client_factory,
    project,
    app_session_factory: sessionmaker[Session],
) -> None:
    created = client_factory("operator").post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "locked", "preset": "rule"},
    ).json()

    with app_session_factory() as session:
        with pytest.raises(DBAPIError):
            session.execute(
                update(ProfileVersion)
                .where(ProfileVersion.id == UUID(created["id"]))
                .values(snapshot_sha256="0" * 64)
            )


def test_offline_migration_defines_profile_version_bounds_and_defaults() -> None:
    backend_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment["EXTRACTION_DATABASE_URL"] = (
        "postgresql+psycopg://offline:offline@localhost/extraction"
    )

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "next_version_number INTEGER DEFAULT 2 NOT NULL" in result.stdout
    assert "version_number INTEGER DEFAULT 1 NOT NULL" in result.stdout
    assert "ck_extraction_profile_next_version_number" in result.stdout
    assert "next_version_number >= 2" in result.stdout
    assert "ck_profile_version_number" in result.stdout
    assert "version_number >= 1" in result.stdout
