from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth.models import Role, User
from app.auth.security import decode_access_token
from app.core.database import get_session


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def auth_session() -> Generator[Session, None, None]:
    yield from get_session()


def current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[Session, Depends(auth_session)],
) -> User:
    unauthorized = HTTPException(
        status_code=401,
        detail="invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = decode_access_token(token)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise unauthorized from None

    user = session.scalar(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == user_id)
    )
    if user is None or user.is_disabled:
        raise unauthorized
    return user


def require_permissions(*required: str):
    def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        granted = {
            permission.code
            for role in user.roles
            for permission in role.permissions
        }
        if not set(required).issubset(granted):
            raise HTTPException(status_code=403, detail="permission denied")
        return user

    return dependency
