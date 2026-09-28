from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.database import Base, get_database_url
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


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_database_url().replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
