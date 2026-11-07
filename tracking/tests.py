import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tracking.management.commands.seed import ensure_admin, seed_demo
from tracking.pricing import QuoteError, ShipmentSpec, calc_quote, option_for, round_weight
from tracking.utils import is_valid_waybill

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _setup(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "uploads"
    # never write test files to a real bucket configured in .env
    settings.STORAGES = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
    settings.TRACKPRO = {**settings.TRACKPRO, "ADMIN_EMAIL": "admin@trackpro.local", "ADMIN_PASSWORD": "admin123",
                         "FUEL_SURCHARGE": 0.18, "VAT_RATE": 0, "CURRENCY_CODE": "USD", "CURRENCY_SYMBOL": "$"}
    settings.STRIPE = {"SECRET_KEY": "sk_test_dummy", "PUBLISHABLE_KEY": "pk_test_dummy", "WEBHOOK_SECRET": "whsec_test",
                       "CURRENCY": "USD", "STATEMENT_SUFFIX": "", "SEND_RECEIPTS": False}
    cache.clear()  # reset throttling counters
    ensure_admin()
    seed_demo()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def admin(client):
    r = client.post("/api/admin/login", {"email": "admin@trackpro.local", "password": "admin123"}, format="json")
    assert r.status_code == 200, r.content
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['access_token']}")
    return c


def _opt(q, product):
    return option_for(q, product)


def test_chargeable_weight_rules():
    assert [round_weight(w) for w in (0, 0.2, 0.51, 1.0, 29.9, 30.2, 31.0)] == [0.5, 0.5, 1.0, 1.0, 30.0, 31.0, 31.0]
    # 40x30x18 / 5000 = 4.32 kg volumetric beats 2.4 kg actual, rounded up to 4.5 kg
    q = calc_quote(ShipmentSpec("US", "US", 1, 2.4, 40, 30, 18))
    assert q["volumetric_kg"] == 4.32 and q["chargeable_kg"] == 4.5 and q["route"]["direction"] == "domestic"
    o = _opt(q, "Express")
    assert o["lines"][0]["amount"] == round(22 + 8 * 2.40, 2) and o["tax"] == 0  # first 0.5 kg + 8 extra half-kilos
    assert o["lines"][1]["code"] == "fuel" and o["lines"][1]["amount"] == round(41.2 * 0.18, 2)


def test_products_routes_and_surcharges():
    q = calc_quote(ShipmentSpec("US", "GB", 2, 5, 40, 30, 30, insured_value=2000, signature=True, duties_paid=True))
    assert q["route"]["direction"] == "export" and q["route"]["zone"] == 2 and q["route"]["international"]
    by = {o["product"]: o for o in q["options"]}
    assert by["Same Day"]["available"] is False
    assert by["Express 9:00"]["total"] > by["Express 12:00"]["total"] > by["Express"]["total"] > by["Economy Select"]["total"]
    codes = [ln["code"] for ln in by["Express"]["lines"]]
    assert codes == ["transport", "fuel", "ddp", "signature", "insurance"]
    assert by["Express"]["lines"][-1]["amount"] == 20.0  # 1% of $2,000
    # time-definite not offered to Africa; elevated-risk surcharge applies to Yemen
    ng = {o["product"]: o for o in calc_quote(ShipmentSpec("US", "NG"))["options"]}
    assert ng["Express 9:00"]["available"] is False and ng["Express"]["available"]
    ye = _opt(calc_quote(ShipmentSpec("US", "YE")), "Express")
    assert any(ln["code"] == "elevated_risk" for ln in ye["lines"])
    # import and third-country lanes cost more than export
    exp = _opt(calc_quote(ShipmentSpec("US", "DE")), "Express")["total"]
    imp = _opt(calc_quote(ShipmentSpec("DE", "US")), "Express")["total"]
    third = _opt(calc_quote(ShipmentSpec("DE", "GB")), "Express")["total"]
    assert exp < imp < third
    # oversize + overweight pieces
    big = _opt(calc_quote(ShipmentSpec("US", "US", 1, 80, 120, 50, 50)), "Express")
    assert {"oversize", "overweight"} <= {ln["code"] for ln in big["lines"]}


def test_quote_errors():
    for spec, msg in [(ShipmentSpec("US", "CU"), "Cuba"), (ShipmentSpec("GB", "GB"), "available online"),
                      (ShipmentSpec("US", "US", 1, 200), "150 kg"),
                      (ShipmentSpec("US", "FR", 1, 3, is_document=True), "Document")]:
        with pytest.raises(QuoteError, match=msg):
            calc_quote(spec)


def test_delivery_dates_skip_weekends_and_cutoff():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fri_evening = datetime(2026, 11, 6, 18, 0, tzinfo=ZoneInfo("America/Chicago"))
    o = _opt(calc_quote(ShipmentSpec("US", "US"), now=fri_evening), "Express")
    assert o["ship_date"] == "2026-11-09" and o["delivery_date"] == "2026-11-10"  # Mon pickup, Tue delivery
    sd = _opt(calc_quote(ShipmentSpec("US", "US"), now=fri_evening.replace(hour=11)), "Same Day")
    assert sd["ship_date"] == sd["delivery_date"] == "2026-11-06"


def test_public_tracking_and_receipts(client):
    r = client.get("/api/track/4829 1375 63")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_transit" and body["receiver_name"] == "Daniel Brooks"
    assert body["events"][0]["occurred_at"] >= body["events"][-1]["occurred_at"]  # newest first
    assert "receiver_phone" not in body and "sender_email" not in body  # no private contact details
    assert body["courier_contact"] is True and body["delivery_by"] == "end of day"
    assert body["updated_at"].endswith("Z")

    nf = client.get("/api/track/NOPE")
    assert nf.status_code == 404 and "couldn't find" in nf.json()["detail"]

    rec = client.get("/api/receipts", {"id": "5710042181"}).json()[0]
    assert rec["payment"]["stamp"] == "PAID" and rec["payment"]["balance"] == 0 and rec["incoterm"] == "DAP"
    assert client.get("/api/receipts", {"r": "rcp-2611-306558"}).json()[0]["tracking_number"] == "3065581201"
    bad = client.get("/api/receipts")
    assert bad.status_code == 400 and bad.json()["detail"] == "Enter a tracking number or a receipt number."


def test_settings(client):
    s = client.get("/api/settings").json()
    assert s["currency_code"] == "USD" and s["statuses"]["at_hub"] == "Processed at facility"
    assert [p["name"] for p in s["products"]][:2] == ["Same Day", "Express 9:00"] and s["payments_enabled"]
    assert {"code": "NG", "name": "Nigeria"} in s["countries"] and not any(c["code"] == "CU" for c in s["countries"])


def test_quote_and_contact_flow(client, admin):
    calc = client.post("/api/quote/calculate", {"origin_country": "US", "dest_country": "CA", "weight_kg": 3},
                       format="json").json()
    express = next(o for o in calc["options"] if o["product"] == "Express")
    assert calc["route"]["zone"] == 1 and express["total"] == round(45 + 5 * 6.5, 2) + round((45 + 5 * 6.5) * 0.18, 2)
    bad = client.post("/api/quote/calculate", {"origin_country": "US", "dest_country": "IR"}, format="json")
    assert bad.status_code == 400 and "Iran" in bad.json()["detail"]

    r = client.post("/api/quotes", {
        "origin": "Houston, TX", "destination": "Toronto", "origin_country": "US", "dest_country": "CA",
        "service_type": "Express", "weight_kg": 3, "name": "Test User", "email": "test@example.com",
    }, format="json")
    assert r.status_code == 201 and r.json()["reference"].startswith("Q-") and r.json()["total"] == express["total"]
    assert r.json()["international"] is True and r.json()["breakdown"][0]["code"] == "transport"
    invalid = client.post("/api/quotes", {"origin": "A"}, format="json")
    assert invalid.status_code == 400 and "origin" in invalid.json()["detail"]

    assert client.post("/api/contact", {"name": "Bob", "email": "bad", "message": "hello"}, format="json").status_code == 400
    assert client.post("/api/contact", {"name": "Bob", "email": "b@example.com", "message": "hello there"},
                       format="json").status_code == 201

    stats = admin.get("/api/admin/stats").json()
    assert stats["new_quotes"] == 1 and stats["unread_messages"] == 1 and stats["shipments"] == 3

    quote_id = admin.get("/api/admin/quotes").json()[0]["id"]
    sh = admin.post(f"/api/admin/quotes/{quote_id}/convert").json()
    assert sh["status"] == "pending" and sh["payments"][0]["stamp"] == "UNPAID" and is_valid_waybill(sh["tracking_number"])
    assert sh["payments"][0]["total"] == express["total"] and sh["dest_country"] == "CA"
    assert admin.get("/api/admin/quotes", {"status": "booked"}).json()[0]["id"] == quote_id

    msg = admin.get("/api/admin/messages").json()[0]
    assert admin.patch(f"/api/admin/messages/{msg['id']}?is_read=true").json()["is_read"] is True


def test_admin_requires_auth(client):
    assert client.get("/api/admin/shipments").status_code == 401
    client.credentials(HTTP_AUTHORIZATION="Bearer junk")
    assert client.get("/api/admin/shipments").status_code == 401
    anon = APIClient()
    r = anon.post("/api/admin/login", {"email": "admin@trackpro.local", "password": "x"}, format="json")
    assert r.status_code == 401 and r.json()["detail"] == "Incorrect email or password."


@pytest.mark.django_db(transaction=True)  # so on_commit file cleanup actually runs
def test_admin_shipment_lifecycle(client, admin):
    lst = admin.get("/api/admin/shipments", {"q": "brooks"}).json()
    assert lst["total"] == 1 and lst["items"][0]["tracking_number"] == "4829137563"

    r = admin.post("/api/admin/shipments", {
        "service_type": "Same Day", "origin": "Leeds", "origin_lat": 53.8, "origin_lng": -1.55,
        "destination": "York", "dest_lat": 53.96, "dest_lng": -1.08, "receiver_name": "Jo",
        "courier_name": "Sam", "courier_phone": "+44 1",
    }, format="json")
    assert r.status_code == 201, r.content
    sh = r.json()
    sid, tn = sh["id"], sh["tracking_number"]
    assert is_valid_waybill(tn) and len(sh["events"]) == 1 and sh["current_lat"] == 53.8

    sh = admin.post(f"/api/admin/shipments/{sid}/events", {
        "status": "out_for_delivery", "location": "A64", "lat": 53.9, "lng": -1.3,
    }, format="json").json()
    assert sh["status"] == "out_for_delivery" and sh["current_location"] == "A64" and len(sh["events"]) == 2

    # back-dated event must not move the live position
    sh = admin.post(f"/api/admin/shipments/{sid}/events", {
        "status": "picked_up", "location": "Leeds depot", "occurred_at": "2020-01-01T10:00:00Z",
    }, format="json").json()
    assert sh["status"] == "out_for_delivery" and len(sh["events"]) == 3

    ev_id = sh["events"][0]["id"]  # admin view lists events oldest first: the 2020 one
    sh = admin.delete(f"/api/admin/shipments/{sid}/events/{ev_id}").json()
    assert len(sh["events"]) == 2

    sh = admin.post(f"/api/admin/shipments/{sid}/payments", {
        "stamp": "PAID", "items": [{"label": "Same day", "amount": 20}], "vat_rate": 0.2,
    }, format="json").json()
    pay = sh["payments"][0]
    assert pay["total"] == 24 and pay["balance"] == 0 and pay["receipt_number"].startswith("RCP-")

    sh = admin.post(f"/api/admin/shipments/{sid}/photos", {"files": [_png()], "caption": "At pickup"},
                    format="multipart").json()
    photo = sh["photos"][0]
    img = client.get(photo["url"])
    assert photo["url"].startswith("/uploads/") and img.status_code == 200
    img.close()
    bad = admin.post(f"/api/admin/shipments/{sid}/photos", {"files": [_txt()]}, format="multipart")
    assert bad.status_code == 415

    pub = client.get(f"/api/track/{tn}").json()
    assert pub["courier_contact"] is True and pub["courier_phone"] == "+44 1" and len(pub["photos"]) == 1

    r = admin.patch(f"/api/admin/shipments/{sid}", {"notice": "Hello", "tracking_number": "HACK"}, format="json")
    assert r.json()["notice"] == "Hello" and r.json()["receiver_name"] == "Jo" and r.json()["tracking_number"] == tn

    dup = admin.post("/api/admin/shipments", {"tracking_number": tn.lower()}, format="json")
    assert dup.status_code == 400 and "already exists" in dup.json()["detail"]

    assert admin.delete(f"/api/admin/shipments/{sid}").status_code == 204
    assert client.get(f"/api/track/{tn}").status_code == 404
    assert client.get(photo["url"]).status_code == 404  # file removed with the shipment


def test_password_change(admin):
    r = admin.post("/api/admin/password", {"current_password": "wrong", "new_password": "a-much-better-pass"}, format="json")
    assert r.status_code == 400
    r = admin.post("/api/admin/password", {"current_password": "admin123", "new_password": "a-much-better-pass"}, format="json")
    assert r.status_code == 200
    anon = APIClient()
    assert anon.post("/api/admin/login", {"email": "admin@trackpro.local", "password": "a-much-better-pass"},
                     format="json").status_code == 200


def _png():
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("p.png", PNG, content_type="image/png")


def _txt():
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("x.txt", b"hi", content_type="text/plain")


# ------------------------------------------------------------------ online booking + Stripe (mocked)
BOOKING = {
    "origin_country": "US", "dest_country": "GB", "pieces": 1, "weight_kg": 2, "length_cm": 30, "width_cm": 20,
    "height_cm": 10, "product": "Express", "contents": "Cotton T-shirts", "customs_value": 120, "accept_terms": True,
    "shipper": {"name": "Ada Lovelace", "email": "ada@example.com", "phone": "+1 713 555 0100",
                "address1": "1 Main St", "city": "Houston", "state": "TX", "postal_code": "77002"},
    "receiver": {"name": "Alan Turing", "email": "alan@example.com", "phone": "+44 20 7946 0000",
                 "address1": "10 Kings Parade", "city": "Cambridge", "postal_code": "CB2 1SJ"},
}


@pytest.fixture
def gateway(monkeypatch):
    """In-memory stand-in for Stripe PaymentIntents."""
    intents, created = {}, []

    def create_intent(*, amount, currency, email, description, metadata, idempotency_key):
        pid = f"pi_{len(intents) + 1}"
        intents[pid] = {"id": pid, "client_secret": f"{pid}_secret_x", "status": "requires_payment_method",
                        "amount": round(amount * 100), "amount_received": 0, "currency": currency.lower(),
                        "metadata": dict(metadata), "payment_method_types": ["card"], "latest_charge": None,
                        "last_payment_error": None}
        created.append({"amount": amount, "currency": currency, "description": description,
                        "idempotency_key": idempotency_key, "id": pid})
        return dict(intents[pid])

    def retrieve_intent(pid):
        return dict(intents[pid])

    monkeypatch.setattr("tracking.stripe_gateway.create_intent", create_intent)
    monkeypatch.setattr("tracking.stripe_gateway.retrieve_intent", retrieve_intent)
    return {"intents": intents, "created": created}


def _succeed(intent, **over):
    intent.update({"status": "succeeded", "amount_received": intent["amount"], "last_payment_error": None,
                   "latest_charge": {"payment_method_details": {"type": "card",
                                                                "card": {"brand": "visa", "last4": "4242"}}},
                   **over})


def _decline(intent, message="Your card was declined."):
    intent.update(status="requires_payment_method", last_payment_error={"message": message})


def test_booking_card_payment_flow(client, admin, gateway):
    assert client.post("/api/bookings", {**BOOKING, "customs_value": 0}, format="json").status_code == 400
    assert client.post("/api/bookings", {**BOOKING, "accept_terms": False}, format="json").status_code == 400

    r = client.post("/api/bookings", BOOKING, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    made = gateway["created"][0]
    assert body["client_secret"] == "pi_1_secret_x" and body["publishable_key"] == "pk_test_dummy"
    assert made["currency"] == "USD" and "VDC Express Logistics" in made["description"]
    quote = client.post("/api/quote/calculate", BOOKING, format="json").json()
    express = next(o for o in quote["options"] if o["product"] == "Express")
    assert made["amount"] == express["total"] == body["amount"]  # priced on the server
    assert client.get("/api/settings").json()["stripe_publishable_key"] == "pk_test_dummy"

    # not paid yet: stays open
    v = client.get("/api/bookings/verify", {"reference": body["reference"]}).json()
    assert v["status"] == "awaiting_payment"

    # declined card: failed, then the same PaymentIntent is reused for another card
    _decline(gateway["intents"]["pi_1"])
    v = client.get("/api/bookings/verify", {"reference": body["reference"]}).json()
    assert v["status"] == "failed" and v["tracking_number"] is None and v["gateway_message"] == "Your card was declined."
    retry = client.post(f"/api/bookings/{body['reference']}/pay").json()
    assert retry["client_secret"] == "pi_1_secret_x" and len(gateway["created"]) == 1

    _succeed(gateway["intents"]["pi_1"])
    v = client.get("/api/bookings/verify", {"payment_intent": "pi_1"}).json()  # 3-D Secure redirect return
    assert v["status"] == "paid" and v["card"] == "VISA •••• 4242" and is_valid_waybill(v["tracking_number"])
    again = client.get("/api/bookings/verify", {"reference": body["reference"]}).json()  # idempotent
    assert again["tracking_number"] == v["tracking_number"]
    assert client.post(f"/api/bookings/{body['reference']}/pay").json()["status"] == "paid"

    track = client.get(f"/api/track/{v['tracking_number']}").json()
    assert track["service_type"] == "Express" and track["dest_country"] == "GB" and track["status"] == "pending"
    rec = client.get("/api/receipts", {"r": v["receipt_number"]}).json()[0]
    assert rec["payment"]["stamp"] == "PAID" and rec["payment"]["method"] == "Card"
    assert rec["payment"]["total"] == express["total"] and rec["receiver_address"].endswith("United Kingdom")

    assert admin.get("/api/admin/bookings").json()[0]["status"] == "paid"
    sh = admin.get("/api/admin/shipments", {"q": v["tracking_number"]}).json()["items"][0]
    assert sh["source"] == "online"
    assert admin.get(f"/api/admin/shipments/{sh['id']}/quote").json()["total"] == express["total"]


def test_canceled_intent_gets_a_fresh_one(client, gateway):
    body = client.post("/api/bookings", BOOKING, format="json").json()
    gateway["intents"]["pi_1"]["status"] = "canceled"
    assert client.get("/api/bookings/verify", {"reference": body["reference"]}).json()["status"] == "failed"
    retry = client.post(f"/api/bookings/{body['reference']}/pay").json()
    assert retry["client_secret"] == "pi_2_secret_x"
    assert gateway["created"][1]["idempotency_key"].endswith("-2")
    _succeed(gateway["intents"]["pi_2"])
    assert client.get("/api/bookings/verify", {"reference": body["reference"]}).json()["status"] == "paid"


def test_stripe_intent_is_card_only(monkeypatch, settings):
    from tracking import stripe_gateway
    sent = {}

    class FakeIntents:
        def create(self, params, options):
            sent.update(params=params, options=options)
            return {"id": "pi_x", "client_secret": "s"}

    class FakeClient:
        payment_intents = FakeIntents()

    monkeypatch.setattr(stripe_gateway, "_client", lambda: FakeClient())
    stripe_gateway.create_intent(amount=98.53, currency="USD", email="a@b.c", description="d",
                                 metadata={"booking": "BK"}, idempotency_key="k")
    assert sent["params"]["payment_method_types"] == ["card"] and sent["params"]["amount"] == 9853
    assert sent["params"]["currency"] == "usd" and "receipt_email" not in sent["params"]
    assert sent["options"] == {"idempotency_key": "k"}


def test_booking_rejects_wrong_payments_and_bad_webhooks(client, gateway):
    body = client.post("/api/bookings", BOOKING, format="json").json()
    _succeed(gateway["intents"]["pi_1"], amount_received=100)  # paid $1
    v = client.get("/api/bookings/verify", {"reference": body["reference"]}).json()
    assert v["status"] == "failed" and v["tracking_number"] is None and "Mismatch" in v["gateway_message"]

    body_b = client.post("/api/bookings", BOOKING, format="json").json()
    _succeed(gateway["intents"]["pi_2"], metadata={"booking": "BK-OTHER"})  # someone else's payment
    assert client.get("/api/bookings/verify", {"reference": body_b["reference"]}).json()["status"] == "failed"

    import hashlib
    import hmac
    import json as _json
    import time
    body2 = client.post("/api/bookings", BOOKING, format="json").json()
    _succeed(gateway["intents"]["pi_3"])
    payload = _json.dumps({"id": "evt_1", "object": "event", "type": "payment_intent.succeeded",
                           "data": {"object": {"id": "pi_3", "object": "payment_intent"}}})
    assert client.post("/api/stripe/webhook", payload, content_type="application/json",
                       HTTP_STRIPE_SIGNATURE="t=1,v1=forged").status_code == 400
    t = int(time.time())
    sig = hmac.new(b"whsec_test", f"{t}.{payload}".encode(), hashlib.sha256).hexdigest()
    assert client.post("/api/stripe/webhook", payload, content_type="application/json",
                       HTTP_STRIPE_SIGNATURE=f"t={t},v1={sig}").status_code == 200
    from tracking.models import Booking
    assert Booking.objects.get(reference=body2["reference"]).status == "paid"  # booked by the webhook alone


def test_booking_needs_payments_configured(client, settings):
    settings.STRIPE = {**settings.STRIPE, "SECRET_KEY": ""}
    r = client.post("/api/bookings", BOOKING, format="json")
    assert r.status_code == 503 and "contact us" in r.json()["detail"]
