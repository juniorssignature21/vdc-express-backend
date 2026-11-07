"""Quotes and online booking with card-only payment taken on our own page (Stripe Payment Element).

Flow: POST /api/bookings prices the shipment on the server and creates a card-only Stripe PaymentIntent; the booking
page shows Stripe's card fields in place and confirms the payment there (3-D Secure opens as a pop-up). Then
GET /api/bookings/verify re-reads the PaymentIntent from Stripe and only then creates the shipment + PAID receipt.
The webhook (POST /api/stripe/webhook) runs the same idempotent finalisation, so a closed browser tab still books.
"""
import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import stripe_gateway as gateway
from .countries import country_name
from .models import Booking, Payment, Shipment
from .pricing import QuoteError, calc_quote, option_for
from .serializers import BookingAdminSerializer, BookingPublicSerializer, BookingSerializer, QuoteCalcSerializer
from .utils import new_booking_reference, new_receipt_number, new_tracking_number

log = logging.getLogger(__name__)


class PaymentUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Online card payments aren't available right now."


class GatewayError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The payment provider didn't respond. Please try again."


def api_error(e: "gateway.GatewayError") -> APIException:
    """Customer-facing API error for a payment problem (the details are already in the log)."""
    return PaymentUnavailable(str(e)) if e.unavailable else GatewayError(str(e))


def price(serializer) -> dict:
    try:
        return calc_quote(serializer.spec())
    except QuoteError as e:
        raise ValidationError(str(e))


@api_view(["POST"])
@permission_classes([AllowAny])
def quote_calculate_view(request):
    s = QuoteCalcSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    return Response(price(s))


# ------------------------------------------------------------------ booking
def place_label(city, state, country):
    parts = [city, state] if country == settings.TRACKPRO["DOMESTIC_COUNTRY"] else [city, country_name(country)]
    return ", ".join(p for p in parts if p)


def full_address(party, country):
    line = ", ".join(p for p in (party["address1"], party.get("address2"), party["city"],
                                 " ".join(x for x in (party.get("state"), party.get("postal_code")) if x)) if p)
    return f"{line}, {country_name(country)}"


def _new_intent(ref: str, attempt: int, amount: float, email: str, product: str) -> dict:
    currency = settings.STRIPE["CURRENCY"]
    try:
        return gateway.create_intent(
            amount=amount, currency=currency, email=email,
            description=f"{settings.TRACKPRO['COMPANY_NAME']} · {product} shipment · booking {ref}",
            metadata={"booking": ref, "product": product}, idempotency_key=f"booking-{ref}-{attempt}",
        )
    except gateway.GatewayError as e:
        raise api_error(e)


def _payment_payload(b: Booking, intent: dict) -> dict:
    return {"reference": b.reference, "client_secret": intent["client_secret"], "amount": b.amount,
            "currency": b.currency, "publishable_key": settings.STRIPE["PUBLISHABLE_KEY"]}


class BookingCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_form"

    def post(self, request):
        problem = gateway.config_problem()
        if problem:
            log.error("Online booking refused, Stripe is not configured: %s", problem)
            raise PaymentUnavailable("Online card payments aren't set up yet. Please contact us to book.")
        s = BookingSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        quote = price(s)
        try:
            option = option_for(quote, d["product"])
        except QuoteError as e:
            raise ValidationError(str(e))
        details = {k: v for k, v in d.items() if k != "accept_terms"}
        details["ship_date"] = option["ship_date"]
        ref = new_booking_reference()
        intent = _new_intent(ref, 1, option["total"], d["shipper"]["email"], d["product"])
        b = Booking.objects.create(
            reference=ref, pay_reference=intent["id"], email=d["shipper"]["email"], product=d["product"],
            amount=option["total"], currency=settings.TRACKPRO["CURRENCY_CODE"],
            charge_amount=option["total"], charge_currency=settings.STRIPE["CURRENCY"],
            quote={k: v for k, v in quote.items() if k != "options"} | {"option": option},
            details=json.loads(json.dumps(details, default=str)),
        )
        return Response(_payment_payload(b, intent), status=status.HTTP_201_CREATED)


REUSABLE = ("requires_payment_method", "requires_confirmation", "requires_action")


class BookingRetryView(APIView):
    """Hand the page a client_secret to pay (again) for an unpaid booking: the same PaymentIntent while Stripe
    still accepts a new card for it, otherwise a fresh one."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_form"

    def post(self, request, reference):
        b = get_object_or_404(Booking, reference__iexact=reference)
        if b.status == "paid":
            return Response(BookingPublicSerializer(b).data)
        try:
            intent = gateway.retrieve_intent(b.pay_reference)
        except gateway.GatewayError as e:
            raise api_error(e)
        if intent.get("status") == "succeeded":
            finalize(b.pay_reference)
            b.refresh_from_db()
            if b.status == "paid":
                return Response(BookingPublicSerializer(b).data)
        if intent.get("status") not in REUSABLE:
            b.attempts += 1
            intent = _new_intent(b.reference, b.attempts, b.amount, b.email, b.product)
            b.pay_reference = intent["id"]
        b.status = "awaiting_payment"
        b.save(update_fields=["attempts", "pay_reference", "status"])
        return Response(_payment_payload(b, intent))


@api_view(["GET"])
@permission_classes([AllowAny])
def booking_verify_view(request):
    q = request.query_params
    ref = (q.get("reference") or q.get("payment_intent") or "").strip()
    if not ref:
        raise ValidationError("Missing payment reference.")
    b = Booking.objects.filter(reference__iexact=ref).first() or Booking.objects.filter(pay_reference=ref).first()
    if b is None:
        raise NotFound("We couldn't find that booking.")
    if b.status != "paid":
        try:
            finalize(b.pay_reference)
        except gateway.GatewayError as e:
            raise api_error(e)
        b.refresh_from_db()
    return Response(BookingPublicSerializer(b).data)


@csrf_exempt
def stripe_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        event = gateway.parse_webhook(request.body, request.headers.get("Stripe-Signature", ""))
    except Exception:  # bad signature, bad JSON or no secret configured
        return HttpResponse(status=400)
    if event.get("type") in ("payment_intent.succeeded", "payment_intent.payment_failed", "payment_intent.canceled"):
        intent_id = ((event.get("data") or {}).get("object") or {}).get("id", "")
        if not isinstance(intent_id, str) or not intent_id.startswith("pi_"):
            return HttpResponse(status=400)
        try:  # finalize() only calls Stripe for an unpaid booking that owns this PaymentIntent
            finalize(intent_id)  # always re-reads the PaymentIntent; the event body is only a trigger
        except gateway.GatewayError:  # reason already logged by the gateway
            return HttpResponse(status=502)  # Stripe retries
    return HttpResponse(status=200)


def finalize(intent_id: str):
    """Re-read the PaymentIntent from Stripe and, once, turn the booking into a paid shipment. Idempotent; a booking
    marked failed is still completed if the customer later pays the same PaymentIntent with another card."""
    if not intent_id or not Booking.objects.filter(pay_reference=intent_id).exclude(status="paid").exists():
        return
    intent = gateway.retrieve_intent(intent_id)  # network call outside the DB transaction
    with transaction.atomic():
        b = Booking.objects.select_for_update().filter(pay_reference=intent_id).first()
        if b is None or b.status == "paid":
            return
        state = intent.get("status")
        charge_amount = b.charge_amount if b.charge_amount is not None else b.amount
        charge_currency = (b.charge_currency or b.currency).upper()
        matches = (intent.get("amount_received") == gateway.to_minor(charge_amount, charge_currency)
                   and (intent.get("currency") or "").upper() == charge_currency
                   and (intent.get("metadata") or {}).get("booking") == b.reference
                   and gateway.paid_by_card(intent))
        if state != "succeeded" or not matches:
            if state == "succeeded":  # money taken, but not what we asked for: flag for staff
                b.status = "failed"
                b.gateway_message = (f"Mismatch: {intent.get('amount_received')} {intent.get('currency')} "
                                     f"for booking {(intent.get('metadata') or {}).get('booking')}")
                log.error("Booking %s: %s", b.reference, b.gateway_message)
            elif state == "canceled" or (state == "requires_payment_method" and intent.get("last_payment_error")):
                b.status = "failed"
                b.gateway_message = gateway.failure_message(intent) or "The card payment was declined."
            else:  # processing / requires_action: still waiting for the bank
                return
            b.save(update_fields=["status", "gateway_message"])
            return
        b.card = gateway.card_label(intent)
        b.gateway_message = "Approved"
        b.shipment = _create_shipment(b)
        b.status, b.paid_at = "paid", timezone.now()
        b.save()


def _create_shipment(b: Booking) -> Shipment:
    d, q = b.details, b.quote
    o = q["option"]
    oc, dc = d["origin_country"], d["dest_country"]
    shipper, receiver = d["shipper"], d["receiver"]
    pieces = int(d.get("pieces") or 1)
    now = timezone.now()
    sh = Shipment.objects.create(
        tracking_number=new_tracking_number(), status="pending", service_type=b.product,
        international=oc != dc, origin_country=oc, dest_country=dc,
        is_document=q.get("is_document", False), incoterm="DDP" if d.get("duties_paid") else "DAP",
        signature_required=bool(d.get("signature")), insured_value=d.get("insured_value") or 0,
        customer_reference=d.get("customer_reference", ""), source="online",
        sender_name=shipper["name"] + (f" ({shipper['company']})" if shipper.get("company") else ""),
        sender_phone=shipper["phone"], sender_email=shipper["email"], sender_address=full_address(shipper, oc),
        receiver_name=receiver["name"] + (f" ({receiver['company']})" if receiver.get("company") else ""),
        receiver_phone=receiver["phone"], receiver_email=receiver["email"], receiver_address=full_address(receiver, dc),
        origin=place_label(shipper["city"], shipper.get("state"), oc),
        destination=place_label(receiver["city"], receiver.get("state"), dc),
        current_location=place_label(shipper["city"], shipper.get("state"), oc),
        contents=d["contents"], weight_kg=round(float(d.get("weight_kg") or 0) * pieces, 2),
        length_cm=d.get("length_cm") or 0, width_cm=d.get("width_cm") or 0, height_cm=d.get("height_cm") or 0,
        pieces=pieces, declared_value=d.get("customs_value") or 0, chargeable_kg=q["chargeable_kg"],
        eta=o["delivery_date"], shipped_at=o["ship_date"],
        vehicle="plane" if oc != dc else "van",
    )
    sh.events.create(status="pending", title="Shipment information received", location=sh.origin,
                     note=f"Booked online ({b.reference}). Pickup scheduled for {o['ship_date']}.", occurred_at=now)
    Payment.objects.create(
        shipment=sh, receipt_number=new_receipt_number(), method="Card", reference=b.card, stamp="PAID",
        items=[{"label": ln["label"], "amount": ln["amount"]} for ln in o["lines"]],
        vat_rate=q.get("tax_rate", 0), payer_name=shipper["name"], paid_at=now,
        note=f"Online booking {b.reference} · Stripe {b.pay_reference}" + (
            f" · card charged {b.charge_amount:,.2f} {b.charge_currency}"
            if b.charge_currency and b.charge_currency != b.currency else ""),
    )
    return sh


class BookingAdminViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = BookingAdminSerializer

    def get_queryset(self):
        qs = Booking.objects.select_related("shipment")
        st = self.request.query_params.get("status", "").strip()
        return qs.filter(status=st) if st else qs
