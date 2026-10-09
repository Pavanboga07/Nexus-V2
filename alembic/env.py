"""Alembic environment: async migrations over the model metadata."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Migration URL: explicit override, env, then ini default."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    from_env = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get(
        "NEXUS_DATABASE_URL"
    )
    if from_env:
        return from_env
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Emit SQL without connecting (offline mode)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect with an async engine and migrate."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    engine = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=None
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
