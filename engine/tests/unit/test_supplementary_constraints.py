from book_engine.ontology.supplementary_constraints import SupplementaryConstraints
from book_engine.llm_v106.pipeline import _candidate_from_row, build_parser


def make_constraints(tmp_path):
    (tmp_path / "policy.json").write_text(
        '{"enabled": true, "property_alias_enabled": true, "relation_normalization_enabled": true, "subject_deny_enabled": true, "subject_deny_action": "manual_review", "subject_lexicon_audit_enabled": true}',
        encoding="utf-8",
    )
    (tmp_path / "property_aliases.tsv").write_text("alias\tcanonical_name\n爆热\t燃烧热、爆热\n", encoding="utf-8")
    (tmp_path / "relation_mapping.tsv").write_text("alias\trelation_type\nmeasuredBy\t方法\n", encoding="utf-8")
    (tmp_path / "subject_denylist.txt").write_text("密度\n", encoding="utf-8")
    (tmp_path / "subject_lexicon.txt").write_text("RDX\n", encoding="utf-8")
    return SupplementaryConstraints.from_paths(
        policy_path=tmp_path / "policy.json",
        property_alias_path=tmp_path / "property_aliases.tsv",
        relation_mapping_path=tmp_path / "relation_mapping.tsv",
        subject_denylist_path=tmp_path / "subject_denylist.txt",
        subject_lexicon_path=tmp_path / "subject_lexicon.txt",
    )


def test_prepares_alias_and_records_lexicon_match(tmp_path):
    constraints = make_constraints(tmp_path)
    row, audits = constraints.prepare_row({"attribute_name": "爆热", "主体名称": "RDX"}, stage="v105")
    assert row["attribute_name"] == "燃烧热、爆热"
    assert {item["action"] for item in audits} == {"property_alias_normalized", "subject_lexicon_match"}


def test_normalizes_only_explicit_relation_alias(tmp_path):
    constraints = make_constraints(tmp_path)
    relation, audits = constraints.normalize_relation_type("measuredBy", fallback="属性", stage="v106")
    assert relation == "方法"
    assert audits[0]["action"] == "relation_normalized"
    untouched, audits = constraints.normalize_relation_type("unmapped_relation", fallback="属性", stage="v106")
    assert untouched == "属性"
    assert audits == []


def test_exact_denylisted_subject_becomes_manual_review_not_deleted(tmp_path):
    constraints = make_constraints(tmp_path)
    result, audits = constraints.subject_decision("密度", stage="v106")
    assert result == "manual_review"
    assert audits[0]["reason"] == "subject_exact_denylist_match"


def test_v106_candidate_uses_supplementary_relation_mapping(tmp_path):
    constraints = make_constraints(tmp_path)
    candidate = _candidate_from_row(
        {"主体名称": "RDX", "predicate_raw": "measuredBy", "尾实体/取值文本": "撞击法"},
        "test",
        constraints=constraints,
    )
    assert candidate["relation_type"] == "方法"


def test_v106_includes_every_book_unless_user_explicitly_excludes_one():
    args = build_parser().parse_args(
        [
            "--books-dir", "books", "--v105-root", "v105", "--output-root", "v106",
            "--property-ontology", "ontology.tsv", "--cache-dir", "cache",
        ]
    )
    assert args.exclude_title == []
