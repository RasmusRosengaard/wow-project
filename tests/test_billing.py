"""Tests for billing.py: Checkout Session creation + Stripe webhook handling.
Same throwaway-SQLite-DB pattern as test_auth.py (real DB writes, not the
dependency-override bypass test_dashboard.py uses) since this module's whole
job is writing subscription state to the User row correctly."""
import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import billing
import dashboard
from db import Base, User, get_async_session

EMAIL = "test@example.com"
PASSWORD = "testpassword123"
WEBHOOK_SECRET = "whsec_test_secret"


async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(db_url)
    asyncio.run(_create_tables(engine))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(billing, "WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(billing, "PRICE_ID", "price_test_123")
    # billing.stripe.api_key is read from STRIPE_SECRET_KEY at billing.py's
    # import time. Locally that's whatever real key happens to be in .env
    # (masking this), but CI has no .env -- api_key would be None there,
    # tripping create_checkout_session's own "not configured" 500 guard
    # before ever reaching the mocked Session.create below. Patch it
    # explicitly so the test is deterministic regardless of environment.
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake_key_for_tests")
    dashboard.app.dependency_overrides[get_async_session] = override_get_async_session
    try:
        with TestClient(dashboard.app) as c:
            c.session_factory = session_factory
            yield c
    finally:
        dashboard.app.dependency_overrides.pop(get_async_session, None)
        asyncio.run(engine.dispose())


def register(client, email=EMAIL, password=PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password})


def login(client, email=EMAIL, password=PASSWORD):
    return client.post("/auth/login", data={"username": email, "password": password})


async def _get_user(session_factory, email: str) -> User:
    async with session_factory() as session:
        return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def _verify_user(session_factory, email: str) -> None:
    """/billing/checkout requires a confirmed email address as of 2026-08-06
    (auth.current_verified_user -- don't start a recurring charge against an
    address that may not exist). Registration leaves an account unverified, so
    any test about checkout *mechanics* has to clear that gate first; the gate
    itself is covered in tests/test_auth.py, not here."""
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.is_verified = True
        await session.commit()


def sign_payload(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> tuple[bytes, str]:
    """Constructs a valid Stripe webhook signature header per Stripe's
    documented scheme (HMAC-SHA256 of "{timestamp}.{payload}"), so tests
    exercise the real verification path (stripe.Webhook.construct_event)
    instead of mocking it away."""
    ts = timestamp or int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={sig}"


def webhook_event(event_type: str, obj: dict, event_id: str = "evt_test") -> bytes:
    # Minimal but structurally real Stripe v1 Event object -- the SDK's own
    # construct_event() inspects top-level "object"/"api_version" to decide
    # how to parse it, so a bare {"type":..., "data":...} isn't enough.
    return json.dumps({
        "id": event_id, "object": "event", "type": event_type,
        "api_version": "2026-06-24.dahlia",
        "data": {"object": obj},
    }).encode()


def test_checkout_requires_login(client):
    r = client.post("/billing/checkout")
    assert r.status_code == 401


def test_checkout_creates_session_and_returns_url(client, monkeypatch):
    register(client)
    login(client)
    asyncio.run(_verify_user(client.session_factory, EMAIL))
    user = asyncio.run(_get_user(client.session_factory, EMAIL))

    captured = {}

    class FakeSession:
        url = "https://checkout.stripe.com/fake-session"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr(billing.stripe.checkout.Session, "create", fake_create)

    r = client.post("/billing/checkout")
    assert r.status_code == 200
    assert r.json()["url"] == "https://checkout.stripe.com/fake-session"
    assert captured["mode"] == "subscription"
    assert captured["line_items"] == [{"price": "price_test_123", "quantity": 1}]
    assert captured["client_reference_id"] == str(user.id)
    assert captured["customer_email"] == EMAIL


def test_portal_requires_login(client):
    r = client.post("/billing/portal")
    assert r.status_code == 401


def test_portal_requires_existing_stripe_customer(client):
    register(client)
    login(client)
    r = client.post("/billing/portal")
    assert r.status_code == 400
    assert "subscribe first" in r.json()["detail"].lower()


def test_portal_creates_session_and_returns_url(client, monkeypatch):
    register(client)
    login(client)
    user = asyncio.run(_get_user(client.session_factory, EMAIL))

    async def _seed():
        async with client.session_factory() as session:
            u = await session.get(User, user.id)
            u.stripe_customer_id = "cus_test123"
            await session.commit()
    asyncio.run(_seed())

    captured = {}

    class FakeSession:
        url = "https://billing.stripe.com/fake-portal-session"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr(billing.stripe.billing_portal.Session, "create", fake_create)

    r = client.post("/billing/portal")
    assert r.status_code == 200
    assert r.json()["url"] == "https://billing.stripe.com/fake-portal-session"
    assert captured["customer"] == "cus_test123"
    assert captured["return_url"].endswith("/profile")


def test_webhook_rejects_bad_signature(client):
    payload = webhook_event("checkout.session.completed", {})
    r = client.post("/billing/webhook", content=payload, headers={"stripe-signature": "t=1,v1=bogus"})
    assert r.status_code == 400


def test_webhook_checkout_completed_activates_subscription(client, monkeypatch):
    register(client)
    user = asyncio.run(_get_user(client.session_factory, EMAIL))

    class FakeSubscription(dict):
        def to_dict(self):
            return dict(self)

    monkeypatch.setattr(billing.stripe.Subscription, "retrieve",
                        lambda sub_id: FakeSubscription(status="active", current_period_end=1_800_000_000))

    payload, sig = sign_payload(webhook_event("checkout.session.completed", {
        "client_reference_id": str(user.id),
        "customer": "cus_test123",
        "subscription": "sub_test456",
    }))
    r = client.post("/billing/webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200

    updated = asyncio.run(_get_user(client.session_factory, EMAIL))
    assert updated.subscription_status == "active"
    assert updated.stripe_customer_id == "cus_test123"
    assert updated.stripe_subscription_id == "sub_test456"
    assert updated.subscription_current_period_end is not None


def test_webhook_subscription_updated_changes_status(client, monkeypatch):
    register(client)
    user = asyncio.run(_get_user(client.session_factory, EMAIL))

    async def _seed():
        async with client.session_factory() as session:
            u = await session.get(User, user.id)
            u.stripe_customer_id = "cus_test123"
            u.subscription_status = "active"
            await session.commit()
    asyncio.run(_seed())

    payload, sig = sign_payload(webhook_event("customer.subscription.updated", {
        "customer": "cus_test123", "status": "past_due", "current_period_end": 1_800_000_000,
    }))
    r = client.post("/billing/webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200

    updated = asyncio.run(_get_user(client.session_factory, EMAIL))
    assert updated.subscription_status == "past_due"


def test_period_end_falls_back_to_subscription_item(monkeypatch):
    """Real bug fix (2026-07-31, found during a bug audit): current_period_end
    used to live directly on the Subscription object; recent Stripe API
    versions moved it onto each SubscriptionItem instead. This project pins
    no stripe.api_version, so whichever version is actually live on the
    account governs which shape a real webhook sends -- never confirmed
    directly, so _period_end() must handle both rather than assuming the
    old shape and silently writing None for every renewal date on a newer
    API version."""
    top_level = billing._period_end({"current_period_end": 1_800_000_000})
    assert top_level is not None

    item_level_only = billing._period_end({
        "current_period_end": None,
        "items": {"data": [{"current_period_end": 1_800_000_000}]},
    })
    assert item_level_only == top_level

    neither = billing._period_end({"current_period_end": None, "items": {"data": []}})
    assert neither is None

    missing_items_key = billing._period_end({"current_period_end": None})
    assert missing_items_key is None


def test_webhook_subscription_updated_reads_period_end_from_item_when_top_level_missing(client, monkeypatch):
    """Same fallback as test_period_end_falls_back_to_subscription_item
    above, exercised through the real webhook route rather than calling
    _period_end() directly."""
    register(client)
    user = asyncio.run(_get_user(client.session_factory, EMAIL))

    async def _seed():
        async with client.session_factory() as session:
            u = await session.get(User, user.id)
            u.stripe_customer_id = "cus_test123"
            u.subscription_status = "active"
            await session.commit()
    asyncio.run(_seed())

    payload, sig = sign_payload(webhook_event("customer.subscription.updated", {
        "customer": "cus_test123", "status": "active",
        "current_period_end": None,
        "items": {"data": [{"current_period_end": 1_800_000_000}]},
    }))
    r = client.post("/billing/webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200

    updated = asyncio.run(_get_user(client.session_factory, EMAIL))
    assert updated.subscription_current_period_end is not None


def test_webhook_subscription_deleted_revokes_access(client, monkeypatch):
    register(client)
    user = asyncio.run(_get_user(client.session_factory, EMAIL))

    async def _seed():
        async with client.session_factory() as session:
            u = await session.get(User, user.id)
            u.stripe_customer_id = "cus_test123"
            u.subscription_status = "active"
            await session.commit()
    asyncio.run(_seed())

    payload, sig = sign_payload(webhook_event("customer.subscription.deleted", {
        "customer": "cus_test123",
    }))
    r = client.post("/billing/webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200

    updated = asyncio.run(_get_user(client.session_factory, EMAIL))
    assert updated.subscription_status == "canceled"


def test_webhook_unknown_customer_does_not_error(client):
    payload, sig = sign_payload(webhook_event("customer.subscription.updated", {
        "customer": "cus_does_not_exist", "status": "active",
    }))
    r = client.post("/billing/webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200
