"""Database infrastructure tests: connectivity, sessions, transactions."""

import asyncio
import os
import pathlib
import types

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.persistence.database import (
    check_connectivity,
    create_engine,
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    get_database_url,
    session_scope,
)
from backend.persistence.errors import DatabaseConnectionError
from backend.persistence.models import Base

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test",
)


def _alembic_config(url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _migrate(url, revision="head"):
    asyncio.run(ensure_database_exists(url))
    command.upgrade(_alembic_config(url), revision)


@pytest.fixture(scope="module")
def db():
    _migrate(TEST_URL)
    # NullPool: pytest-asyncio runs each test on a fresh event loop, so
    # pooled connections bound to a previous loop would break. NullPool
    # opens and closes per checkout — correct for tests, irrelevant rate.
    engine = _sa_create_engine(
        TEST_URL, poolclass=NullPool, pool_pre_ping=True
    )
    try:
        yield types.SimpleNamespace(
            engine=engine, sessions=create_session_factory(engine)
        )
    finally:
        asyncio.run(dispose_engine(engine))


@pytest.mark.asyncio
async def test_connection_works(db):  # 1
    await check_connectivity(db.engine)


@pytest.mark.asyncio
async def test_session_factory_works(db):  # 2
    async with db.sessions() as session:
        result = await session.execute(__import__("sqlalchemy").text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_transaction_commits(db):  # 3
    from backend.persistence.repositories.users import UserRepository

    async with session_scope(db.sessions) as session:
        user = await UserRepository(session).create()
        user_id = user.id
    async with db.sessions() as session:
        assert await UserRepository(session).get(user_id) is not None


@pytest.mark.asyncio
async def test_transaction_rolls_back(db):  # 4
    from backend.persistence.repositories.users import UserRepository

    async with db.sessions() as session:
        user = await UserRepository(session).create()
        user_id = user.id
        await session.rollback()
    async with db.sessions() as session:
        assert await UserRepository(session).get(user_id) is None


@pytest.mark.asyncio
async def test_connection_failure_handled():  # 5
    engine = create_engine(
        "postgresql+asyncpg://nexus:nexus@127.0.0.1:59999/nexus_test"
    )
    try:
        with pytest.raises(DatabaseConnectionError):
            await check_connectivity(engine)
    finally:
        await dispose_engine(engine)


def test_clean_upgrade_works(db):  # 46
    command.upgrade(_alembic_config(TEST_URL), "head")


def test_downgrade_works(db):  # 47
    command.downgrade(_alembic_config(TEST_URL), "-1")
    command.upgrade(_alembic_config(TEST_URL), "head")


def test_upgrade_after_downgrade_works(db):  # 48
    command.downgrade(_alembic_config(TEST_URL), "base")
    command.upgrade(_alembic_config(TEST_URL), "head")


@pytest.mark.asyncio
async def test_schema_matches_declared_models(db):  # 49
    async with db.engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_conn: inspect(sync_conn).get_table_names()
        )
        assert set(tables) - {"alembic_version"} == set(
            Base.metadata.tables
        )
        for name, table in Base.metadata.tables.items():
            columns = await connection.run_sync(
                lambda sync_conn, n=name: [
                    column["name"]
                    for column in inspect(sync_conn).get_columns(n)
                ]
            )
            assert sorted(columns) == sorted(table.columns.keys()), name


def test_database_url_defaults():
    url = get_database_url(
        host="h", port=1, user="u", password="p", database="d"
    )
    assert url == "postgresql+asyncpg://u:p@h:1/d"
