from pathlib import Path
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.facts.importer import import_facts
from app.facts.models import RawFact
from app.facts.schema59 import FIELD
from app.reviews.models import FactVersion
from app.reviews.service import (InvalidReview, VersionConflict, claim_review_task,
    create_review_task, current_fact, ensure_raw_root, publication_facts, release_review_task, review_fact)
from tests.facts.test_importer import _job, _row, _write


def _fact(session,project,user,tmp:Path):
    steps=_job(session,project.id,user.id);import_facts(session,steps["validate"].id,_write(tmp/"v.tsv",[_row(steps)]));return session.scalar(select(RawFact))


def _review(session,fact,actor,action,patch,expected,key=None):
    root=ensure_raw_root(session,fact.id,actor.id);task=create_review_task(session,root.id,actor.id);task=claim_review_task(session,task.id,actor.id)
    return review_fact(session,root.id,action,patch,expected,actor.id,review_task_id=task.id,expected_lease_version=task.lease_version,idempotency_key=key)


def test_modify_preserves_raw_and_stale_or_changed_idempotency_conflicts(app_session_factory,project,users,tmp_path):
    with app_session_factory.begin() as s:
        fact=_fact(s,project,users["operator"],tmp_path);raw=dict(fact.row_json)
        root=ensure_raw_root(s,fact.id,users["admin"].id);task=create_review_task(s,root.id,users["admin"].id);task=claim_review_task(s,task.id,users["admin"].id);lease=task.lease_version
        first=review_fact(s,root.id,"modify_approve",{FIELD["subject"]:"修订"},0,users["admin"].id,review_task_id=task.id,expected_lease_version=lease,idempotency_key="k")
        assert fact.row_json==raw and first.row_json[FIELD["subject"]]=="修订"
        assert review_fact(s,root.id,"modify_approve",{FIELD["subject"]:"修订"},0,users["admin"].id,review_task_id=task.id,expected_lease_version=lease,idempotency_key="k").id==first.id
        with pytest.raises(VersionConflict):review_fact(s,root.id,"reject",{},0,users["admin"].id,review_task_id=task.id,expected_lease_version=lease,idempotency_key="k")


@pytest.mark.parametrize("action",["approve","reject","dispute","delete","publish"])
def test_actions_append_without_deleting_raw(app_session_factory,project,users,tmp_path,action):
    with app_session_factory.begin() as s:
        fact=_fact(s,project,users["operator"],tmp_path);v=_review(s,fact,users["admin"],action,{},0)
        assert v.is_tombstone is (action=="delete") and s.get(RawFact,fact.id) is not None


def test_publish_inherits_modification_delete_hides_current_and_publication_filters(app_session_factory,project,users,tmp_path):
    with app_session_factory.begin() as s:
        fact=_fact(s,project,users["operator"],tmp_path)
        _review(s,fact,users["admin"],"modify_approve",{FIELD["subject"]:"最终"},0)
        published=_review(s,fact,users["admin"],"publish",{},1)
        assert published.row_json[FIELD["subject"]]=="最终" and publication_facts(s,project.id)
        _review(s,fact,users["admin"],"delete",{},2);assert current_fact(s,fact.id) is None


def test_patch_and_history_are_strict(app_session_factory,project,users,tmp_path):
    with app_session_factory.begin() as s:
        fact=_fact(s,project,users["operator"],tmp_path)
        for patch in ({"unknown":"x"},{FIELD["document_id"]:"x"},{FIELD["subject"]:"bad\x00"},{FIELD["evidence_text"]:" "}):
            root=ensure_raw_root(s,fact.id,users["admin"].id);task=create_review_task(s,root.id,users["admin"].id);task=claim_review_task(s,task.id,users["admin"].id)
            with pytest.raises(InvalidReview):review_fact(s,root.id,"modify_approve",patch,0,users["admin"].id,review_task_id=task.id,expected_lease_version=task.lease_version)
            release_review_task(s,task.id,users["admin"].id,task.lease_version)
        v=_review(s,fact,users["admin"],"approve",{},0);vid=v.id
    with app_session_factory() as s:
        with pytest.raises(IntegrityError):s.execute(update(FactVersion).where(FactVersion.id==vid).values(action="reject"));s.commit()
