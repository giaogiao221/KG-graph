"""Add extraction batch, document job, and step state machines.

Revision ID: 0004_jobs
Revises: 0003_extraction_profiles
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_jobs"
down_revision: str | None = "0003_extraction_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'queued', 'running', 'partial_success', 'completed', 'retryable_failed', 'permanent_failed', 'cancelled'"
KINDS = "'rule_text', 'llm_text', 'rule_table', 'llm_table', 'merge', 'validate'"


def _common_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "extraction_batches",
        *_common_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_extraction_batch_status"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["profile_version_id"], ["profile_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key"),
    )
    op.create_index(op.f("ix_extraction_batches_project_id"), "extraction_batches", ["project_id"])
    op.create_index(op.f("ix_extraction_batches_profile_version_id"), "extraction_batches", ["profile_version_id"])
    op.create_table(
        "document_jobs",
        *_common_columns(),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_document_job_status"),
        sa.ForeignKeyConstraint(["batch_id"], ["extraction_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "document_version_id", name="uq_document_job_batch_document"),
    )
    op.create_index(op.f("ix_document_jobs_batch_id"), "document_jobs", ["batch_id"])
    op.create_index(op.f("ix_document_jobs_document_version_id"), "document_jobs", ["document_version_id"])
    op.create_table(
        "job_steps",
        *_common_columns(),
        sa.Column("document_job_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="ck_job_step_kind"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_job_step_status"),
        sa.CheckConstraint("version >= 1", name="ck_job_step_version"),
        sa.CheckConstraint("position >= 0", name="ck_job_step_position"),
        sa.CheckConstraint("stage >= 0", name="ck_job_step_stage"),
        sa.CheckConstraint(
            "(kind IN ('rule_text', 'llm_text', 'rule_table', 'llm_table') AND stage = 0) "
            "OR (kind = 'merge' AND stage = 1) "
            "OR (kind = 'validate' AND stage = 2)",
            name="ck_job_step_kind_stage",
        ),
        sa.ForeignKeyConstraint(["document_job_id"], ["document_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_job_id", "kind", name="uq_job_step_kind"),
        sa.UniqueConstraint("document_job_id", "position", name="uq_job_step_position"),
        sa.UniqueConstraint(
            "document_job_id",
            "idempotency_key",
            name="uq_job_step_execution_fingerprint",
        ),
    )
    op.create_index(op.f("ix_job_steps_document_job_id"), "job_steps", ["document_job_id"])
    op.execute(
        """
        CREATE FUNCTION enforce_batch_profile_project()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- Lock order: profile/document -> batch -> job.
            -- FOR SHARE conflicts with project_id NO KEY UPDATE.
            -- lock referenced profile
            PERFORM 1
            FROM extraction_profiles ep
            JOIN profile_versions pv ON pv.profile_id = ep.id
            WHERE pv.id = NEW.profile_version_id
            FOR SHARE OF ep;

            -- lock profile version
            PERFORM 1
            FROM profile_versions pv
            WHERE pv.id = NEW.profile_version_id
            FOR SHARE OF pv;
            IF NOT EXISTS (
                SELECT 1 FROM profile_versions pv
                JOIN extraction_profiles ep ON ep.id = pv.profile_id
                WHERE pv.id = NEW.profile_version_id
                  AND ep.project_id = NEW.project_id
            ) THEN
                RAISE EXCEPTION 'batch profile project mismatch';
            END IF;
            IF TG_OP = 'UPDATE' AND EXISTS (
                SELECT 1 FROM document_jobs dj
                JOIN document_versions dv ON dv.id = dj.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE dj.batch_id = OLD.id
                  AND d.project_id != NEW.project_id
            ) THEN
                RAISE EXCEPTION 'batch project change breaks jobs';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_batch_profile_project
        BEFORE INSERT OR UPDATE ON extraction_batches
        FOR EACH ROW EXECUTE FUNCTION enforce_batch_profile_project()
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_document_project_change_breaking_jobs()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- The document row is already update-locked; lock related batches next.
            PERFORM 1
            FROM document_versions dv
            JOIN document_jobs dj ON dj.document_version_id = dv.id
            JOIN extraction_batches eb ON eb.id = dj.batch_id
            WHERE dv.document_id = OLD.id
            ORDER BY eb.id
            FOR SHARE OF eb;
            IF EXISTS (
                SELECT 1 FROM document_versions dv
                JOIN document_jobs dj ON dj.document_version_id = dv.id
                JOIN extraction_batches eb ON eb.id = dj.batch_id
                WHERE dv.document_id = OLD.id
                  AND eb.project_id != NEW.project_id
            ) THEN
                RAISE EXCEPTION 'document project change breaks jobs';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_document_project_change_breaking_jobs
        BEFORE UPDATE OF project_id ON documents
        FOR EACH ROW EXECUTE FUNCTION prevent_document_project_change_breaking_jobs()
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_profile_project_change_breaking_batches()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- The profile row is already update-locked; stabilize its versions.
            PERFORM 1
            FROM profile_versions pv
            WHERE pv.profile_id = OLD.id
            ORDER BY pv.id
            FOR SHARE OF pv;
            IF EXISTS (
                SELECT 1 FROM profile_versions pv
                JOIN extraction_batches eb ON eb.profile_version_id = pv.id
                WHERE pv.profile_id = OLD.id
                  AND eb.project_id != NEW.project_id
            ) THEN
                RAISE EXCEPTION 'profile project change breaks batches';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_profile_project_change_breaking_batches
        BEFORE UPDATE OF project_id ON extraction_profiles
        FOR EACH ROW EXECUTE FUNCTION prevent_profile_project_change_breaking_batches()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_job_document_project()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- Lock order: profile/document -> batch -> job.
            -- FOR SHARE conflicts with project_id NO KEY UPDATE.
            -- lock referenced document
            PERFORM 1
            FROM documents d
            JOIN document_versions dv ON dv.document_id = d.id
            WHERE dv.id = NEW.document_version_id
            FOR SHARE OF d;

            -- lock document version
            PERFORM 1
            FROM document_versions dv
            WHERE dv.id = NEW.document_version_id
            FOR SHARE OF dv;
            -- lock referenced batch
            PERFORM 1
            FROM extraction_batches eb
            WHERE eb.id = NEW.batch_id
            FOR SHARE OF eb;
            -- Revalidate only after both referenced entities are stable.
            IF NOT EXISTS (
                SELECT 1 FROM extraction_batches eb
                JOIN document_versions dv ON dv.id = NEW.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE eb.id = NEW.batch_id
                  AND eb.project_id = d.project_id
            ) THEN
                RAISE EXCEPTION 'job document project mismatch';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_job_document_project
        BEFORE INSERT OR UPDATE ON document_jobs
        FOR EACH ROW EXECUTE FUNCTION enforce_job_document_project()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_profile_project_change_breaking_batches "
        "ON extraction_profiles"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_profile_project_change_breaking_batches()")
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_document_project_change_breaking_jobs "
        "ON documents"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_document_project_change_breaking_jobs()")
    op.execute("DROP TRIGGER IF EXISTS enforce_job_document_project ON document_jobs")
    op.execute("DROP FUNCTION IF EXISTS enforce_job_document_project()")
    op.execute("DROP TRIGGER IF EXISTS enforce_batch_profile_project ON extraction_batches")
    op.execute("DROP FUNCTION IF EXISTS enforce_batch_profile_project()")
    op.drop_index(op.f("ix_job_steps_document_job_id"), table_name="job_steps")
    op.drop_table("job_steps")
    op.drop_index(op.f("ix_document_jobs_document_version_id"), table_name="document_jobs")
    op.drop_index(op.f("ix_document_jobs_batch_id"), table_name="document_jobs")
    op.drop_table("document_jobs")
    op.drop_index(op.f("ix_extraction_batches_profile_version_id"), table_name="extraction_batches")
    op.drop_index(op.f("ix_extraction_batches_project_id"), table_name="extraction_batches")
    op.drop_table("extraction_batches")
