"""
Shared fixtures for moderation service tests.
Uses NullPool to avoid asyncpg event-loop binding issues across test functions.
"""
import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.modules.moderation import models as _mod_models  # noqa: F401
from backend.database import Base, get_db

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/tochkamod_test",
)


@pytest.fixture(scope="session", autouse=True)
def create_tables_sync():
    async def _setup():
        engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


@pytest.fixture(autouse=True)
async def override_db():
    from backend.main import app

    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Truncate all tables before each test so tests don't leak data into each other
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE product_moderation, processed_events RESTART IDENTITY CASCADE"))

    async def _get_test_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()
