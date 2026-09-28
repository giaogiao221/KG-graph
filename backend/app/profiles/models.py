from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DDL,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ExtractionProfile(Base):
    __tablename__ = "extraction_profiles"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_extraction_profile_name"),
        CheckConstraint(
            "next_version_number >= 2",
            name="ck_extraction_profile_next_version_number",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    next_version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    project: Mapped["Project"] = relationship()
    versions: Mapped[list["ProfileVersion"]] = relationship(
        back_populates="profile",
        order_by="ProfileVersion.version_number",
        lazy="selectin",
        passive_deletes=True,
    )


class ProfileVersion(Base):
    __tablename__ = "profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version_number", name="uq_profile_version"),
        CheckConstraint("version_number >= 1", name="ck_profile_version_number"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile: Mapped[ExtractionProfile] = relationship(back_populates="versions")
    created_by: Mapped["User"] = relationship()


from app.auth.models import User  # noqa: E402, F401
from app.projects.models import Project  # noqa: E402, F401


@event.listens_for(ProfileVersion, "before_update")
@event.listens_for(ProfileVersion, "before_delete")
def _prevent_profile_version_mutation(*_args: object) -> None:
    raise ValueError("profile versions are immutable")


for operation in ("UPDATE", "DELETE"):
    event.listen(
        ProfileVersion.__table__,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER prevent_profile_version_{operation.lower()}
            BEFORE {operation} ON profile_versions
            BEGIN
                SELECT RAISE(ABORT, 'profile versions are immutable');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
