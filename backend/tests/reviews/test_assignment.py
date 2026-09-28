from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.facts.importer import import_facts
from app.facts.models import RawFact
from app.reviews.models import ReviewTask
from app.reviews.service import LeaseConflict, claim_review_task, create_raw_review_task, release_review_task, renew_review_task
from tests.facts.test_importer import _job, _row, _write


def _task(session, project, user, actor, tmp_path: Path) -> ReviewTask:
    steps = _job(session, project.id, user.id)
    import_facts(session, steps["validate"].id, _write(tmp_path / "lease.tsv", [_row(steps)]))
    fact = session.scalar(select(RawFact))
    return create_raw_review_task(session, fact.id, actor.id)


def test_claim_is_exclusive_and_can_be_renewed_released(
    app_session_factory, project, users, tmp_path
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        task = _task(session, project, users["operator"], users["admin"], tmp_path)
        claimed = claim_review_task(session, task.id, users["admin"].id, now=now)
        with pytest.raises(LeaseConflict):
            claim_review_task(session, task.id, users["reviewer"].id, now=now)
        renewed = renew_review_task(session, task.id, users["admin"].id,
                                    claimed.lease_version, now=now + timedelta(seconds=1))
        released = release_review_task(session, task.id, users["admin"].id,
                                       renewed.lease_version)
        assert released.reviewer_id is None


def test_expired_lease_is_safely_reclaimed(
    app_session_factory, project, users, tmp_path
) -> None:
    import time
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        task = _task(session, project, users["operator"], users["admin"], tmp_path)
        first = claim_review_task(session, task.id, users["admin"].id, now=now,
                                  lease_for=timedelta(seconds=1))
        first_version = first.lease_version
        # SQLite CURRENT_TIMESTAMP has whole-second precision; cross two ticks.
        time.sleep(2.1)
        second = claim_review_task(session, task.id, users["reviewer"].id,
                                   now=datetime.now(UTC))
        assert second.reviewer_id == users["reviewer"].id
        assert second.lease_version > first_version


def test_database_prevents_review_task_linkage_rewrite(
    app_session_factory, project, foreign_project, users, tmp_path
) -> None:
    with app_session_factory.begin() as session:
        task = _task(session, project, users["operator"], users["admin"], tmp_path)
        task_id = task.id
    with app_session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(ReviewTask).where(ReviewTask.id == task_id)
                            .values(project_id=foreign_project.id))
            session.commit()


@pytest.mark.parametrize("seconds",[0,-1,86_401])
def test_claim_rejects_invalid_lease_duration(app_session_factory,project,users,tmp_path,seconds):
    with app_session_factory.begin() as session:
        task=_task(session,project,users["operator"],users["admin"],tmp_path)
        with pytest.raises(LeaseConflict):claim_review_task(session,task.id,users["admin"].id,lease_for=timedelta(seconds=seconds))


def test_database_rejects_past_claim_and_shortened_active_renewal(app_session_factory,project,users,tmp_path):
    with app_session_factory.begin() as session:
        task=_task(session,project,users["operator"],users["admin"],tmp_path)
        task_id=task.id
    with app_session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(ReviewTask).where(ReviewTask.id==task_id).values(status="claimed",reviewer_id=users["admin"].id,lease_version=1,lease_expires_at=datetime.now(UTC)-timedelta(seconds=1)))
            session.flush()
        session.rollback()
    with app_session_factory.begin() as session:
        task=session.get(ReviewTask,task_id)
        claimed=claim_review_task(session,task.id,users["admin"].id)
        old_expiry=claimed.lease_expires_at
        with pytest.raises(IntegrityError):
            session.execute(update(ReviewTask).where(ReviewTask.id==task.id).values(lease_version=claimed.lease_version+1,lease_expires_at=old_expiry-timedelta(seconds=1)))
            session.flush()
