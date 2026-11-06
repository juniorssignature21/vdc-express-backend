"""Minimal Paystack client: card-only checkout, server-side verification and webhook signature checks.

Docs: https://paystack.com/docs/api/transaction/  (amounts are in the currency's subunit, e.g. cents for USD)
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.request

from django.conf import settings

API = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.PAYSTACK["SECRET_KEY"])


def to_subunit(amount: float) -> int:
    return int(round(amount * 100))


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    if not enabled():
        raise PaystackError("Online card payments aren't set up yet. Please contact us to book.")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {settings.PAYSTACK['SECRET_KEY']}", "Content-Type": "application/json",
                 "User-Agent": "vdc-express-api"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            body = json.load(res)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        raise PaystackError(body.get("message") or f"Payment provider error ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise PaystackError("Couldn't reach the payment provider. Please try again.") from e
    if not body.get("status"):
        raise PaystackError(body.get("message") or "Payment provider error.")
    return body["data"]


def initialize(*, email: str, amount: float, currency: str, reference: str, callback_url: str, metadata: dict) -> dict:
    """Start a card-only checkout. Returns {authorization_url, access_code, reference}."""
    return _call("POST", "/transaction/initialize", {
        "email": email, "amount": to_subunit(amount), "currency": currency, "reference": reference,
        "callback_url": callback_url, "channels": ["card"], "metadata": metadata,
    })


def verify(reference: str) -> dict:
    return _call("GET", f"/transaction/verify/{urllib.request.quote(reference)}")


def valid_signature(raw_body: bytes, signature: str) -> bool:
    if not enabled() or not signature:
        return False
    expected = hmac.new(settings.PAYSTACK["SECRET_KEY"].encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)
