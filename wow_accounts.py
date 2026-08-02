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


def _account_to_json(account: WowAccount, realms: list[int], realm_names: dict[int, str] | None = None) -> dict:
    # realm_names (added 2026-08-02): the specific member name picked at
    # registration time, id -> name, only present for realms that have one
    # (rows added before this existed have none -- see db.WowAccountRealm's
    # docstring). Additive to the existing plain `realms` id list rather
    # than replacing it, so dashboard.html's existing id-only cross-
    # reference logic is untouched.
    return {"id": account.id, "label": account.label, "realms": realms, "realm_names": realm_names or {}}


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


async def _realm_names_for_account(account_id: int, session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(
        select(WowAccountRealm.connected_realm_id, WowAccountRealm.realm_name)
        .where(WowAccountRealm.wow_account_id == account_id, WowAccountRealm.realm_name.isnot(None))
    )).all()
    return {cr_id: name for cr_id, name in rows}


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


async def _insert_realm_atomic(wow_account_id: int, connected_realm_id: int, session: AsyncSession,
                               realm_name: str | None = None) -> bool:
    """Same atomic-cap pattern as _insert_account_atomic, for
    MAX_REALMS_PER_ACCOUNT. Raises sqlalchemy.exc.IntegrityError (uncaught)
    if this realm is already registered on the account -- the table's
    UniqueConstraint is the actual atomic source of truth for the duplicate
    case, so no separate pre-check is needed here; the caller catches it.

    realm_name (added 2026-08-02): the specific member name the user
    searched/picked, stored purely for display -- see db.WowAccountRealm's
    docstring. Optional; matching stays by connected_realm_id alone."""
    count_subq = (
        select(func.count()).select_from(WowAccountRealm)
        .where(WowAccountRealm.wow_account_id == wow_account_id)
        .scalar_subquery()
    )
    insert_stmt = insert(WowAccountRealm).from_select(
        [WowAccountRealm.wow_account_id, WowAccountRealm.connected_realm_id,
         WowAccountRealm.realm_name, WowAccountRealm.created_at],
        select(
            literal(wow_account_id, type_=WowAccountRealm.wow_account_id.type),
            literal(connected_realm_id, type_=WowAccountRealm.connected_realm_id.type),
            literal(realm_name, type_=WowAccountRealm.realm_name.type),
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
    names_by_account: dict[int, dict[int, str]] = {aid: {} for aid in account_ids}
    if account_ids:
        rows = (await session.execute(
            select(WowAccountRealm.wow_account_id, WowAccountRealm.connected_realm_id, WowAccountRealm.realm_name)
            .where(WowAccountRealm.wow_account_id.in_(account_ids))
        )).all()
        for wow_account_id, connected_realm_id, realm_name in rows:
            realms_by_account[wow_account_id].append(connected_realm_id)
            if realm_name:
                names_by_account[wow_account_id][connected_realm_id] = realm_name
    return {"accounts": [_account_to_json(a, realms_by_account[a.id], names_by_account[a.id]) for a in accounts]}


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
    return _account_to_json(account, await _realm_ids_for_account(account.id, session),
                            await _realm_names_for_account(account.id, session))


class WowAccountRealmInput(BaseModel):
    connected_realm_id: int
    realm_name: str | None = None


class WowAccountBatchUpdate(BaseModel):
    label: str | None = None
    realms: list[WowAccountRealmInput] | None = None


@router.patch("/{account_id}/batch")
async def batch_update_account(account_id: int, payload: WowAccountBatchUpdate,
                               user: User = Depends(current_subscribed_user),
                               session: AsyncSession = Depends(get_async_session)) -> dict:
    """Saves a rename and/or a full realm-set replacement in one request
    (added 2026-08-02, human request -- same "don't fire a request per
    click" principle as watchlist.py's own /batch route, applied here too:
    profile.html's card now lets a user add/remove several realms locally
    before ever hitting Save, instead of one POST/DELETE /realms round trip
    per click). Either field is optional -- omit label to leave the name
    untouched, omit realms to leave realms untouched.

    realms, when given, is treated as the account's *complete* desired
    realm set, not a delta -- the diff (add missing, remove extra) is
    computed here, not by the client. Not wrapped in the atomic
    INSERT...SELECT...WHERE pattern _insert_realm_atomic() uses (see that
    function's docstring / module docstring for why it exists elsewhere):
    that pattern fits "insert one row if under a count," not "replace a
    whole set to match exactly this list," and two concurrent batch saves
    on the *same* account by the *same* user is an extreme edge case for
    self-declared personal metadata with no cost/security consequence if
    it ever produced a merged-rather-than-one-wins result -- not worth the
    added complexity here, same reasoning this module's docstring already
    gives for why the atomic pattern is convention, not a hard requirement,
    for caps like this one.

    Each entry also carries the specific realm_name the user picked
    (2026-08-02) -- for an id already registered, its stored name is
    synced to whatever the client sends (a user can re-pick a different
    member name for the same connected realm later); a null/omitted name
    leaves an existing stored name untouched rather than blanking it, so a
    plain realm-removal-only save elsewhere in the payload can't
    accidentally wipe a name it never meant to touch."""
    account = await _get_owned_account(account_id, user, session)

    if payload.label is not None:
        label = payload.label.strip()
        if not label:
            raise HTTPException(400, "label can't be empty")
        if len(label) > LABEL_MAX_LEN:
            raise HTTPException(400, f"label must be {LABEL_MAX_LEN} characters or fewer")
        account.label = label

    if payload.realms is not None:
        desired = {r.connected_realm_id: r.realm_name for r in payload.realms}
        if len(desired) > MAX_REALMS_PER_ACCOUNT:
            raise HTTPException(400, f"maximum {MAX_REALMS_PER_ACCOUNT} realms per account")
        existing_rows = (await session.execute(
            select(WowAccountRealm).where(WowAccountRealm.wow_account_id == account.id)
        )).scalars().all()
        current = {r.connected_realm_id: r for r in existing_rows}
        to_remove = current.keys() - desired.keys()
        to_add = desired.keys() - current.keys()
        if to_remove:
            await session.execute(delete(WowAccountRealm).where(
                WowAccountRealm.wow_account_id == account.id,
                WowAccountRealm.connected_realm_id.in_(to_remove),
            ))
        for realm_id in to_add:
            session.add(WowAccountRealm(wow_account_id=account.id, connected_realm_id=realm_id,
                                        realm_name=desired[realm_id]))
        for realm_id in (current.keys() & desired.keys()):
            new_name = desired[realm_id]
            if new_name is not None and current[realm_id].realm_name != new_name:
                current[realm_id].realm_name = new_name

    await session.commit()
    return _account_to_json(account, await _realm_ids_for_account(account.id, session),
                            await _realm_names_for_account(account.id, session))


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
    realm_name: str | None = None


@router.post("/{account_id}/realms")
async def add_realm(account_id: int, payload: WowAccountRealmAdd,
                    user: User = Depends(current_subscribed_user),
                    session: AsyncSession = Depends(get_async_session)) -> dict:
    account = await _get_owned_account(account_id, user, session)
    if payload.connected_realm_id <= 0:
        raise HTTPException(400, "invalid realm id")

    try:
        inserted = await _insert_realm_atomic(account.id, payload.connected_realm_id, session, payload.realm_name)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "realm already added to this account")
    if not inserted:
        raise HTTPException(400, f"maximum {MAX_REALMS_PER_ACCOUNT} realms per account")

    return _account_to_json(account, await _realm_ids_for_account(account.id, session),
                            await _realm_names_for_account(account.id, session))


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
    return _account_to_json(account, await _realm_ids_for_account(account.id, session),
                            await _realm_names_for_account(account.id, session))
