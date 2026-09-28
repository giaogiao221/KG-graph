from pathlib import Path
import pytest
from sqlalchemy import select

from app.facts.schema59 import FIELD
from app.reviews.service import (InvalidReview, claim_review_task, create_manual_fact,
    create_review_task, review_fact, version_history)


def test_manual_root_supports_task_review_and_history(app_session_factory,project,users):
    with app_session_factory.begin() as s:
        manual=create_manual_fact(s,project.id,users["admin"].id,{FIELD["subject"]:"手工",FIELD["property"]:"关系",FIELD["value"]:"值",FIELD["unit"]:"",FIELD["condition"]:"",FIELD["evidence_text"]:"证据"},idempotency_key="m-root")
        root_id=manual.root_id
        task=create_review_task(s,root_id,users["admin"].id);task=claim_review_task(s,task.id,users["admin"].id)
        approved=review_fact(s,root_id,"modify_approve",{FIELD["value"]:"新值"},1,users["admin"].id,review_task_id=task.id,expected_lease_version=task.lease_version)
        for expected,action in ((2,"approve"),(3,"publish"),(4,"delete")):
            task=create_review_task(s,root_id,users["admin"].id);task=claim_review_task(s,task.id,users["admin"].id)
            review_fact(s,root_id,action,{},expected,users["admin"].id,review_task_id=task.id,expected_lease_version=task.lease_version)
        history,total=version_history(s,project.id,root_id)
        assert total==5 and approved.row_json[FIELD["value"]]=="新值"
        assert [v.action for v in history]==["manual_create","modify_approve","approve","publish","delete"]


def test_manual_document_version_derives_and_checks_full_chain(app_session_factory,project,foreign_project,users,tmp_path):
    from tests.facts.test_importer import _job
    with app_session_factory.begin() as s:
        steps=_job(s,project.id,users["operator"].id);version=steps["merge"].document_job.document_version
        created=create_manual_fact(s,project.id,users["admin"].id,{FIELD["subject"]:"S",FIELD["property"]:"P",FIELD["value"]:"V",FIELD["evidence_text"]:"E"},document_version_id=version.id,idempotency_key="doc-ok")
        assert created.root.document_id==version.document_id
        with pytest.raises(InvalidReview):
            create_manual_fact(s,foreign_project.id,users["admin"].id,{FIELD["subject"]:"S",FIELD["property"]:"P",FIELD["value"]:"V",FIELD["evidence_text"]:"E"},document_version_id=version.id,idempotency_key="doc-bad")
