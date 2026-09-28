"""add fenced worker leases and transactional dispatch outbox

Revision ID: 0005_step_leases
Revises: 0004_jobs
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_step_leases"
down_revision: str | None = "0004_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )


def upgrade() -> None:
    op.add_column("job_steps", sa.Column("lease_token", sa.String(64), nullable=True))
    op.add_column(
        "job_steps",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_steps",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("job_steps", sa.Column("failure_code", sa.String(64)))
    op.add_column("job_steps", sa.Column("failure_summary", sa.String(200)))
    op.add_column("job_steps", sa.Column("result_json", sa.JSON()))
    op.create_check_constraint(
        "ck_job_step_attempt_count",
        "job_steps",
        "attempt_count >= 0 AND attempt_count <= 3",
    )
    op.create_check_constraint(
        "ck_job_step_lease_pair",
        "job_steps",
        "(status = 'running' AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status != 'running' AND lease_token IS NULL "
        "AND lease_expires_at IS NULL)",
    )
    op.create_table(
        "job_dispatch_outbox",
        *_common_columns(),
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("queue", sa.String(16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publish_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_summary", sa.String(200)),
        sa.CheckConstraint("queue IN ('rule', 'llm')", name="ck_job_dispatch_queue"),
        sa.CheckConstraint(
            "dispatch_generation >= 1", name="ck_job_dispatch_generation"
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0", name="ck_job_dispatch_publish_attempts"
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claimed_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_job_dispatch_claim_pair",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["job_steps.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "step_id",
            "dispatch_generation",
            name="uq_job_dispatch_step_generation",
        ),
    )
    op.create_index(
        "ix_job_dispatch_outbox_step_id", "job_dispatch_outbox", ["step_id"]
    )
    op.create_table(
        "job_cleanup_outbox",
        *_common_columns(),
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("work_token", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'completed')", name="ck_job_cleanup_status"
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_job_cleanup_attempt"),
        sa.CheckConstraint("retry_count >= 0", name="ck_job_cleanup_retry_count"),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claimed_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_job_cleanup_claim_pair",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["job_steps.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "step_id",
            "attempt_number",
            "work_token",
            name="uq_job_cleanup_attempt_token",
        ),
    )
    op.create_index(
        "ix_job_cleanup_outbox_step_id", "job_cleanup_outbox", ["step_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_job_cleanup_outbox_step_id", table_name="job_cleanup_outbox")
    op.drop_table("job_cleanup_outbox")
    op.drop_index("ix_job_dispatch_outbox_step_id", table_name="job_dispatch_outbox")
    op.drop_table("job_dispatch_outbox")
    op.drop_constraint("ck_job_step_lease_pair", "job_steps", type_="check")
    op.drop_constraint("ck_job_step_attempt_count", "job_steps", type_="check")
    op.drop_column("job_steps", "result_json")
    op.drop_column("job_steps", "failure_summary")
    op.drop_column("job_steps", "failure_code")
    op.drop_column("job_steps", "attempt_count")
    op.drop_column("job_steps", "lease_expires_at")
    op.drop_column("job_steps", "lease_token")
