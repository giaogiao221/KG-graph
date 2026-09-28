"""add configurable pure-LLM prompt templates

Revision ID: 0013_prompt_templates
Revises: 0012_fix_export_json_trigger
"""

from alembic import op
import sqlalchemy as sa
from uuid import UUID


revision = "0013_prompt_templates"
down_revision = "0012_fix_export_json_trigger"
branch_labels = None
depends_on = None


DEFAULT_ID = "4d5b4f4c-88dd-4c86-a34b-0aba65429cf1"
TEXT_PROMPT = """You extract knowledge-graph facts from the supplied text only. Do not infer, fill gaps, or use external knowledge. Return only a JSON array. Every item must contain exactly subject, property, value, and evidence_text. evidence_text must be a continuous quote from the supplied text that directly supports the fact. Return [] when the evidence is insufficient."""
TABLE_PROMPT = """You extract knowledge-graph facts from the supplied Markdown table only. Preserve the relationship between headers, rows, and columns. Do not infer missing cells or use external knowledge. Return only a JSON array. Every item must contain exactly subject, property, value, and evidence_text. evidence_text must quote the supplied table. Return [] when the table has no verifiable facts."""


def upgrade() -> None:
    common = (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "prompt_templates", *common,
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("text_prompt", sa.Text(), nullable=False),
        sa.Column("table_prompt", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("length(name) > 0", name="ck_prompt_template_name"),
        sa.CheckConstraint("length(text_prompt) > 0", name="ck_prompt_template_text"),
        sa.CheckConstraint("length(table_prompt) > 0", name="ck_prompt_template_table"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("name", name="uq_prompt_template_name"),
    )
    op.create_index("ix_prompt_templates_is_enabled", "prompt_templates", ["is_enabled"])
    op.execute(sa.text("""
        INSERT INTO prompt_templates (id, created_at, updated_at, version, name, description, text_prompt, table_prompt, is_enabled)
        VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, :name, :description, :text_prompt, :table_prompt, true)
        ON CONFLICT (id) DO NOTHING
    """).bindparams(id=UUID(DEFAULT_ID), name="默认事实抽取", description="系统内置的纯大模型事实抽取模板，可复制后再调整。", text_prompt=TEXT_PROMPT, table_prompt=TABLE_PROMPT))


def downgrade() -> None:
    op.drop_index("ix_prompt_templates_is_enabled", table_name="prompt_templates")
    op.drop_table("prompt_templates")
