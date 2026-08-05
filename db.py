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
from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String,
                        UniqueConstraint, true as sa_true)
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
    # "Default sniper list" -- opt-in to watchlist.py's standing rule scan
    # (added 2026-08-05). Distinct from discord_webhook_url: the webhook is
    # *where* notifications go, this is *whether* the standing rule is one
    # of the things that sends them. A user can keep their per-item triggers
    # and turn only the standing rule off.
    #
    # NOT NULL, server_default true: the rule shipped earlier the same day
    # already delivering to every subscriber with a webhook, so defaulting
    # existing rows to false would silently switch off a live feature for
    # everyone who currently has it. New accounts get the same default via
    # the model-level default.
    default_sniper_list_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_true())
    # Per-user overrides for the standing rule's two thresholds (added
    # 2026-08-05). **NULL means "use the current default"**, deliberately,
    # rather than backfilling every row with today's numbers: the defaults
    # live in watchlist.py (RULE_MIN_SALE_AVG_COPPER /
    # RULE_BUY_FRACTION_OF_SALE_AVG) and will be retuned again, and a
    # backfill would silently freeze every existing account at whatever the
    # value happened to be on migration day. Only a user who has actually
    # tuned these gets a stored number.
    sniper_min_sale_avg_copper: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sniper_buy_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Signup timestamp (added 2026-08-04 for the admin page's signup list --
    # FastAPI-Users' base table has no such column, and uuid4 ids carry no
    # embedded time, so there was no way to order accounts by age).
    # Deliberately NULLABLE with no backfill: accounts that existed before
    # this shipped have no recoverable signup date, and stamping them all
    # with the migration's own timestamp would invent one -- every
    # pre-existing account would read as "signed up the day this deployed",
    # which is worse than admitting we don't know. The admin page renders
    # NULL as "before tracking". New registrations get it from this default
    # automatically (SQLAlchemy applies it on insert, so auth.py's
    # UserManager needs no change).
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        default=lambda: datetime.now(timezone.utc))


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
    any sell realm (see .claude/docs/feature-watchlist.md -- Watchlist has no sell realm
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


class AnonSession(Base):
    """First-party anonymous-visitor identity (2026-08-03) -- lets a visitor with no
    account use /snipes, while still enforcing the same "locked to first sell realm
    queried" rule a free-tier User gets (see User.locked_sell_realm), to bound how many
    distinct expensive DuckDB queries one anonymous visitor can generate. Identified by
    an opaque cookie value (ah_anon, see auth.py's resolve_or_create_anon_session()), NOT
    IP (human decision: IPs are shared/NAT'd and change across sessions, which would
    cause false locks/resets for unrelated visitors sharing a network). token is the
    primary key directly -- generated the same way forum.py generates its opaque
    filenames (uuid.uuid4().hex), just used as a PK here instead of a filename.

    No cleanup/expiry mechanism (deliberately deferred) -- this app has no precedent for
    reaping rows in any similar one-row-per-something table (e.g. ForumPost); unbounded
    growth here is accepted the same way unless it becomes a real problem."""
    __tablename__ = "anon_session"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    locked_sell_realm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))


class VisitorIP(Base):
    """Persistent visitor history for the superuser admin page (2026-08-04,
    human request: "current active users ... history of all IP's accessing
    page"). One row per distinct client IP, NOT one per request or per visit
    (human decision) -- this table is bounded by how many distinct visitors
    the site has ever had, so it stays small indefinitely, where a
    row-per-request log would grow by thousands a day and put exactly the
    kind of write pressure on the pool that already caused a real outage
    (see engine()'s comment below).

    Supersedes the in-memory-only tracker admin.py used to be: "who's on the
    site right now" is just last_seen within admin.ACTIVE_WINDOW_SECONDS, so
    both the live view and the history read from this one table.

    Nothing here is written on the request path. admin.track_activity() only
    touches an in-process dict; admin._visitor_flush_loop() batches those
    into this table roughly once a minute.

    first_seen is when the *flush* first observed the IP, not a true
    first-ever-visit for IPs that were already visiting before this table
    existed -- there was no history to backfill from, so early rows read as
    "first seen since this shipped".

    No geolocation columns yet (human decision, 2026-08-04: ship the
    tracking first, add location later) -- adding nullable country/city/org
    is a trivial follow-up migration when a provider is picked, and empty
    columns in the meantime would just be dead UI."""
    __tablename__ = "visitor_ip"

    # 45 chars fits the longest possible IPv6 form (an IPv4-mapped address
    # like ::ffff:192.168.100.228 plus a zone suffix); a header-supplied
    # X-Forwarded-For value is truncated to fit rather than rejected -- see
    # admin._client_ip().
    ip: Mapped[str] = mapped_column(String(45), primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))
    # Indexed: the admin page's two reads are both "most recently active
    # first" -- the live view filters last_seen >= cutoff, the history
    # orders by it.
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True,
                                                default=lambda: datetime.now(timezone.utc))
    # Total /api/* requests ever seen from this IP -- accumulated across
    # flushes, so it survives the redeploys that reset the in-memory dict.
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    .claude/docs/history.md). Root cause: every route gated by auth.current_active_user
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
