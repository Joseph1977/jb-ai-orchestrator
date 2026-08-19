# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine import make_url

from app.config import Config
from app.db.base import Base
from app.utils.logger import logger

_engine: AsyncEngine | None = None
async_session: async_sessionmaker[AsyncSession] | None = None


async def ensure_database_exists(database_url: str) -> None:
    url = make_url(database_url)
    database = url.database
    if not database:
        return

    admin_url = url.set(database="postgres")

    conn: Optional[asyncpg.Connection] = None
    try:
        conn = await asyncpg.connect(
            user=admin_url.username,
            password=admin_url.password,
            host=admin_url.host,
            port=admin_url.port or 5432,
            database=admin_url.database,
        )
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            database,
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{database}"')
            logger.info("Created database %s", database)
    except asyncpg.InvalidCatalogNameError:
        # If postgres database doesn't exist, fall back silently
        logger.warning(
            "Unable to connect to postgres database to ensure %s exists",
            database,
        )
    finally:
        if conn:
            await conn.close()


def _build_engine(database_url: str) -> AsyncEngine:
    if not Config.DATABASE_URL:
        raise ValueError("DATABASE_URL configuration is required")
    return create_async_engine(
        database_url,
        future=True,
        echo=False,
        pool_pre_ping=True,
    )


async def init_db() -> None:
    """Initialize the database engine and ensure the schema exists."""
    global _engine, async_session
    if _engine is None:
        database_url = Config.DATABASE_URL
        if not database_url:
            raise ValueError("DATABASE_URL configuration is required")

        await ensure_database_exists(database_url)

        _engine = _build_engine(database_url)
        async_session = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            autoflush=False,
        )

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialized successfully")


@asynccontextmanager
async def get_session() -> AsyncSession:
    """Provide an async SQLAlchemy session."""
    if async_session is None:
        raise RuntimeError("Database session maker not initialized. Call init_db() first.")

    session = async_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
