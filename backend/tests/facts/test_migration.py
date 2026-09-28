from pathlib import Path


def test_raw_fact_migration_is_chained_and_installs_postgres_immutability() -> None:
    path = Path(__file__).parents[2] / "alembic/versions/0007_raw_facts.py"
    migration = path.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0006_job_artifacts"' in migration
    assert "CREATE TABLE raw_facts" not in migration  # use Alembic operations
    assert "CREATE FUNCTION prevent_raw_fact_mutation" in migration
    assert "BEFORE UPDATE OR DELETE ON raw_facts" in migration
    assert "DROP FUNCTION IF EXISTS prevent_raw_fact_mutation()" in migration
    assert "uq_raw_fact_job_dedup" in migration
    assert "ck_raw_fact_evidence_hash" in migration
    assert '"ix_raw_facts_confidence"' in migration


def test_alembic_environment_registers_fact_models() -> None:
    environment = (Path(__file__).parents[2] / "alembic/env.py").read_text(
        encoding="utf-8"
    )
    assert "import app.facts.models" in environment
