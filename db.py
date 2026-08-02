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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
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
    # Free tier (2026-07-25): a non-subscribed account is locked to the
    # first sell realm it ever queries via /api/snipes, to bound the number
    # of distinct expensive DuckDB queries a free account can generate --
    # written once by dashboard.py's api_snipes() on first use, never by
    # client input directly. NULL until a free-tier account's first query;
    # never enforced for an active subscription or superuser.
    locked_sell_realm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Public display name for forum.py's Snipe Board (added 2026-07-29) --
    # posts used to show the account's real email publicly; this replaces
    # that. NULL until the user sets one (forum.create_post() requires it be
    # set before a first post, see that module -- not enforced at
    # registration, so this stays nullable for every account that never
    # posts). No uniqueness constraint -- a duplicate nickname is a cosmetic
    # nuisance, not a correctness problem, not worth the extra validation.
    nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Watchlist notification delivery target (added 2026-08-02, human
    # decision -- see watchlist.py). A plain Discord webhook URL the user
    # pastes in themselves (no OAuth) -- NULL means Watchlist triggers are
    # tracked but never delivered anywhere for this account.
    discord_webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ForumPost(Base):
    """A user-submitted "I found this snipe" post -- image required, title
    optional (forum.py's product decision). author_email/author_nickname are
    captured at creation time rather than joined from `user` on read, since a
    post should keep showing who found it even if that account is later
    deleted (or later changes its nickname); author_id is kept alongside for
    a real relational link (e.g. a future "my posts" view) but nothing
    currently reads it back. author_email is no longer exposed by the API
    (see forum._post_to_json) -- kept only for internal reference, real
    display uses author_nickname (added 2026-07-29, nullable only because
    posts made before nicknames existed have none; forum.create_post()
    requires every new post to carry one)."""
    __tablename__ = "forum_post"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("user.id"), nullable=False)
    author_email: Mapped[str] = mapped_column(String(320), nullable=False)
    author_nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    image_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Set in Python (not a DB server_default) so the value is tz-aware and
    # identical across the Postgres-in-production / SQLite-in-tests split
    # this project already relies on (see this module's docstring).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))


class WowAccount(Base):
    """A user's self-declared WoW account label (e.g. "Main", "Alt account")
    -- no Blizzard OAuth/credentials, purely metadata typed in on
    profile.html. wow_accounts.py enforces the 10-per-user cap at write
    time via an atomic INSERT...SELECT...WHERE, not a Python-side
    count-then-insert (see that module for why)."""
    __tablename__ = "wow_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("user.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))


class WowAccountRealm(Base):
    """One EU connected-realm id (Blizzard's cr_id) a WowAccount has a
    character on -- stores the raw int id, same convention every realm
    reference in this app already uses (snipe_check/dashboard never store a
    realm name for matching, only the id -- see dashboard._row_to_json's
    buy_realm field). Never validated against blizz.list_connected_realms()
    at write time (that's a live Blizzard call); a bogus id is harmless --
    it simply never matches any dashboard row's buy_realm.

    realm_name (added 2026-08-02, human request -- Watchlist's Discord
    alert should say which specific member name of a connected realm an
    account is on, e.g. "Zul'jin" rather than "Zul'jin / Sanguino
    (connected realm)"): the specific name the user searched/picked on
    profile.html at registration time, purely descriptive -- matching is
    still by connected_realm_id only, same as everywhere else in this app
    (picking any one member name still registers the whole connected
    realm). Nullable because rows created before this column existed have
    none; those fall back to the generic "list every member name" display
    wherever this is read."""
    __tablename__ = "wow_account_realm"
    __table_args__ = (
        UniqueConstraint("wow_account_id", "connected_realm_id", name="uq_wow_account_realm_account_realm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wow_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("wow_account.id"), nullable=False)
    connected_realm_id: Mapped[int] = mapped_column(Integer, nullable=False)
    realm_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))


class WatchlistItem(Base):
    """A single item a subscribed user wants notified about, independent of
    any sell realm (see FEATURE_WATCHLIST.md -- Watchlist has no sell realm
    at all, unlike the main snipe flow). Matching is item_id-only (2026-08-02
    human decision, matching the rest of the product's 2026-07-26 call to
    drop bonus/ilvl-aware matching rather than reopen it here) --
    pet_species_id is kept alongside since every caged pet shares one
    item_id (82800), same reason snipe_check.py/dashboard.py both carry it;
    there is no pet_quality_id column here, deliberately -- a per-quality
    trigger wasn't asked for and would add matching granularity the rest of
    this model doesn't have.

    trigger_price_copper is a plain user-chosen absolute price, not a
    discount vs region_median_cheapest (human explicitly rejected an
    "auto-price" discount-threshold design during a 2026-08-02 conversation
    -- "we only want to trigger for whatever price the user wants"). Nullable
    (not required at creation) specifically because a TSM group import can
    add dozens/hundreds of items at once with wildly different values --
    forcing one shared trigger price on a whole imported group would be
    meaningless; watchlist.py's checker simply skips a row with no trigger
    set yet, and the item sits watched-but-silent until the user fills one
    in per-row. label is optional, populated from a TSM group's path when
    added via tsm_import.py, NULL for a plain add-by-item-id.

    last_notified_at backs watchlist.py's notification cooldown -- a listing
    that stays under the trigger for hours shouldn't re-fire a Discord
    message every ~10-minute collect_all.py cycle."""
    __tablename__ = "watchlist_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("user.id"), nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pet_species_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_price_copper: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("Set DATABASE_URL in .env (see README) -- e.g. "
                         "postgresql+asyncpg://user:pass@host/db")
    return url


_engine = None
_sessionmaker = None


def engine():
    """Pool sizing tuned 2026-08-01 (real incident: QueuePool exhausted,
    login failing, with a single account doing light browsing -- see
    HISTORY.md). Root cause: every route gated by auth.current_active_user
    checks out a connection for the *entire* request via FastAPI's
    Depends()-generator lifetime, including dashboard.py's api_snipes()
    route, whose actual work (a DuckDB query, documented elsewhere as
    30-175s cold, plus NameCache resolution) has nothing to do with
    Postgres at all -- the connection just sits checked out, unused, for
    the whole duration. The bare SQLAlchemy defaults this used to run on
    (pool_size=5, max_overflow=10 -- 15 total, never explicitly set) meant
    a handful of overlapping slow requests from even one browser tab (an
    auto-refresh firing while a previous slow query is still in flight, a
    realm switch) could exhaust it. Raised to a much larger ceiling
    (pool_size=20, max_overflow=40 -- 60 total) for headroom Postgres
    itself can comfortably serve; this doesn't fix the underlying "why is a
    DB connection held for 30-175s of unrelated work" design issue (a
    separate, real follow-up -- restructuring that safely requires
    reworking how the route resolves its user, since FastAPI-Users'
    current_active_user ties a connection to the whole request by design,
    not something to rush alongside pool tuning), but makes the pool large
    enough that realistic concurrent slow-query load doesn't hit the wall.
    pool_pre_ping=True discards a connection that's gone stale (e.g. an
    idle timeout on the Postgres side) and transparently reconnects instead
    of surfacing a broken-connection error to a request. pool_timeout=30
    (SQLAlchemy's own default, set explicitly here for clarity) is how long
    a request waits for a free connection before raising -- unchanged, but
    with 4x the pool capacity a real wait is now far less likely to begin
    with. Only applies to the real Postgres engine -- tests never call this
    function at all (dashboard.app.dependency_overrides swaps
    get_async_session for a throwaway per-test SQLite engine/session
    entirely, see test_dashboard.py's `client` fixture), so these
    Postgres-specific pool kwargs never reach a SQLite connection (which
    uses NullPool by default and would reject pool_size/max_overflow)."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            _database_url(),
            pool_size=20,
            max_overflow=40,
            pool_timeout=30,
            pool_pre_ping=True,
        )
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def isolated_session():
    """An AsyncSession backed by a brand-new engine, not engine()'s shared
    process-wide singleton -- for a caller running inside its own
    asyncio.run()-created event loop (watchlist.check_triggers(), called
    from collect_all.py's background thread) rather than the main FastAPI
    loop. asyncpg connections are loop-bound; handing one out of engine()'s
    pool (populated from connections opened under the main loop) to a
    different loop crashes with "attached to a different loop" / "got
    result for unknown protocol state 3" -- confirmed live in production,
    every single collect_all cycle, 2026-08-02, silently swallowing every
    Watchlist Discord notification with no visible symptom on the dashboard
    itself. The engine here is created and fully disposed within this one
    call, so no connection ever escapes to be reused by a different loop.
    pool_size=1: a single-threaded, once-per-cycle check, not concurrent
    request traffic, doesn't need engine()'s much larger pool."""
    bg_engine = create_async_engine(_database_url(), pool_size=1, max_overflow=0)
    try:
        bg_sessionmaker = async_sessionmaker(bg_engine, expire_on_commit=False)
        async with bg_sessionmaker() as session:
            yield session
    finally:
        await bg_engine.dispose()


async def get_async_session():
    async with sessionmaker()() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)
