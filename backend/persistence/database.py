"""Async PostgreSQL engine/session management (SQLAlchemy 2.x + asyncpg).

Repositories take an explicit ``AsyncSession``: there is no global or
otherwise hidden mutable session state anywhere in this component.
Callers use short-lived sessions, one transaction per unit of work::

    factory = create_session_factory(
        create_engine(get_database_url())
    )
    async with session_scope(factory) as session:
        await AgentRepository(session).create(...)

``session_scope`` commits on success and rolls back on error, so
multi-step writes never partially commit.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine as _sa_create_async_engine,
)

from .errors import DatabaseConnectionError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5432
DEFAULT_USER = "nexus"
DEFAULT_PASSWORD = "nexus"
DEFAULT_DB = "nexus"


def get_database_url(
    url: str | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> str:
    """Build the async SQLAlchemy URL from env (never hard-code secrets).

    Precedence: explicit ``url`` > ``NEXUS_DATABASE_URL`` > individual
    ``NEXUS_DB_*`` parts > local-development defaults.
    """
    if url is not None:
        return url
    from_env = os.environ.get("NEXUS_DATABASE_URL")
    if from_env:
        return from_env
    host = host or os.environ.get("NEXUS_DB_HOST", DEFAULT_HOST)
    port = port or int(os.environ.get("NEXUS_DB_PORT", str(DEFAULT_PORT)))
    user = user or os.environ.get("NEXUS_DB_USER", DEFAULT_USER)
    password = password or os.environ.get("NEXUS_DB_PASSWORD", DEFAULT_PASSWORD)
    database = database or os.environ.get("NEXUS_DB_NAME", DEFAULT_DB)
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


def create_engine(
    url: str, *, pool_size: int = 5, echo: bool = False
) -> AsyncEngine:
    """Create the async engine (asyncpg driver, health-checked pool)."""
    return _sa_create_async_engine(
        url, pool_size=pool_size, pool_pre_ping=True, echo=echo
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with ``expire_on_commit=False`` (read-after-write)."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_connectivity(engine: AsyncEngine) -> None:
    """Open and close one connection; wrap failures clearly."""
    try:
        async with engine.connect():
            return
    except OperationalError as exc:
        raise DatabaseConnectionError(
            f"cannot reach database: {exc}"
        ) from exc
    except OSError as exc:
        raise DatabaseConnectionError(
            f"cannot reach database: {exc}"
        ) from exc


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Short-lived transactional scope: commit on success, rollback on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def ensure_database_exists(url: str) -> None:
    """Create the named database if missing (provisioning helper).

    Connects to the ``postgres`` maintenance database on the same
    server; safe to call repeatedly.
    """
    import asyncpg

    parsed = urlparse(url)
    target = parsed.path.lstrip("/") or "postgres"
    conn = await asyncpg.connect(
        host=parsed.hostname or DEFAULT_HOST,
        port=parsed.port or DEFAULT_PORT,
        user=parsed.username or DEFAULT_USER,
        password=parsed.password or DEFAULT_PASSWORD,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target
        )
        if not exists:
            await conn.execute(
                f'CREATE DATABASE "{target}"'
            )
    finally:
        await conn.close()


async def dispose_engine(engine: AsyncEngine) -> None:
    """Clean shutdown: close all pooled connections."""
    await engine.dispose()
