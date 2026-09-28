from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.facts.importer import import_facts
from app.facts.models import RawFact
from app.facts.schema59 import evidence_hash
from app.reviews.models import FactVersion

from .test_importer import _job, _row, _write


def test_project_fact_query_is_scoped_paginated_filtered_and_safe(
    app_session_factory, client_factory, project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        rows = [
            _row(steps, graph_fact_key="alpha:mass"),
            _row(
                steps,
                graph_fact_key="beta:length",
                主体="Beta",
                属性="length",
                证据文本="Beta has length 4 m.",
                证据哈希="c3ab8ff13720e8ad9047dd39466b3c8974e592c2fa383d4a3960714c7b0c4f2c",
            ),
        ]
        # Keep the second row valid while exercising the API filter.
        rows[1]["证据哈希"] = evidence_hash(rows[1]["证据文本"])
        import_facts(session, steps["validate"].id, _write(tmp_path / "api.tsv", rows))

    operator = client_factory("operator")
    response = operator.get(
        f"/api/projects/{project.id}/facts",
        params={"subject": "Beta", "source": "merge", "limit": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 1
    assert payload["items"][0]["subject"] == "Beta"
    assert "row_json" not in payload["items"][0]
    assert set(payload["items"][0]) >= {
        "id", "document_id", "document_version_id", "document_job_id",
        "subject", "property", "value", "evidence_text", "review_status",
    }
    detail = operator.get(
        f"/api/projects/{project.id}/facts/{payload['items'][0]['id']}"
    )
    assert detail.status_code == 200
    assert len(detail.json()["row_json"]) == 59
    assert "Beta" in detail.json()["row_json"].values()
    assert "storage_key" not in detail.text

    outsider = client_factory("outsider")
    assert outsider.get(f"/api/projects/{project.id}/facts").status_code == 403
    assert client_factory("admin").get(
        f"/api/projects/{project.id}/facts", params={"offset": 1, "limit": 1}
    ).status_code == 200


def test_fact_query_rejects_unbounded_page(client_factory, project) -> None:
    response = client_factory("operator").get(
        f"/api/projects/{project.id}/facts", params={"limit": 501}
    )
    assert response.status_code == 422


def test_admin_can_batch_delete_selected_facts_with_audit_history(
    app_session_factory, client_factory, project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        rows = [_row(steps), _row(steps, graph_fact_key="beta:mass", 主体="Beta")]
        rows[1]["证据哈希"] = evidence_hash(rows[1]["证据文本"])
        import_facts(session, steps["validate"].id, _write(tmp_path / "delete.tsv", rows))
        fact_ids = [str(value) for value in session.scalars(select(RawFact.id)).all()]

    response = client_factory("admin").request(
        "DELETE", f"/api/projects/{project.id}/facts", json={"fact_ids": fact_ids}
    )
    assert response.status_code == 204
    assert client_factory("operator").get(
        f"/api/projects/{project.id}/facts"
    ).json()["total"] == 0
    with app_session_factory() as session:
        versions = list(session.scalars(select(FactVersion)).all())
        assert len(versions) == 2
        assert all(version.action == "delete" and version.is_tombstone for version in versions)
