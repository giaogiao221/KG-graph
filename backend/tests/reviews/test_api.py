from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.facts.importer import import_facts
from app.facts.models import RawFact
from tests.facts.test_importer import _job, _row, _write


def _fact_id(factory, project, user, tmp_path: Path):
    with factory.begin() as session:
        steps = _job(session, project.id, user.id)
        import_facts(session, steps["validate"].id, _write(tmp_path / "api-review.tsv", [_row(steps)]))
        return session.scalar(select(RawFact.id))


def test_reviewer_can_review_member_project_and_read_history(
    app_session_factory, client_factory, project, users, tmp_path
) -> None:
    fact_id = _fact_id(app_session_factory, project, users["operator"], tmp_path)
    client = client_factory("reviewer")
    task = client.post(f"/api/projects/{project.id}/review-tasks", json={"fact_id": str(fact_id)}).json()
    task = client.post(f"/api/projects/{project.id}/review-tasks/{task['id']}/claim").json()
    response = client.post(
        f"/api/projects/{project.id}/facts/{fact_id}/reviews",
        json={"action": "approve", "patch": {}, "expected_version": 0,
              "review_task_id": task["id"], "expected_lease_version": task["lease_version"],
              "idempotency_key": "api-1"},
    )
    assert response.status_code == 201
    history = client.get(f"/api/projects/{project.id}/facts/{fact_id}/versions")
    assert history.status_code == 200
    assert history.json()["total"] == 1


def test_reviewed_status_filters_and_batch_summary_use_latest_review_version(
    app_session_factory, client_factory, project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        rows = [
            _row(steps, graph_fact_key="review:approved"),
            _row(steps, graph_fact_key="review:rejected"),
        ]
        import_facts(
            session,
            steps["validate"].id,
            _write(tmp_path / "review-status.tsv", rows),
        )
        fact_ids = list(session.scalars(select(RawFact.id).order_by(RawFact.id)))

    reviewer = client_factory("reviewer")
    batch_id = None
    for index, (fact_id, action) in enumerate(
        zip(fact_ids, ("approve", "reject"), strict=True), start=1
    ):
        task = reviewer.post(
            f"/api/projects/{project.id}/review-tasks",
            json={"fact_id": str(fact_id)},
        ).json()
        batch_id = task["batch_id"]
        task = reviewer.post(
            f"/api/projects/{project.id}/review-tasks/{task['id']}/claim"
        ).json()
        reviewed = reviewer.post(
            f"/api/projects/{project.id}/facts/{fact_id}/reviews",
            json={
                "action": action,
                "patch": {},
                "expected_version": 0,
                "review_task_id": task["id"],
                "expected_lease_version": task["lease_version"],
                "idempotency_key": f"status-filter-{index}",
            },
        )
        assert reviewed.status_code == 201

    approved = reviewer.get(
        f"/api/projects/{project.id}/facts", params={"review_status": "approved"}
    )
    rejected = reviewer.get(
        f"/api/projects/{project.id}/facts", params={"review_status": "rejected"}
    )
    assert approved.status_code == rejected.status_code == 200
    assert approved.json()["total"] == 1
    assert approved.json()["items"][0]["review_status"] == "approved"
    assert rejected.json()["total"] == 1
    assert rejected.json()["items"][0]["review_status"] == "rejected"

    batches = reviewer.get(f"/api/projects/{project.id}/review-batches")
    assert batches.status_code == 200
    summary = next(
        item for item in batches.json()["items"] if item["batch_id"] == batch_id
    )
    assert (
        summary["total"], summary["pending"],
        summary["claimed"], summary["completed"],
    ) == (2, 0, 0, 2)


def test_operator_viewer_and_outsider_cannot_mutate_review(
    app_session_factory, client_factory, project, users, tmp_path
) -> None:
    fact_id = _fact_id(app_session_factory, project, users["operator"], tmp_path)
    payload = {"action": "approve", "patch": {}, "expected_version": 0,
               "review_task_id": "00000000-0000-0000-0000-000000000001",
               "expected_lease_version": 1}
    assert client_factory("operator").post(
        f"/api/projects/{project.id}/facts/{fact_id}/reviews", json=payload).status_code == 403
    assert client_factory("viewer").post(
        f"/api/projects/{project.id}/facts/{fact_id}/reviews", json=payload).status_code == 403
    assert client_factory("outsider").get(
        f"/api/projects/{project.id}/facts/{fact_id}/versions").status_code == 403


def test_cross_project_fact_link_is_hidden(
    app_session_factory, client_factory, project, foreign_project, users, tmp_path
) -> None:
    fact_id = _fact_id(app_session_factory, project, users["operator"], tmp_path)
    response = client_factory("admin").post(
        f"/api/projects/{foreign_project.id}/facts/{fact_id}/reviews",
        json={"action": "approve", "patch": {}, "expected_version": 0,
              "review_task_id": "00000000-0000-0000-0000-000000000001",
              "expected_lease_version": 1},
    )
    assert response.status_code == 404


def test_review_queue_is_bounded_and_claim_checks_project(
    app_session_factory, client_factory, project, users, tmp_path
) -> None:
    fact_id = _fact_id(app_session_factory, project, users["operator"], tmp_path)
    admin = client_factory("admin")
    created = admin.post(f"/api/projects/{project.id}/review-tasks", json={"fact_id": str(fact_id)})
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert client_factory("reviewer").post(
        f"/api/projects/{project.id}/review-tasks/{task_id}/assign",
        json={"reviewer_id": str(users["reviewer"].id)},
    ).status_code == 403
    claimed = client_factory("reviewer").post(f"/api/projects/{project.id}/review-tasks/{task_id}/claim")
    assert claimed.status_code == 200
    assert admin.get(f"/api/projects/{project.id}/review-tasks", params={"limit": 501}).status_code == 422


def test_reviewer_can_claim_selected_page_tasks_in_one_request(
    app_session_factory, client_factory, project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        import_facts(
            session,
            steps["validate"].id,
            _write(
                tmp_path / "batch-claim.tsv",
                [
                    _row(steps, graph_fact_key="batch-claim:one"),
                    _row(steps, graph_fact_key="batch-claim:two"),
                ],
            ),
        )
        fact_ids = list(session.scalars(select(RawFact.id).order_by(RawFact.id)))

    reviewer = client_factory("reviewer")
    task_ids = [
        reviewer.post(
            f"/api/projects/{project.id}/review-tasks",
            json={"fact_id": str(fact_id)},
        ).json()["id"]
        for fact_id in fact_ids
    ]
    response = reviewer.post(
        f"/api/projects/{project.id}/review-tasks/claim-batch",
        json={"task_ids": task_ids},
    )
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == set(task_ids)
    assert response.json()["skipped_task_ids"] == []

    claimed = reviewer.get(
        f"/api/projects/{project.id}/review-tasks",
        params={"status": "claimed", "reviewer_id": str(users["reviewer"].id)},
    )
    assert claimed.json()["total"] == 2


def test_claimed_queue_can_be_filtered_to_current_reviewer(
    app_session_factory, client_factory, project, users, tmp_path
) -> None:
    fact_id = _fact_id(app_session_factory, project, users["operator"], tmp_path)
    admin = client_factory("admin")
    created = admin.post(
        f"/api/projects/{project.id}/review-tasks", json={"fact_id": str(fact_id)}
    ).json()
    claimed = client_factory("reviewer").post(
        f"/api/projects/{project.id}/review-tasks/{created['id']}/claim"
    )
    assert claimed.status_code == 200

    reviewer = client_factory("reviewer")
    own = reviewer.get(
        f"/api/projects/{project.id}/review-tasks",
        params={"status": "claimed", "reviewer_id": str(users["reviewer"].id)},
    )
    assert own.status_code == 200
    assert own.json()["total"] == 1
    assert own.json()["items"][0]["id"] == created["id"]

    forbidden = reviewer.get(
        f"/api/projects/{project.id}/review-tasks",
        params={"status": "claimed", "reviewer_id": str(users["admin"].id)},
    )
    assert forbidden.status_code == 403

    admin_view = admin.get(
        f"/api/projects/{project.id}/review-tasks",
        params={"status": "claimed", "reviewer_id": str(users["reviewer"].id)},
    )
    assert admin_view.status_code == 200
    assert admin_view.json()["total"] == 1


def test_manual_fact_api_supports_list_modify_history_and_delete(
    client_factory, project
) -> None:
    client = client_factory("reviewer")
    created = client.post(
        f"/api/projects/{project.id}/manual-facts",
        json={
            "fields": {
                "subject": "manual subject",
                "property": "manual relation",
                "value": "old value",
                "evidence": "manual evidence",
            },
            "idempotency_key": "manual-api-crud-create",
        },
    )
    assert created.status_code == 201
    root_id = created.json()["root_id"]

    current = client.get(
        f"/api/projects/{project.id}/current-facts",
        params={"action": "manual_create"},
    )
    assert current.status_code == 200
    assert [item["root_id"] for item in current.json()["items"]] == [root_id]

    task = client.post(
        f"/api/projects/{project.id}/review-tasks", json={"root_id": root_id}
    ).json()
    task = client.post(
        f"/api/projects/{project.id}/review-tasks/{task['id']}/claim"
    ).json()
    modified = client.post(
        f"/api/projects/{project.id}/review-facts/{root_id}/reviews",
        json={
            "action": "modify_approve",
            "patch": {"value": "new value"},
            "expected_version": 1,
            "review_task_id": task["id"],
            "expected_lease_version": task["lease_version"],
            "idempotency_key": "manual-api-crud-update",
        },
    )
    assert modified.status_code == 201
    assert modified.json()["editable_values"]["value"] == "new value"

    history = client.get(
        f"/api/projects/{project.id}/review-facts/{root_id}/versions"
    )
    assert history.status_code == 200
    assert history.json()["total"] == 2

    task = client.post(
        f"/api/projects/{project.id}/review-tasks", json={"root_id": root_id}
    ).json()
    task = client.post(
        f"/api/projects/{project.id}/review-tasks/{task['id']}/claim"
    ).json()
    deleted = client.post(
        f"/api/projects/{project.id}/review-facts/{root_id}/reviews",
        json={
            "action": "delete",
            "patch": {},
            "expected_version": 2,
            "review_task_id": task["id"],
            "expected_lease_version": task["lease_version"],
            "idempotency_key": "manual-api-crud-delete",
        },
    )
    assert deleted.status_code == 201
    assert deleted.json()["is_tombstone"] is True

    current = client.get(f"/api/projects/{project.id}/current-facts")
    assert current.status_code == 200
    assert root_id not in [item["root_id"] for item in current.json()["items"]]
