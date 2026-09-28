from __future__ import annotations

import base64


def test_document_responses_never_expose_storage_keys(client_factory, project):
    client = client_factory("operator")
    created = client.post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("paper.md", b"paper", "text/markdown")},
    )

    assert created.status_code == 201
    assert "storage_key" not in created.json()
    listed = client.get(f"/api/projects/{project.id}/documents")
    assert listed.status_code == 200
    assert "storage_key" not in listed.json()[0]


def test_operator_can_list_profile_summaries_and_enabled_models(
    client_factory, project, monkeypatch
):
    monkeypatch.setenv(
        "EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"k" * 32).decode()
    )
    configured = client_factory("admin").post(
        "/api/admin/models",
        json={
            "name": "内网模型", "provider": "openai-compatible",
            "endpoint": "https://models.example/v1/chat/completions",
            "model_name": "extractor", "api_key": "never-return-this",
            "allowed_hosts": ["models.example"], "is_enabled": True,
        },
    )
    assert configured.status_code == 201
    operator = client_factory("operator")
    profile = operator.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "四路方案", "preset": "custom", "routes": {
            "text_rule": False, "text_llm": False,
            "table_rule": False, "table_llm": True,
        }, "model_config_ids": [configured.json()["id"]]},
    )
    assert profile.status_code == 201

    profiles = operator.get(f"/api/projects/{project.id}/profiles")
    models = operator.get("/api/models")

    assert profiles.status_code == 200
    assert profiles.json()[0]["latest_version"]["id"] == profile.json()["id"]
    assert models.status_code == 200
    assert models.json() == [{
        "id": configured.json()["id"], "name": "内网模型",
        "provider": "openai-compatible", "model_name": "extractor",
    }]
    assert "endpoint" not in models.text
    assert "never-return-this" not in models.text


def test_batch_list_detail_are_project_scoped_and_retry_is_safe(
    client_factory, project, foreign_project
):
    client = client_factory("operator")
    profile = client.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "rule", "preset": "rule"},
    ).json()
    document = client.post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("paper.md", b"paper", "text/markdown")},
    ).json()
    batch = client.post(
        f"/api/projects/{project.id}/batches",
        json={
            "profile_version_id": profile["id"],
            "document_version_ids": [document["id"]],
        },
    ).json()

    listed = client.get(f"/api/projects/{project.id}/batches?limit=25")
    detail = client.get(f"/api/projects/{project.id}/batches/{batch['id']}")
    wrong = client.get(f"/api/projects/{foreign_project.id}/batches/{batch['id']}")
    queued_step = batch["document_jobs"][0]["steps"][0]
    not_retryable = client.post(
        f"/api/projects/{project.id}/batches/{batch['id']}/steps/{queued_step['id']}/retry",
        json={"expected_version": queued_step["version"]},
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == batch["id"]
    assert detail.status_code == 200
    assert wrong.status_code in (403, 404)
    assert not_retryable.status_code == 409


def test_batch_progress_stream_requires_header_auth_and_is_project_scoped(
    client_factory, project, foreign_project
):
    client = client_factory("operator")
    profile = client.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "progress", "preset": "rule"},
    ).json()
    document = client.post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("paper.md", b"paper", "text/markdown")},
    ).json()
    batch = client.post(
        f"/api/projects/{project.id}/batches",
        json={"profile_version_id": profile["id"], "document_version_ids": [document["id"]]},
    ).json()

    stream = client.get(
        f"/api/projects/{project.id}/batches/{batch['id']}/events"
    )
    wrong = client.get(
        f"/api/projects/{foreign_project.id}/batches/{batch['id']}/events"
    )

    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert '"status":"queued"' in stream.text
    assert wrong.status_code in (403, 404)


def test_llm_profile_requires_an_enabled_existing_model(
    client_factory, project, monkeypatch
):
    monkeypatch.setenv(
        "EXTRACTION_MODEL_MASTER_KEY", base64.b64encode(b"k" * 32).decode()
    )
    operator = client_factory("operator")
    missing = operator.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "missing", "preset": "llm"},
    )
    unknown = operator.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "unknown", "preset": "llm", "model_config_ids": [
            "01234567-89ab-cdef-8123-456789abcdef"
        ]},
    )
    configured = client_factory("admin").post(
        "/api/admin/models",
        json={
            "name": "disabled-model", "provider": "openai-compatible",
            "endpoint": "https://models.example/v1/chat/completions",
            "model_name": "extractor", "api_key": "secret",
            "allowed_hosts": ["models.example"], "is_enabled": False,
        },
    ).json()
    disabled = operator.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "disabled", "preset": "llm", "model_config_ids": [configured["id"]]},
    )

    assert missing.status_code == 422
    assert unknown.status_code == 422
    assert disabled.status_code == 422


def test_admin_user_list_is_bounded_and_never_returns_password_hash(client_factory):
    response = client_factory("admin").get("/api/admin/users?offset=0&limit=2")

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert all(set(item) == {"id", "username", "is_disabled", "roles"} for item in response.json())
    assert "password" not in response.text.lower()
    assert client_factory("operator").get("/api/admin/users").status_code == 403
    assert client_factory("admin").get("/api/admin/users?limit=501").status_code == 422
