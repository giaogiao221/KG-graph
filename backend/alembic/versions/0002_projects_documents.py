"""Add project membership and immutable document versions.

Revision ID: 0002_projects_documents
Revises: 0001_core_tables
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_projects_documents"
down_revision: str | None = "0001_core_tables"
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
        "projects",
        *_common_columns(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "project_memberships",
        *_common_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_membership"),
    )
    op.create_index(
        op.f("ix_project_memberships_project_id"),
        "project_memberships",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_memberships_user_id"),
        "project_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "documents",
        *_common_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("next_version_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_project_id"), "documents", ["project_id"], unique=False)
    op.create_index(
        op.f("ix_documents_created_by_id"), "documents", ["created_by_id"], unique=False
    )
    op.create_table(
        "document_versions",
        *_common_columns(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("uploader_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=1024), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("is_extractable", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploader_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_version"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        op.f("ix_document_versions_document_id"),
        "document_versions",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_versions_uploader_id"),
        "document_versions",
        ["uploader_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION prevent_document_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'document versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_document_version_mutation
        BEFORE UPDATE OR DELETE ON document_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_document_version_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_document_version_mutation ON document_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_document_version_mutation()")
    op.drop_index(
        op.f("ix_document_versions_uploader_id"), table_name="document_versions"
    )
    op.drop_index(op.f("ix_document_versions_document_id"), table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index(op.f("ix_documents_created_by_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_project_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_project_memberships_user_id"), table_name="project_memberships")
    op.drop_index(op.f("ix_project_memberships_project_id"), table_name="project_memberships")
    op.drop_table("project_memberships")
    op.drop_table("projects")
