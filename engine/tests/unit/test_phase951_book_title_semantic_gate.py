from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from check_semantic_precision_schema59_v2_phase95 import _book_title_subject_policy  # noqa: E402


def _row(subject: str, subject_type: str = "材料") -> dict[str, str]:
    return {
        "主体名称": subject,
        "主体类型": subject_type,
        "来源类型": "table_conditional_record",
    }


def test_material_entity_book_title_is_a_legal_root_anchor():
    result = _book_title_subject_policy("复合火药", [_row("复合火药")])
    assert result["action"] == "allow_entity_anchor"
    assert result["heading_kind"] == "pure_entity"


def test_topic_only_book_title_remains_illegal_as_subject():
    result = _book_title_subject_policy("炸药理论基础", [_row("炸药理论基础")])
    assert result["action"] == "reject_non_entity_title"
    assert result["heading_kind"] == "topic_only"


def test_descriptive_book_title_must_not_be_used_unparsed():
    result = _book_title_subject_policy("复合火药的性能", [_row("复合火药的性能")])
    assert result["action"] == "reject_unparsed_descriptive_title"
    assert result["parsed_entity"] == "复合火药"


def test_unknown_proper_name_book_title_is_visible_but_not_hard_rejected():
    result = _book_title_subject_policy("黑索今", [_row("黑索今")])
    assert result["action"] == "allow_ambiguous_root_anchor_with_warning"
