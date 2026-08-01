"""Per-user "WoW accounts" registration -- lets a subscribed user record
which of their real WoW accounts (a self-declared label only, e.g. "Main",
"Alt account" -- no Blizzard OAuth/credentials involved anywhere) has a
character on which EU connected-realm. dashboard.html cross-references this
against each snipe row's buy-side realm to show which account to log into.

Mirrors forum.py's shape: own APIRouter, Depends(current_subscribed_user)
(not current_active_user -- gated the same as the paid sniping feature
itself, this is the first real consumer of that dependency), manual
if/raise HTTPException validation, session.add/commit, a JSON serializer.

Caps (MAX_ACCOUNTS_PER_USER=8 -- matches Blizzard's real per-Battle.net-
account WoW account limit, human-specified 2026-08-02 -- MAX_REALMS_PER_ACCOUNT=50,
also human-specified) are enforced via an atomic INSERT...SELECT...WHERE rather than a
Python-side SELECT COUNT(*) then INSERT -- this app has two recorded TOCTOU
bugs from exactly that pattern (dashboard._enforce_realm_lock's old
read-then-write race, item_names.NameCache.save()'s lost-update race, both
in HISTORY.md), both fixed by making the check and the write one atomic DB
statement instead of two round trips. Unlike locked_sell_realm (which
bounds real compute/API cost) or the Blizzard rate limiter (which bounds a
real external budget), these caps are pure UX limits on self-declared
metadata with no cost or security consequence if raced by a row or two --
the atomic approach is used anyway because it's the same amount of code and
matches this codebase's now-established convention, not because the stakes
demand it. The duplicate-realm case doesn't need a second atomic check at
all: WowAccountRealm's UniqueConstraint is the actual atomic source of
truth, caught here as an IntegrityError.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_subscribed_user
from db import User, WowAccount, WowAccountRealm, get_async_session

router = APIRouter(prefix="/api/wow-accounts", tags=["wow_accounts"])

MAX_ACCOUNTS_PER_USER = 8
MAX_REALMS_PER_ACCOUNT = 50
LABEL_MAX_LEN = 50  # matches dashboard.NICKNAME_MAX_LEN's precedent


def _account_to_json(account: WowAccount, realms: list[int]) -> dict:
    return {"id": account.id, "label": account.label, "realms": realms}


async def _get_owned_account(account_id: int, user: User, session: AsyncSession) -> WowAccount:
    account = (await session.execute(
        select(WowAccount).where(WowAccount.id == account_id, WowAccount.owner_id == user.id)
    )).scalar_one_or_none()
    if account is None:
        # No distinction from "exists but isn't yours" -- same
        # non-existence-leaking precedent used everywhere else in this app.
        raise HTTPException(404, "WoW account not found")
    return account


async def _realm_ids_for_account(account_id: int, session: AsyncSession) -> list[int]:
    return list((await session.execute(
        select(WowAccountRealm.connected_realm_id).where(WowAccountRealm.wow_account_id == account_id)
    )).scalars().all())


async def _insert_account_atomic(owner_id: uuid.UUID, label: str, session: AsyncSession) -> bool:
    """One INSERT...SELECT...WHERE statement -- the count check and the
    write happen as a single atomic DB operation, not a separate `SELECT
    COUNT(*)` followed by a Python-side `if`, which would let two concurrent
    callers both observe count == 7 before either commits (see module
    docstring). Returns True if the row was actually inserted, False if the
    cap was already reached. Caller is responsible for committing --
    exposed as its own function (not inlined in create_account()) so tests
    can drive the exact interleaving directly with independent sessions,
    same testability precedent as dashboard._enforce_realm_lock."""
    count_subq = (
        select(func.count()).select_from(WowAccount)
        .where(WowAccount.owner_id == owner_id)
        .scalar_subquery()
    )
    insert_stmt = insert(WowAccount).from_select(
        [WowAccount.owner_id, WowAccount.label, WowAccount.created_at],
        select(
            literal(owner_id, type_=WowAccount.owner_id.type),
            literal(label, type_=WowAccount.label.type),
            literal(datetime.now(timezone.utc), type_=WowAccount.created_at.type),
        ).where(count_subq < MAX_ACCOUNTS_PER_USER),
    )
    result = await session.execute(insert_stmt)
    return result.rowcount > 0


async def _insert_realm_atomic(wow_account_id: int, connected_realm_id: int, session: AsyncSession) -> bool:
    """Same atomic-cap pattern as _insert_account_atomic, for
    MAX_REALMS_PER_ACCOUNT. Raises sqlalchemy.exc.IntegrityError (uncaught)
    if this realm is already registered on the account -- the table's
    UniqueConstraint is the actual atomic source of truth for the duplicate
    case, so no separate pre-check is needed here; the caller catches it."""
    count_subq = (
        select(func.count()).select_from(WowAccountRealm)
        .where(WowAccountRealm.wow_account_id == wow_account_id)
        .scalar_subquery()
    )
    insert_stmt = insert(WowAccountRealm).from_select(
        [WowAccountRealm.wow_account_id, WowAccountRealm.connected_realm_id, WowAccountRealm.created_at],
        select(
            literal(wow_account_id, type_=WowAccountRealm.wow_account_id.type),
            literal(connected_realm_id, type_=WowAccountRealm.connected_realm_id.type),
            literal(datetime.now(timezone.utc), type_=WowAccountRealm.created_at.type),
        ).where(count_subq < MAX_REALMS_PER_ACCOUNT),
    )
    result = await session.execute(insert_stmt)
    return result.rowcount > 0


@router.get("")
async def list_accounts(user: User = Depends(current_subscribed_user),
                        session: AsyncSession = Depends(get_async_session)) -> dict:
    accounts = (await session.execute(
        select(WowAccount).where(WowAccount.owner_id == user.id).order_by(WowAccount.id)
    )).scalars().all()
    account_ids = [a.id for a in accounts]
    realms_by_account: dict[int, list[int]] = {aid: [] for aid in account_ids}
    if account_ids:
        rows = (await session.execute(
            select(WowAccountRealm.wow_account_id, WowAccountRealm.connected_realm_id)
            .where(WowAccountRealm.wow_account_id.in_(account_ids))
        )).all()
        for wow_account_id, connected_realm_id in rows:
            realms_by_account[wow_account_id].append(connected_realm_id)
    return {"accounts": [_account_to_json(a, realms_by_account[a.id]) for a in accounts]}


class WowAccountCreate(BaseModel):
    label: str


@router.post("")
async def create_account(payload: WowAccountCreate, user: User = Depends(current_subscribed_user),
                         session: AsyncSession = Depends(get_async_session)) -> dict:
    label = payload.label.strip()
    if not label:
        raise HTTPException(400, "label can't be empty")
    if len(label) > LABEL_MAX_LEN:
        raise HTTPException(400, f"label must be {LABEL_MAX_LEN} characters or fewer")

    inserted = await _insert_account_atomic(user.id, label, session)
    await session.commit()
    if not inserted:
        raise HTTPException(400, f"maximum {MAX_ACCOUNTS_PER_USER} WoW accounts")

    # Look up the row we just inserted rather than trusting anything held in
    # memory -- same "the DB is the source of truth, not an in-memory
    # assumption" precedent as _enforce_realm_lock's own follow-up SELECT.
    account = (await session.execute(
        select(WowAccount).where(WowAccount.owner_id == user.id, WowAccount.label == label)
        .order_by(WowAccount.id.desc())
    )).scalars().first()
    return _account_to_json(account, [])


class WowAccountRename(BaseModel):
    label: str


@router.patch("/{account_id}")
async def rename_account(account_id: int, payload: WowAccountRename,
                         user: User = Depends(current_subscribed_user),
                         session: AsyncSession = Depends(get_async_session)) -> dict:
    account = await _get_owned_account(account_id, user, session)
    label = payload.label.strip()
    if not label:
        raise HTTPException(400, "label can't be empty")
    if len(label) > LABEL_MAX_LEN:
        raise HTTPException(400, f"label must be {LABEL_MAX_LEN} characters or fewer")
    account.label = label
    await session.commit()
    return _account_to_json(account, await _realm_ids_for_account(account.id, session))


@router.delete("/{account_id}")
async def delete_account(account_id: int, user: User = Depends(current_subscribed_user),
                         session: AsyncSession = Depends(get_async_session)) -> dict:
    account = await _get_owned_account(account_id, user, session)
    # No DB-level cascade (see db.py's WowAccountRealm docstring) -- delete
    # children explicitly, same transaction.
    await session.execute(delete(WowAccountRealm).where(WowAccountRealm.wow_account_id == account.id))
    await session.delete(account)
    await session.commit()
    return {"deleted": account_id}


class WowAccountRealmAdd(BaseModel):
    connected_realm_id: int


@router.post("/{account_id}/realms")
async def add_realm(account_id: int, payload: WowAccountRealmAdd,
                    user: User = Depends(current_subscribed_user),
                    session: AsyncSession = Depends(get_async_session)) -> dict:
    account = await _get_owned_account(account_id, user, session)
    if payload.connected_realm_id <= 0:
        raise HTTPException(400, "invalid realm id")

    try:
        inserted = await _insert_realm_atomic(account.id, payload.connected_realm_id, session)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "realm already added to this account")
    if not inserted:
        raise HTTPException(400, f"maximum {MAX_REALMS_PER_ACCOUNT} realms per account")

    return _account_to_json(account, await _realm_ids_for_account(account.id, session))


@router.delete("/{account_id}/realms/{connected_realm_id}")
async def remove_realm(account_id: int, connected_realm_id: int,
                       user: User = Depends(current_subscribed_user),
                       session: AsyncSession = Depends(get_async_session)) -> dict:
    account = await _get_owned_account(account_id, user, session)
    result = await session.execute(
        delete(WowAccountRealm).where(
            WowAccountRealm.wow_account_id == account.id,
            WowAccountRealm.connected_realm_id == connected_realm_id,
        )
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "realm not registered on this account")
    return _account_to_json(account, await _realm_ids_for_account(account.id, session))
