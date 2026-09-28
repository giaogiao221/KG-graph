"""improve built-in Chinese pure-LLM template

Revision ID: 0014_improve_default_prompt
Revises: 0013_prompt_templates
"""

from alembic import op
import sqlalchemy as sa

from app.extraction.native.pure_llm_prompts import TABLE_PROMPT, TEXT_PROMPT


revision = "0014_improve_default_prompt"
down_revision = "0013_prompt_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only the system built-in is adjusted; administrator-created templates
    # remain untouched and profiles still snapshot their selected content.
    op.execute(
        sa.text("""
            UPDATE prompt_templates
            SET text_prompt = :text_prompt,
                table_prompt = :table_prompt,
                description = :description,
                updated_at = CURRENT_TIMESTAMP,
                version = version + 1
            WHERE id = '4d5b4f4c-88dd-4c86-a34b-0aba65429cf1'
              AND name = '默认事实抽取'
        """).bindparams(
            text_prompt=TEXT_PROMPT,
            table_prompt=TABLE_PROMPT,
            description="面向中文资料的纯大模型事实抽取模板，保留原文证据并提高明确事实召回率。",
        )
    )


def downgrade() -> None:
    pass
