"""One-command bootstrap for the initial platform administrator.

Usage:
    python -m app.auth.bootstrap [--username NAME] [--reset-password]

Credentials come from ``--username``, then the
``EXTRACTION_BOOTSTRAP_ADMIN_USERNAME`` / ``EXTRACTION_BOOTSTRAP_ADMIN_PASSWORD``
environment variables, then an interactive prompt. The command is idempotent:
re-running it with an existing username fails cleanly instead of duplicating
or silently overwriting the account; pass ``--reset-password`` to rotate the
password of an existing active administrator intentionally.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.core.models  # noqa: F401  Ensures model metadata is registered.
import app.auth.models  # noqa: F401
import app.documents.models  # noqa: F401
import app.profiles.models  # noqa: F401
import app.projects.models  # noqa: F401
import app.jobs.models  # noqa: F401
import app.facts.models  # noqa: F401
import app.models.models  # noqa: F401
import app.reviews.models  # noqa: F401
import app.exports.models  # noqa: F401
import app.core.audit  # noqa: F401
from app.auth.models import User
from app.auth.security import hash_password
from app.auth.service import seed_roles
from app.core.audit import audit_event
from app.core.database import get_session_factory, session_scope

_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,99}")
_MIN_PASSWORD_LENGTH = 12


class BootstrapError(RuntimeError):
    """Fixed-message failure safe for console output."""


def _validate_username(username: str) -> str:
    value = username.strip()
    if not _USERNAME_PATTERN.fullmatch(value):
        raise BootstrapError("invalid administrator username")
    return value


def _validate_password(password: str) -> str:
    if (
        len(password) < _MIN_PASSWORD_LENGTH
        or password != password.strip()
        or not password.isprintable()
    ):
        raise BootstrapError("administrator password is too weak")
    return password


def bootstrap_admin(
    session: Session,
    *,
    username: str,
    password: str,
    request_id: str,
    reset_password: bool = False,
) -> tuple[User, bool]:
    """Create the initial admin account; returns ``(user, created)``.

    Idempotent by username: an existing account is left untouched unless
    ``reset_password`` is requested, and disabled accounts are never
    re-enabled or rotated implicitly.
    """
    name = _validate_username(username)
    secret = _validate_password(password)
    roles = seed_roles(session)
    existing = session.execute(
        select(User).where(User.username == name)
    ).scalar_one_or_none()
    if existing is not None:
        if not reset_password:
            raise BootstrapError("administrator account already exists")
        if existing.is_disabled:
            raise BootstrapError("administrator account is disabled")
        existing.password_hash = hash_password(secret)
        if roles["admin"] not in existing.roles:
            existing.roles.append(roles["admin"])
        session.flush()
        audit_event(
            session,
            actor_id=existing.id,
            project_id=None,
            action="admin.bootstrap_reset",
            target_type="user",
            target_id=existing.id,
            request_id=request_id,
            outcome="success",
        )
        return existing, False
    user = User(
        username=name,
        password_hash=hash_password(secret),
        roles=[roles["admin"]],
    )
    session.add(user)
    session.flush()
    audit_event(
        session,
        actor_id=user.id,
        project_id=None,
        action="admin.bootstrap",
        target_type="user",
        target_id=user.id,
        request_id=request_id,
        outcome="success",
    )
    return user, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.auth.bootstrap",
        description="Create the initial administrator account (idempotent).",
    )
    parser.add_argument("--username", help="administrator username")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="rotate the password of an existing active administrator",
    )
    args = parser.parse_args(argv)

    username = (args.username or "").strip() or os.getenv(
        "EXTRACTION_BOOTSTRAP_ADMIN_USERNAME", ""
    ).strip()
    password = os.getenv("EXTRACTION_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username:
        username = input("Admin username: ").strip()
    if not password:
        password = getpass.getpass("Admin password: ")
        if password != getpass.getpass("Confirm password: "):
            print("bootstrap failed: passwords do not match", file=sys.stderr)
            return 2

    try:
        with session_scope(get_session_factory()) as session:
            user, created = bootstrap_admin(
                session,
                username=username,
                password=password,
                request_id=f"bootstrap-{uuid4().hex[:24]}",
                reset_password=args.reset_password,
            )
    except BootstrapError as error:
        print(f"bootstrap failed: {error}", file=sys.stderr)
        return 1
    outcome = "created" if created else "password reset"
    print(f"administrator {outcome}: {user.username}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
