from pathlib import Path


def test_model_usage_migration_is_chained_and_enforces_history():
    source=(Path(__file__).parents[2]/"alembic/versions/0008_model_usage.py").read_text(encoding="utf-8")
    assert 'revision: str = "0008_model_usage"' in source
    assert 'down_revision: str | None = "0007_raw_facts"' in source
    assert "enforce_model_price_history" in source
    assert "prevent_model_call_mutation" in source
    assert "enforce_model_call_linkage" in source
