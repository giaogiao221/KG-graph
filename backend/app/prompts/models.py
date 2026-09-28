from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("name", name="uq_prompt_template_name"),
        CheckConstraint("length(name) > 0", name="ck_prompt_template_name"),
        CheckConstraint("length(text_prompt) > 0", name="ck_prompt_template_text"),
        CheckConstraint("length(table_prompt) > 0", name="ck_prompt_template_table"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    text_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    table_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
