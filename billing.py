"""Billing.

DEMO_MODE (default ON): Stripe is mocked. The "Subscribe" button on the
/billing page instantly activates the plan with a 14-day trial — no keys,
no cards, no network calls.

Real mode: set DEMO_MODE=0 and STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET.
Subscribing redirects to Stripe Checkout (14-day trial, card collected but
not charged until trial ends). Webhook events from Stripe activate and sync
subscriptions via POST /webhooks/stripe.
"""
import os
from datetime import datetime, timezone

from store import get_subscription, set_subscription, set_stripe_ids

DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

PLANS = {
    "starter":   {"name": "Starter",   "price": 19, "units": 5,  "blurb": "Up to 5 units · 1 property"},
    "growth":    {"name": "Growth",    "price": 39, "units": 20, "blurb": "Up to 20 units · unlimited properties · late-fee calculator · monthly report"},
    "portfolio": {"name": "Portfolio", "price": 79, "units": 50, "blurb": "Up to 50 units · priority support · multiple users"},
}

TRIAL_DAYS = 14
CYCLES = ("monthly", "annual")

_stripe = None
_price_cache = {}


def stripe_configured():
    return bool(STRIPE_SECRET_KEY) and not DEMO_MODE


def _stripe_lib():
    global _stripe
    if _stripe is None:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        _stripe = stripe
    return _stripe


def price_id(plan: str, cycle: str = "monthly") -> str:
    """Resolve a Stripe Price ID from its lookup key (cached)."""
    key = (plan, cycle)
    if key not in _price_cache:
        s = _stripe_lib()
        lookup = f"rentping_{plan}_{cycle}"
        prices = s.Price.list(lookup_keys=[lookup], limit=1)
        if not prices.data:
            raise ValueError(f"no Stripe price with lookup key {lookup}")
        _price_cache[key] = prices.data[0].id
    return _price_cache[key]


def activate_demo_subscription(landlord_id: int, plan: str) -> dict:
    """What the demo Subscribe button does: start the 14-day trial instantly."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan: {plan}")
    trial_ends = (datetime.now(timezone.utc).timestamp() + TRIAL_DAYS * 86400)
    trial_ends_iso = datetime.fromtimestamp(trial_ends, timezone.utc).isoformat()
    set_subscription(landlord_id, plan, "trialing", trial_ends_at=trial_ends_iso)
    return {"plan": plan, "status": "trialing", "trial_ends_at": trial_ends_iso}


def subscription_summary(landlord_id: int) -> dict:
    sub = get_subscription(landlord_id)
    if not sub:
        return {"plan": None, "status": "none"}
    return {"plan": sub["plan"], "status": sub["status"],
            "trial_ends_at": sub["trial_ends_at"]}


def sync_subscription_from_stripe(landlord_id: int):
    """Pull the landlord's latest Stripe subscription state straight from the
    API. Used when they return from Checkout so the page reflects reality
    even if the webhook hasn't arrived yet."""
    from store import ensure_stripe_columns
    ensure_stripe_columns()
    sub = get_subscription(landlord_id)
    try:
        customer_id = sub["stripe_customer_id"] if sub else None
    except (KeyError, IndexError, TypeError):
        customer_id = None
    if not customer_id:
        return None
    s = _stripe_lib()
    subs = _as_dict(s.Subscription.list(customer=customer_id, limit=1))
    data = subs.get("data") or []
    if not data:
        return None
    _sync_from_subscription(landlord_id, data[0])
    return subscription_summary(landlord_id)


def _get_or_create_customer(landlord: dict) -> str:
    """Return the Stripe customer ID for this landlord, creating one if needed."""
    sub = get_subscription(landlord["id"])
    if sub and sub["stripe_customer_id"]:
        return sub["stripe_customer_id"]
    s = _stripe_lib()
    customer = s.Customer.create(
        email=landlord["email"],
        name=landlord["name"] or "",
        metadata={"landlord_id": str(landlord["id"])},
    )
    set_stripe_ids(landlord["id"], customer_id=customer.id)
    return customer.id


def create_checkout_session(landlord: dict, plan: str, cycle: str, base_url: str) -> str:
    """Create a Stripe Checkout session for a plan; returns the redirect URL."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan: {plan}")
    if cycle not in CYCLES:
        cycle = "monthly"
    s = _stripe_lib()
    customer_id = _get_or_create_customer(landlord)
    session = s.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id(plan, cycle), "quantity": 1}],
        subscription_data={
            "trial_period_days": TRIAL_DAYS,
            "metadata": {"landlord_id": str(landlord["id"]), "plan": plan},
        },
        success_url=f"{base_url}/billing?checkout=success",
        cancel_url=f"{base_url}/billing?checkout=cancelled",
        allow_promotion_codes=True,
    )
    return session.url


def _as_dict(obj):
    """Stripe API objects aren't real dicts in recent stripe-python versions
    (.get() raises); normalize to a plain dict at the boundary."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return dict(obj)


def _sync_from_subscription(landlord_id: int, sub) -> None:
    """Write a Stripe Subscription object's state into our subscriptions table."""
    sub = _as_dict(sub)
    meta = _as_dict(sub.get("metadata") or {})
    plan = meta.get("plan") or _plan_from_price(sub)
    status = {"trialing": "trialing", "active": "active",
              "past_due": "past_due", "canceled": "canceled",
              "unpaid": "past_due"}.get(sub.get("status"), sub.get("status"))
    trial_ends = None
    if sub.get("trial_end"):
        trial_ends = datetime.fromtimestamp(sub["trial_end"], timezone.utc).isoformat()
    set_subscription(landlord_id, plan or "starter", status, trial_ends_at=trial_ends)
    set_stripe_ids(landlord_id, subscription_id=sub.get("id"))


def _plan_from_price(sub) -> str | None:
    sub = _as_dict(sub)
    try:
        items = _as_dict(sub.get("items") or {})
        data = items.get("data") or []
        price_id_ = _as_dict(data[0]).get("price", {})
        price_id_ = _as_dict(price_id_).get("id")
        s = _stripe_lib()
        price = _as_dict(s.Price.retrieve(price_id_))
        lk = price.get("lookup_key") or ""
        for plan in PLANS:
            if lk == f"rentping_{plan}_monthly" or lk == f"rentping_{plan}_annual":
                return plan
    except Exception:
        pass
    return None


def verify_webhook(payload: bytes, sig_header: str):
    s = _stripe_lib()
    return s.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)


def handle_stripe_event(event) -> dict:
    """Apply a verified Stripe event to our subscription state."""
    event = _as_dict(event)
    etype = event.get("type", "unknown")
    print(f"[stripe webhook] {etype}", flush=True)
    s = _stripe_lib()

    if etype == "checkout.session.completed":
        session = _as_dict((event.get("data") or {}).get("object") or {})
        sess_meta = _as_dict(session.get("metadata") or {})
        landlord_id = int(sess_meta.get("landlord_id") or 0)
        sub_id = session.get("subscription")
        if sub_id:
            sub = _as_dict(s.Subscription.retrieve(sub_id))
            sub_meta = _as_dict(sub.get("metadata") or {})
            lid = int(sub_meta.get("landlord_id") or landlord_id or 0)
            if lid:
                _sync_from_subscription(lid, sub)
        return {"received": True, "type": etype}

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub = _as_dict((event.get("data") or {}).get("object") or {})
        sub_meta = _as_dict(sub.get("metadata") or {})
        lid = int(sub_meta.get("landlord_id") or 0)
        if not lid:
            # fall back: find landlord by customer id
            from store import get_landlord_by_stripe_customer
            landlord = get_landlord_by_stripe_customer(sub.get("customer"))
            lid = landlord["id"] if landlord else 0
        if lid:
            if etype == "customer.subscription.deleted":
                current = get_subscription(lid) or {}
                set_subscription(lid, current["plan"] if current else "starter", "canceled")
                set_stripe_ids(lid, subscription_id=sub.get("id"))
            else:
                _sync_from_subscription(lid, sub)
        return {"received": True, "type": etype}

    return {"received": True, "type": etype, "ignored": True}
