from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.models import CommonModel


class Base(CommonModel, DeclarativeBase):
    pass


def get_database_url() -> str:
    database_url = os.getenv("EXTRACTION_DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RuntimeError("EXTRACTION_DATABASE_URL is required")
    return database_url


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    try:
        database_url = make_url(get_database_url())
    except ArgumentError:
        raise RuntimeError("EXTRACTION_DATABASE_URL is invalid") from None
    return sessionmaker(
        bind=create_engine(database_url),
        expire_on_commit=False,
    )


def get_session(
    factory: sessionmaker[Session] | None = None,
) -> Generator[Session, None, None]:
    if factory is None:
        factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
