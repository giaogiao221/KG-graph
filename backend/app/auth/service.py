from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth.models import Permission, Role, User
from app.auth.security import verify_password


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"records:view", "exports:create"},
    "reviewer": {"records:view", "records:review", "exports:create"},
    "operator": {"records:view", "records:create", "records:edit", "exports:create"},
    "admin": {
        "records:view",
        "records:create",
        "records:edit",
        "records:review",
        "users:create",
        "projects:members",
        "exports:create",
        "exports:unreviewed",
    },
}


def seed_roles(session: Session) -> dict[str, Role]:
    permission_codes = set().union(*ROLE_PERMISSIONS.values())
    permissions = {
        permission.code: permission
        for permission in session.scalars(
            select(Permission).where(Permission.code.in_(permission_codes))
        )
    }
    for code in permission_codes - permissions.keys():
        permission = Permission(code=code)
        session.add(permission)
        permissions[code] = permission

    roles = {
        role.name: role
        for role in session.scalars(
            select(Role)
            .options(selectinload(Role.permissions))
            .where(Role.name.in_(ROLE_PERMISSIONS))
        )
    }
    for name, codes in ROLE_PERMISSIONS.items():
        role = roles.get(name)
        if role is None:
            role = Role(name=name)
            session.add(role)
            roles[name] = role
        granted = {permission.code for permission in role.permissions}
        role.permissions.extend(permissions[code] for code in codes - granted)

    session.flush()
    return roles


def authenticate_user(
    session: Session, username: str, password: str
) -> User | None:
    user = session.scalar(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.username == username)
    )
    if user is None or user.is_disabled:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
