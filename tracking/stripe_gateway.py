"""Stripe card payments taken on our own booking page (Payment Element), no redirect to a hosted checkout.

How it fits together (https://docs.stripe.com/payments/accept-a-payment?platform=web&ui=elements):
* The server creates a PaymentIntent (amount priced on the server, card only) and hands its client_secret to the page.
* The page mounts Stripe's Payment Element (card fields in Stripe-hosted iframes, so card numbers never touch our
  servers: PCI SAQ A) and confirms the payment in place; 3-D Secure opens as a pop-up over our page.
* The server re-reads the PaymentIntent (and the payment_intent.succeeded webhook does the same) before booking.
"""
import json
import logging

import stripe
from django.conf import settings

log = logging.getLogger(__name__)

ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF",
                "XOF", "XPF"}

# What customers see. Stripe's own messages (which can mention API keys, accounts or request ids) only go to the log.
UNAVAILABLE = "Online card payments are temporarily unavailable. Please try again later or contact us to book."
TRY_AGAIN = "We couldn't reach our payment provider. Please try again in a moment."
FAILED = "We couldn't process this payment. Please try again or contact us."


class GatewayError(Exception):
    """A payment problem with a message that is safe to show customers.
    `unavailable` = our setup is broken (bad keys, wrong account), so retrying won't help."""

    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


def config_problem() -> str:
    """Why the Stripe keys can't work, or '' if they look right. Checked before every call so a mistyped key
    switches card payments off cleanly instead of failing at the customer."""
    cfg = settings.STRIPE
    sk, pk = cfg["SECRET_KEY"], cfg["PUBLISHABLE_KEY"]
    if not sk or not pk:
        return "TP_STRIPE_SECRET_KEY and TP_STRIPE_PUBLISHABLE_KEY are not both set"
    if sk.startswith("pk_"):
        return "TP_STRIPE_SECRET_KEY holds a publishable key (pk_...): use the secret key (sk_...)"
    if not sk.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        return "TP_STRIPE_SECRET_KEY must start with sk_test_ or sk_live_"
    if not pk.startswith(("pk_test_", "pk_live_")):
        return "TP_STRIPE_PUBLISHABLE_KEY must start with pk_test_ or pk_live_"
    if ("_test_" in sk) != ("_test_" in pk):
        return "TP_STRIPE_SECRET_KEY and TP_STRIPE_PUBLISHABLE_KEY are from different modes (one test, one live)"
    return ""


def enabled() -> bool:
    return not config_problem()


def to_minor(amount: float, currency: str) -> int:
    return int(round(amount)) if currency.upper() in ZERO_DECIMAL else int(round(amount * 100))


def _client() -> stripe.StripeClient:
    problem = config_problem()
    if problem:
        log.error("Stripe is not configured: %s", problem)
        raise GatewayError(UNAVAILABLE, unavailable=True)
    return stripe.StripeClient(settings.STRIPE["SECRET_KEY"], max_network_retries=2)


def _plain(obj) -> dict:
    return json.loads(json.dumps(obj))


def _detail(e: stripe.StripeError) -> str:
    return f"{type(e).__name__} (HTTP {e.http_status}, code={e.code}, request={e.request_id}): {e.user_message or e}"


def _wrap(fn, action: str):
    """Run a Stripe call, logging the real reason and raising a GatewayError with a customer-safe message."""
    try:
        return fn()
    except (stripe.AuthenticationError, stripe.PermissionError) as e:
        log.critical("Stripe rejected our API key while trying to %s. Check TP_STRIPE_SECRET_KEY "
                     "(Stripe dashboard -> Developers -> API keys). %s", action, _detail(e))
        raise GatewayError(UNAVAILABLE, unavailable=True) from e
    except stripe.CardError as e:  # card problems are meant for the cardholder
        raise GatewayError(e.user_message or "Your card was declined.") from e
    except (stripe.RateLimitError, stripe.APIConnectionError) as e:
        log.warning("Stripe unreachable while trying to %s: %s", action, _detail(e))
        raise GatewayError(TRY_AGAIN) from e
    except stripe.InvalidRequestError as e:
        log.error("Stripe refused the request to %s: %s", action, _detail(e))
        raise GatewayError(FAILED) from e
    except stripe.StripeError as e:
        log.error("Stripe error while trying to %s: %s", action, _detail(e))
        raise GatewayError(TRY_AGAIN) from e


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
                                                                 options={"idempotency_key": idempotency_key}),
                        "create a payment"))


def retrieve_intent(intent_id: str) -> dict:
    return _plain(_wrap(lambda: _client().payment_intents.retrieve(intent_id, params={"expand": ["latest_charge"]}),
                        f"look up payment {intent_id}"))


def check_connection() -> str:
    """Make one harmless authenticated call; returns a description of the account mode. Raises GatewayError."""
    _wrap(lambda: _client().balance.retrieve(), "check the API key")
    return "test mode" if "_test_" in settings.STRIPE["SECRET_KEY"] else "live mode"


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
