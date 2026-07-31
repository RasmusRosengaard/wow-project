"""Stripe billing: Checkout Session creation + webhook handling for the
single €4.99/month subscription that gates the sniper page. Written and
deployed straight to live mode (human decision 2026-07-23) -- there was no
test-mode verification pass first, so treat the initial live transactions as
the actual verification.

Webhook endpoint (Stripe Dashboard -> Developers -> Webhooks) listens for
exactly three events, nothing more:
  checkout.session.completed    -- new subscription: look up the full
                                    Subscription object (status won't be on
                                    the Checkout Session itself) and record it
  customer.subscription.updated -- renewals, plan changes, payment failures
                                    moving status to past_due
  customer.subscription.deleted -- the subscription actually ends (Stripe
                                    fires `updated` with cancel_at_period_end
                                    first, `deleted` only once the period lapses)

subscription_status/stripe_customer_id/stripe_subscription_id/
subscription_current_period_end on db.User are written *only* here, from
verified webhook events -- never trust client input for these fields.
"""
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_active_user
from db import User, get_async_session

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/checkout")
async def create_checkout_session(request: Request, user: User = Depends(current_active_user)) -> dict:
    if not stripe.api_key or not PRICE_ID:
        raise HTTPException(500, "Stripe is not configured (STRIPE_SECRET_KEY/STRIPE_PRICE_ID)")

    base_url = str(request.base_url).rstrip("/")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": PRICE_ID, "quantity": 1}],
        customer=user.stripe_customer_id,
        customer_email=None if user.stripe_customer_id else user.email,
        client_reference_id=str(user.id),
        # /snipes, not / -- the sniper tool moved off the root path
        # 2026-07-26 (now the public marketing page); missed in that
        # migration (every other post-login/post-action redirect was
        # updated, this one wasn't -- found during a 2026-07-31 bug audit),
        # so a paying customer landed back on the marketing page instead of
        # the tool they just paid for.
        success_url=f"{base_url}/snipes?checkout=success",
        cancel_url=f"{base_url}/subscribe?checkout=cancelled",
    )
    return {"url": session.url}


@router.post("/portal")
async def create_portal_session(request: Request, user: User = Depends(current_active_user)) -> dict:
    """Redirects to Stripe's hosted Customer Portal -- cancel, update payment
    method, view invoices/receipts. Deliberately not hand-built: Stripe's own
    guidance is to use the portal for self-service subscription management
    rather than custom cancel/update endpoints, and it's what actually
    changes the subscription (our webhook handler picks up the resulting
    customer.subscription.updated/deleted event same as any other change)."""
    if not user.stripe_customer_id:
        raise HTTPException(400, "No Stripe customer on file yet -- subscribe first")

    base_url = str(request.base_url).rstrip("/")
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{base_url}/profile",
    )
    return {"url": session.url}


async def _user_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def _user_by_stripe_customer_id(session: AsyncSession, customer_id: str) -> User | None:
    result = await session.execute(select(User).where(User.stripe_customer_id == customer_id))
    return result.scalar_one_or_none()


def _period_end(subscription: dict) -> datetime | None:
    """current_period_end used to live directly on the Subscription object;
    recent Stripe API versions moved it onto each SubscriptionItem instead
    (found during a 2026-07-31 bug audit -- this project pins no
    stripe.api_version, so whichever version is actually live on the
    account governs the shape Stripe sends, and that was never confirmed
    directly). Checks both locations rather than assuming one -- without
    this, an account on a newer API version could have every webhook
    silently write None for the renewal date, with no error raised
    anywhere."""
    ts = subscription.get("current_period_end")
    if ts is None:
        items = (subscription.get("items") or {}).get("data") or []
        if items:
            ts = items[0].get("current_period_end")
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


@router.post("/webhook")
async def stripe_webhook(request: Request, session: AsyncSession = Depends(get_async_session)) -> dict:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not WEBHOOK_SECRET:
        raise HTTPException(500, "STRIPE_WEBHOOK_SECRET not configured")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        # Never trust an unverified body -- this is the one thing that must
        # never be skipped, webhook bodies are otherwise unauthenticated.
        raise HTTPException(400, "invalid webhook signature")

    event_type = event["type"]
    # event["data"]["object"] is a StripeObject, not a plain dict -- it
    # supports obj["key"] but NOT obj.get("key"), which every handler below
    # relies on. to_dict() once here rather than switching every call site
    # to subscript access.
    obj = event["data"]["object"].to_dict()

    if event_type == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        if not (user_id and customer_id and subscription_id):
            return {"received": True}  # not a subscription checkout we care about

        user = await _user_by_id(session, user_id)
        if user is None:
            return {"received": True}

        subscription = stripe.Subscription.retrieve(subscription_id).to_dict()
        user.stripe_customer_id = customer_id
        user.stripe_subscription_id = subscription_id
        user.subscription_status = subscription.get("status")
        user.subscription_current_period_end = _period_end(subscription)
        await session.commit()

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        user = await _user_by_stripe_customer_id(session, customer_id) if customer_id else None
        if user is None:
            return {"received": True}

        user.subscription_status = "canceled" if event_type == "customer.subscription.deleted" else obj.get("status")
        user.subscription_current_period_end = _period_end(obj)
        await session.commit()

    return {"received": True}
