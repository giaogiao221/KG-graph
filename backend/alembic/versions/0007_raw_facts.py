"""add immutable project-scoped raw facts

Revision ID: 0007_raw_facts
Revises: 0006_job_artifacts
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_raw_facts"
down_revision: str | None = "0006_job_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("document_job_id", sa.Uuid(), nullable=False),
        sa.Column("import_step_id", sa.Uuid(), nullable=False),
        sa.Column("source_step_id", sa.Uuid(), nullable=False),
        sa.Column("graph_fact_key", sa.String(512)),
        sa.Column("dedup_key", sa.String(66), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("property", sa.String(512), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(512), nullable=False),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(128), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("extraction_source", sa.String(128), nullable=False),
        sa.Column("route", sa.String(128), nullable=False),
        sa.Column("row_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("length(dedup_key) = 66", name="ck_raw_fact_dedup_key"),
        sa.CheckConstraint("version >= 1", name="ck_raw_fact_version"),
        sa.CheckConstraint(
            "graph_fact_key IS NULL OR length(graph_fact_key) > 0",
            name="ck_raw_fact_graph_key",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64", name="ck_raw_fact_evidence_hash"
        ),
        sa.CheckConstraint(
            "length(evidence_text) > 0", name="ck_raw_fact_evidence_text"
        ),
        sa.CheckConstraint(
            "review_status IN ('candidate', 'candidate_review', 'approved', 'rejected')",
            name="ck_raw_fact_review_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_job_id"], ["document_jobs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["import_step_id"], ["job_steps.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_step_id"], ["job_steps.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_job_id", "dedup_key", name="uq_raw_fact_job_dedup"
        ),
    )
    for column in (
        "project_id", "document_id", "document_version_id", "document_job_id",
        "import_step_id", "source_step_id", "subject", "property", "source_type",
        "review_status", "extraction_source",
    ):
        op.create_index(f"ix_raw_facts_{column}", "raw_facts", [column])
    op.create_index("ix_raw_facts_confidence", "raw_facts", ["confidence"])

    op.execute(
        """
        CREATE FUNCTION enforce_raw_fact_linkage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM 1
            FROM document_jobs AS dj
            JOIN extraction_batches AS eb ON eb.id = dj.batch_id
            JOIN document_versions AS dv ON dv.id = dj.document_version_id
            JOIN documents AS d ON d.id = dv.document_id
            JOIN job_steps AS import_step
              ON import_step.id = NEW.import_step_id
             AND import_step.document_job_id = dj.id
             AND import_step.kind IN ('merge', 'validate')
            JOIN job_steps AS source_step
              ON source_step.id = NEW.source_step_id
             AND source_step.document_job_id = dj.id
             AND source_step.kind = 'merge'
            WHERE dj.id = NEW.document_job_id
              AND eb.project_id = NEW.project_id
              AND d.project_id = NEW.project_id
              AND d.id = NEW.document_id
              AND dv.id = NEW.document_version_id
            FOR KEY SHARE OF dj, eb, dv, d, import_step, source_step;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'raw fact linkage mismatch';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_raw_fact_linkage
        BEFORE INSERT ON raw_facts
        FOR EACH ROW EXECUTE FUNCTION enforce_raw_fact_linkage()
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_raw_fact_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'raw facts are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_raw_fact_mutation
        BEFORE UPDATE OR DELETE ON raw_facts
        FOR EACH ROW EXECUTE FUNCTION prevent_raw_fact_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_raw_fact_mutation ON raw_facts")
    op.execute("DROP FUNCTION IF EXISTS prevent_raw_fact_mutation()")
    op.execute("DROP TRIGGER IF EXISTS enforce_raw_fact_linkage ON raw_facts")
    op.execute("DROP FUNCTION IF EXISTS enforce_raw_fact_linkage()")
    op.drop_index("ix_raw_facts_confidence", table_name="raw_facts")
    for column in reversed((
        "project_id", "document_id", "document_version_id", "document_job_id",
        "import_step_id", "source_step_id", "subject", "property", "source_type",
        "review_status", "extraction_source",
    )):
        op.drop_index(f"ix_raw_facts_{column}", table_name="raw_facts")
    op.drop_table("raw_facts")
