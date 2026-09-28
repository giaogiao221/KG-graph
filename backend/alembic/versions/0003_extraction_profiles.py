"""Add project-scoped immutable extraction profile versions.

Revision ID: 0003_extraction_profiles
Revises: 0002_projects_documents
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_extraction_profiles"
down_revision: str | None = "0002_projects_documents"
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
    op.create_table(
        "extraction_profiles",
        *_common_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "next_version_number",
            sa.Integer(),
            server_default=sa.text("2"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "next_version_number >= 2",
            name="ck_extraction_profile_next_version_number",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_extraction_profile_name"),
    )
    op.create_index(
        op.f("ix_extraction_profiles_project_id"),
        "extraction_profiles",
        ["project_id"],
        unique=False,
    )
    op.create_table(
        "profile_versions",
        *_common_columns(),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column(
            "version_number", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["extraction_profiles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("version_number >= 1", name="ck_profile_version_number"),
        sa.UniqueConstraint("profile_id", "version_number", name="uq_profile_version"),
    )
    op.create_index(
        op.f("ix_profile_versions_profile_id"),
        "profile_versions",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_profile_versions_created_by_id"),
        "profile_versions",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_profile_versions_snapshot_sha256"),
        "profile_versions",
        ["snapshot_sha256"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION prevent_profile_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'profile versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_profile_version_mutation
        BEFORE UPDATE OR DELETE ON profile_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_profile_version_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_profile_version_mutation ON profile_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_profile_version_mutation()")
    op.drop_index(
        op.f("ix_profile_versions_snapshot_sha256"), table_name="profile_versions"
    )
    op.drop_index(
        op.f("ix_profile_versions_created_by_id"), table_name="profile_versions"
    )
    op.drop_index(op.f("ix_profile_versions_profile_id"), table_name="profile_versions")
    op.drop_table("profile_versions")
    op.drop_index(
        op.f("ix_extraction_profiles_project_id"), table_name="extraction_profiles"
    )
    op.drop_table("extraction_profiles")
