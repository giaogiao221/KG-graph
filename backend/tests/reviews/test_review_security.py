from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.facts.importer import import_facts
from app.facts.models import RawFact
from app.facts.schema59 import FIELD
from app.reviews.models import ReviewTask
from app.reviews.service import (InvalidReview, LeaseConflict, VersionConflict,
    claim_review_task, create_review_task, ensure_raw_root, review_fact)
from tests.facts.test_importer import _job, _row, _write


def _claimed(session, project, users, tmp_path: Path):
    steps = _job(session, project.id, users["operator"].id)
    import_facts(session, steps["validate"].id, _write(tmp_path / "secure.tsv", [_row(steps)]))
    fact = session.scalar(select(RawFact))
    root=ensure_raw_root(session,fact.id,users["admin"].id)
    task = create_review_task(session, root.id, users["admin"].id)
    task = claim_review_task(session, task.id, users["admin"].id)
    return root, task


def test_review_requires_live_owned_lease_and_completes_atomically(
    app_session_factory, project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        root, task = _claimed(session, project, users, tmp_path)
        version = review_fact(session, root.id, "approve", {}, 0, users["admin"].id,
                              review_task_id=task.id,
                              expected_lease_version=task.lease_version,
                              idempotency_key="secure-1")
        assert version.root_id == task.root_id
        session.refresh(task)
        assert (task.status, task.reviewer_id, task.lease_expires_at) == (
            "completed", users["admin"].id, None)


def test_review_rejects_wrong_or_expired_lease_and_outsider_service_call(
    app_session_factory, project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        root, task = _claimed(session, project, users, tmp_path)
        with pytest.raises(InvalidReview):
            review_fact(session, root.id, "approve", {}, 0, users["viewer"].id,
                        review_task_id=task.id, expected_lease_version=task.lease_version)
        with pytest.raises(LeaseConflict):
            review_fact(session, root.id, "approve", {}, 0, users["admin"].id,
                        review_task_id=task.id, expected_lease_version=task.lease_version,
                        now=task.lease_expires_at+timedelta(seconds=1))


def test_idempotency_key_conflicts_when_request_fingerprint_differs(
    app_session_factory, project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        root, task = _claimed(session, project, users, tmp_path)
        review_fact(session, root.id, "approve", {}, 0, users["admin"].id,
                    review_task_id=task.id, expected_lease_version=task.lease_version,
                    idempotency_key="same-key")
        with pytest.raises(VersionConflict):
            review_fact(session, root.id, "reject", {}, 0, users["admin"].id,
                        review_task_id=task.id, expected_lease_version=task.lease_version,
                        idempotency_key="same-key")


def test_patch_whitelist_rejects_metadata_and_surrogates(
    app_session_factory, project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        root, task = _claimed(session, project, users, tmp_path)
        for patch in ({FIELD["route"]: "x"}, {"graph_fact_key": "x"},
                      {FIELD["subject"]: "bad\ud800"}):
            with pytest.raises(InvalidReview):
                review_fact(session, root.id, "modify_approve", patch, 0,
                            users["admin"].id, review_task_id=task.id,
                            expected_lease_version=task.lease_version)


def test_ensure_raw_root_authorizes_before_creating(app_session_factory,project,users,tmp_path):
    from app.reviews.models import ReviewFactRoot
    steps_session=app_session_factory()
    try:
        steps=_job(steps_session,project.id,users["operator"].id)
        import_facts(steps_session,steps["validate"].id,_write(tmp_path/"auth-root.tsv",[_row(steps)]));steps_session.commit()
        fact=steps_session.scalar(select(RawFact))
        existing=steps_session.scalar(select(ReviewFactRoot))
        assert existing is not None
        with pytest.raises(InvalidReview):ensure_raw_root(steps_session,fact.id,users["viewer"].id)
        assert steps_session.scalar(select(ReviewFactRoot)).id == existing.id
    finally:steps_session.close()
