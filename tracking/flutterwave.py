"""Minimal Flutterwave client: card-only Flutterwave Standard (hosted checkout), server-side verification and
webhook checks.

Docs: https://developer.flutterwave.com/docs/flutterwave-standard-1 , /docs/webhooks ,
      /v3.0.0/docs/transaction-verification-1
* Card details are entered on Flutterwave's page, never on ours (no PCI DSS certificate needed).
* Amounts are in the currency's main unit (dollars, not cents).
"""
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

API = "https://api.flutterwave.com/v3"


class GatewayError(Exception):
    pass


class NotFound(GatewayError):
    pass


def enabled() -> bool:
    return bool(settings.FLUTTERWAVE["SECRET_KEY"])


def charge_for(amount: float) -> tuple[float, str]:
    """Convert a price (site currency) into what the card is charged: (amount, Flutterwave currency)."""
    cfg = settings.FLUTTERWAVE
    if cfg["CURRENCY"] == settings.TRACKPRO["CURRENCY_CODE"].upper():
        return round(amount, 2), cfg["CURRENCY"]
    return round(amount * cfg["FX_RATE"], 2), cfg["CURRENCY"]


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    if not enabled():
        raise GatewayError("Online card payments aren't set up yet. Please contact us to book.")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {settings.FLUTTERWAVE['SECRET_KEY']}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "vdc-express-api"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            body = json.load(res)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        msg = body.get("message") or f"Payment provider error ({e.code})."
        if e.code == 404 or "no transaction" in msg.lower():
            raise NotFound(msg) from e
        raise GatewayError(msg) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise GatewayError("Couldn't reach the payment provider. Please try again.") from e
    if body.get("status") != "success":
        msg = body.get("message") or "Payment provider error."
        if "no transaction" in msg.lower():
            raise NotFound(msg)
        raise GatewayError(msg)
    return body["data"]


def initialize(*, email: str, name: str, phone: str, amount: float, currency: str, reference: str,
               redirect_url: str, title: str, description: str, logo: str, meta: dict) -> dict:
    """Create a card-only hosted payment link. Returns {link}."""
    return _call("POST", "/payments", {
        "tx_ref": reference, "amount": amount, "currency": currency, "redirect_url": redirect_url,
        "payment_options": "card",
        "customer": {"email": email, "name": name, "phonenumber": phone},
        "customizations": {"title": title, "description": description, "logo": logo},
        "meta": meta,
    })


def verify(reference: str) -> dict:
    """Look a transaction up by our tx_ref (raises NotFound if the customer never submitted a card)."""
    return _call("GET", "/transactions/verify_by_reference?" + urllib.parse.urlencode({"tx_ref": reference}))


def card_label(data: dict) -> str:
    card = data.get("card") or {}
    return f"{(card.get('type') or 'Card').strip().upper()} •••• {card.get('last_4digits') or '----'}"


def valid_webhook(signature: str) -> bool:
    """Flutterwave sends the secret hash you set in the dashboard back in the `verif-hash` header."""
    secret = settings.FLUTTERWAVE["SECRET_HASH"]
    return bool(secret and signature) and hmac.compare_digest(secret, signature)
