from sqlalchemy import func, select

from app.facts.schema59 import FIELD
from app.reviews.models import FactVersion, ReviewFactRoot
from app.reviews.service import create_manual_fact


def test_manual_create_adds_independent_root_and_first_version(
    app_session_factory, project, users
) -> None:
    with app_session_factory.begin() as session:
        version = create_manual_fact(session, project.id, users["admin"].id, {
            FIELD["subject"]: "手工主体", FIELD["property"]: "关系",
            FIELD["value"]: "值", FIELD["unit"]: "",
            FIELD["condition"]: "", FIELD["evidence_text"]: "人工录入证据",
        }, idempotency_key="manual-1")
        assert version.action == "manual_create"
        assert version.root.raw_fact_id is None
        assert version.root.source == "manual"
        assert session.scalar(select(func.count()).select_from(ReviewFactRoot)) == 1
        assert session.scalar(select(func.count()).select_from(FactVersion)) == 1


def test_manual_create_accepts_safe_semantic_field_names(
    app_session_factory, project, users
) -> None:
    with app_session_factory.begin() as session:
        version = create_manual_fact(
            session, project.id, users["admin"].id,
            {"subject": "主体", "property": "关系", "value": "值",
             "unit": "", "condition": "", "evidence": "人工证据"},
            idempotency_key="manual-semantic-fields",
        )

        assert version.row_json[FIELD["subject"]] == "主体"
        assert version.row_json[FIELD["evidence_text"]] == "人工证据"
