"""archive deleted extraction batches

Revision ID: 0011_archive_batches
Revises: 0010_exports_audit
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_archive_batches"
down_revision = "0010_exports_audit"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("extraction_batches", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")))

def downgrade() -> None:
    op.drop_column("extraction_batches", "is_archived")
