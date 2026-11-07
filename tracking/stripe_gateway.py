"""Stripe card payments taken on our own booking page (Payment Element), no redirect to a hosted checkout.

How it fits together (https://docs.stripe.com/payments/accept-a-payment?platform=web&ui=elements):
* The server creates a PaymentIntent (amount priced on the server, card only) and hands its client_secret to the page.
* The page mounts Stripe's Payment Element (card fields in Stripe-hosted iframes, so card numbers never touch our
  servers: PCI SAQ A) and confirms the payment in place; 3-D Secure opens as a pop-up over our page.
* The server re-reads the PaymentIntent (and the payment_intent.succeeded webhook does the same) before booking.
"""
import json

import stripe
from django.conf import settings

ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF",
                "XOF", "XPF"}


class GatewayError(Exception):
    pass


def enabled() -> bool:
    cfg = settings.STRIPE
    return bool(cfg["SECRET_KEY"] and cfg["PUBLISHABLE_KEY"])


def to_minor(amount: float, currency: str) -> int:
    return int(round(amount)) if currency.upper() in ZERO_DECIMAL else int(round(amount * 100))


def _client() -> stripe.StripeClient:
    if not enabled():
        raise GatewayError("Online card payments aren't set up yet. Please contact us to book.")
    return stripe.StripeClient(settings.STRIPE["SECRET_KEY"], max_network_retries=2)


def _plain(obj) -> dict:
    return json.loads(json.dumps(obj))


def _wrap(fn):
    try:
        return fn()
    except stripe.InvalidRequestError as e:
        raise GatewayError(e.user_message or "The payment couldn't be set up.") from e
    except stripe.StripeError as e:
        raise GatewayError(e.user_message or "The payment provider didn't respond. Please try again.") from e


def create_intent(*, amount: float, currency: str, email: str, description: str, metadata: dict,
                  idempotency_key: str) -> dict:
    """Card-only PaymentIntent. Returns {id, client_secret, status, amount, currency}."""
    params = {
        "amount": to_minor(amount, currency), "currency": currency.lower(),
        "payment_method_types": ["card"], "description": description[:1000], "metadata": metadata,
    }
    suffix = settings.STRIPE["STATEMENT_SUFFIX"]
    if suffix:
        params["statement_descriptor_suffix"] = suffix[:22]
    if settings.STRIPE["SEND_RECEIPTS"]:
        params["receipt_email"] = email
    return _plain(_wrap(lambda: _client().payment_intents.create(params=params,
                                                                 options={"idempotency_key": idempotency_key})))


def retrieve_intent(intent_id: str) -> dict:
    return _plain(_wrap(lambda: _client().payment_intents.retrieve(intent_id, params={"expand": ["latest_charge"]})))


def card_label(intent: dict) -> str:
    charge = intent.get("latest_charge") or {}
    card = ((charge.get("payment_method_details") or {}).get("card") or {}) if isinstance(charge, dict) else {}
    return f"{(card.get('brand') or 'Card').upper()} •••• {card.get('last4') or '----'}"


def paid_by_card(intent: dict) -> bool:
    charge = intent.get("latest_charge")
    if isinstance(charge, dict):
        return (charge.get("payment_method_details") or {}).get("type") == "card"
    return intent.get("payment_method_types") == ["card"]


def failure_message(intent: dict) -> str:
    err = intent.get("last_payment_error") or {}
    return str(err.get("message") or "")[:255]


def parse_webhook(payload: bytes, signature: str) -> dict:
    """Return the webhook event. With TP_STRIPE_WEBHOOK_SECRET set, the Stripe-Signature header is verified (raises
    ValueError / SignatureVerificationError). Without it the event is read unverified, which is safe here because
    it is only used as a trigger: finalize() re-reads the PaymentIntent from Stripe with the secret key."""
    secret = settings.STRIPE["WEBHOOK_SECRET"]
    if secret:
        return _plain(stripe.Webhook.construct_event(payload, signature, secret))
    event = json.loads(payload)
    if not isinstance(event, dict):
        raise ValueError("Not a Stripe event")
    return event
