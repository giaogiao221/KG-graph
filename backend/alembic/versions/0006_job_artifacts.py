"""add durable job artifact state machine

Revision ID: 0006_job_artifacts
Revises: 0005_step_leases
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_job_artifacts"
down_revision: str | None = "0005_step_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_kind", sa.String(32), nullable=False),
        sa.Column("owner_token", sa.String(32), nullable=False),
        sa.Column("storage_key", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("status", sa.String(32), server_default="reserved", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("artifact_kind IN ('manifest', 'candidate_tsv')", name="ck_job_artifact_kind"),
        sa.CheckConstraint("status IN ('reserved','writing','ready','referenced','expiring','expired','importing','import_cleanup','imported_cleanup','cleanup_failed','ownership_failed')", name="ck_job_artifact_status"),
        sa.CheckConstraint("(status IN ('reserved', 'writing') AND size_bytes IS NULL AND sha256 IS NULL) OR (status IN ('ready', 'referenced', 'importing', 'import_cleanup') AND size_bytes IS NOT NULL AND size_bytes >= 0 AND sha256 IS NOT NULL AND length(sha256) = 64) OR (status IN ('expiring', 'cleanup_failed', 'expired', 'imported_cleanup', 'ownership_failed') AND ((size_bytes IS NULL AND sha256 IS NULL) OR (size_bytes IS NOT NULL AND size_bytes >= 0 AND sha256 IS NOT NULL AND length(sha256) = 64)))", name="ck_job_artifact_metadata"),
        sa.CheckConstraint("(status IN ('expiring', 'importing', 'import_cleanup', 'cleanup_failed') AND claim_token IS NOT NULL AND claimed_at IS NOT NULL) OR (status NOT IN ('expiring', 'importing', 'import_cleanup', 'cleanup_failed') AND claim_token IS NULL AND claimed_at IS NULL)", name="ck_job_artifact_claim"),
        sa.CheckConstraint("length(owner_token) = 32", name="ck_job_artifact_owner_token"),
        sa.CheckConstraint("storage_key LIKE 'artifacts/' || owner_token || '/payload.%'", name="ck_job_artifact_storage_key"),
        sa.CheckConstraint("version >= 1", name="ck_job_artifact_version"),
        sa.ForeignKeyConstraint(["step_id"], ["job_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_job_artifact_storage_key"),
        sa.UniqueConstraint("owner_token", name="uq_job_artifact_owner_token"),
    )
    op.create_index("ix_job_artifacts_step_id", "job_artifacts", ["step_id"])
    op.create_index("ix_job_artifacts_status", "job_artifacts", ["status"])
    op.create_index("ix_job_artifacts_expires_at", "job_artifacts", ["expires_at"])
    op.execute(
        """
        CREATE FUNCTION prevent_job_artifact_owner_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.owner_token IS DISTINCT FROM OLD.owner_token THEN
                RAISE EXCEPTION 'job artifact owner is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_job_artifact_owner_change
        BEFORE UPDATE OF owner_token ON job_artifacts
        FOR EACH ROW EXECUTE FUNCTION prevent_job_artifact_owner_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_job_artifact_owner_change ON job_artifacts")
    op.execute("DROP FUNCTION IF EXISTS prevent_job_artifact_owner_change()")
    op.drop_index("ix_job_artifacts_expires_at", table_name="job_artifacts")
    op.drop_index("ix_job_artifacts_status", table_name="job_artifacts")
    op.drop_index("ix_job_artifacts_step_id", table_name="job_artifacts")
    op.drop_table("job_artifacts")
