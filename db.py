"""Async SQLAlchemy setup for the hosted product's relational data: users,
sessions, subscription state. Deliberately separate from the AH data layer
(parquet + DuckDB, see analyze.py/snipe_check.py) -- the auction data isn't
per-user and doesn't belong in this database; only genuinely relational,
per-user concerns live here.

DATABASE_URL drives everything (e.g. postgresql+asyncpg://... in production,
sqlite+aiosqlite:///... in tests) -- tests override it via dependency
injection rather than env vars, so the test suite never needs a real
Postgres.
"""
import os
from datetime import datetime

from fastapi import Depends
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from sqlalchemy import DateTime, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import blizz  # noqa: F401  -- triggers .env loading as a side effect, same as every other module here


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """FastAPI-Users' base table (id/email/hashed_password/is_active/
    is_verified) plus subscription state, updated only by billing.py's
    Stripe webhook handler -- never trust client input for these fields."""
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subscription_current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("Set DATABASE_URL in .env (see README) -- e.g. "
                         "postgresql+asyncpg://user:pass@host/db")
    return url


_engine = None
_sessionmaker = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(_database_url())
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine(), expire_on_commit=False)
    return _sessionmaker


async def get_async_session():
    async with sessionmaker()() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)
