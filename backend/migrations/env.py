import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.db.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base

# Importing app.db.models (above) registers every domain model on Base.metadata
# so autogenerate and target_metadata below see the full Phase 1 schema.
target_metadata = Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Connection string comes from Settings (backend/.env) by default, so there is one
# source of truth for DATABASE_URL in normal (dev/CI) use. ALEMBIC_DATABASE_URL is
# an explicit escape hatch for running migrations against a different database
# programmatically — used by the schema-test suite to migrate TEST_DATABASE_URL
# without ever touching DATABASE_URL.
settings = get_settings()
database_url = os.environ.get("ALEMBIC_DATABASE_URL", settings.database_url)
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
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
