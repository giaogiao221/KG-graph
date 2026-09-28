from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DDL,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Document(Base):
    __tablename__ = "documents"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    next_version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    project: Mapped["Project"] = relationship(back_populates="documents")
    created_by: Mapped["User"] = relationship()
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document",
        order_by="DocumentVersion.version_number",
        lazy="selectin",
        passive_deletes=True,
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_version"),
    )

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    uploader_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    is_extractable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    document: Mapped[Document] = relationship(back_populates="versions")
    uploader: Mapped["User"] = relationship()


from app.auth.models import User  # noqa: E402, F401
from app.projects.models import Project  # noqa: E402, F401


@event.listens_for(DocumentVersion, "before_update")
@event.listens_for(DocumentVersion, "before_delete")
def _prevent_document_version_mutation(*_args: object) -> None:
    raise ValueError("document versions are immutable")


event.listen(
    DocumentVersion.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER prevent_document_version_update
        BEFORE UPDATE ON document_versions
        BEGIN
            SELECT RAISE(ABORT, 'document versions are immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    DocumentVersion.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER prevent_document_version_delete
        BEFORE DELETE ON document_versions
        BEGIN
            SELECT RAISE(ABORT, 'document versions are immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
