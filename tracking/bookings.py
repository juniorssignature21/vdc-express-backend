"""Quotes and online booking with card-only payment (Paystack).

Flow: POST /api/bookings (priced on the server) -> Paystack hosted card checkout -> customer returns to
/book/confirm -> GET /api/bookings/verify re-checks the transaction with Paystack -> shipment + PAID receipt.
The webhook (POST /api/paystack/webhook) runs the same idempotent finalisation, so a closed browser tab still books.
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

from . import paystack
from .countries import country_name
from .models import Booking, Payment, Shipment
from .pricing import QuoteError, calc_quote, option_for
from .serializers import BookingAdminSerializer, BookingPublicSerializer, BookingSerializer, QuoteCalcSerializer
from .utils import new_booking_reference, new_pay_reference, new_receipt_number, new_tracking_number

log = logging.getLogger(__name__)


class PaymentUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Online card payments aren't available right now."


class GatewayError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The payment provider didn't respond. Please try again."


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


def _start_checkout(b: Booking) -> dict:
    try:
        data = paystack.initialize(
            email=b.email, amount=b.amount, currency=b.currency, reference=b.pay_reference,
            callback_url=f"{settings.FRONTEND_URL}/book/confirm",
            metadata={"booking": b.reference, "product": b.product,
                      "custom_fields": [{"display_name": "Booking", "variable_name": "booking", "value": b.reference}]},
        )
    except paystack.PaystackError as e:
        b.gateway_message = str(e)[:255]
        b.save(update_fields=["gateway_message"])
        raise GatewayError(str(e))
    return {"reference": b.reference, "authorization_url": data["authorization_url"]}


class BookingCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_form"

    def post(self, request):
        if not paystack.enabled():
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
        b = Booking.objects.create(
            reference=ref, pay_reference=new_pay_reference(ref, 1), email=d["shipper"]["email"], product=d["product"],
            amount=option["total"], currency=settings.TRACKPRO["CURRENCY_CODE"],
            quote={k: v for k, v in quote.items() if k != "options"} | {"option": option},
            details=json.loads(json.dumps(details, default=str)),
        )
        return Response(_start_checkout(b), status=status.HTTP_201_CREATED)


class BookingRetryView(APIView):
    """Start a fresh card checkout for a booking that wasn't paid (Paystack references are single-use)."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_form"

    def post(self, request, reference):
        b = get_object_or_404(Booking, reference__iexact=reference)
        if b.status == "paid":
            return Response(BookingPublicSerializer(b).data)
        b.attempts += 1
        b.pay_reference = new_pay_reference(b.reference, b.attempts)
        b.status = "awaiting_payment"
        b.save(update_fields=["attempts", "pay_reference", "status"])
        return Response(_start_checkout(b))


@api_view(["GET"])
@permission_classes([AllowAny])
def booking_verify_view(request):
    ref = (request.query_params.get("reference") or request.query_params.get("trxref") or "").strip()
    if not ref:
        raise ValidationError("Missing payment reference.")
    b = Booking.objects.filter(pay_reference=ref).first() or Booking.objects.filter(reference__iexact=ref).first()
    if b is None:
        raise NotFound("We couldn't find that booking.")
    if b.status != "paid":
        try:
            finalize(b.pay_reference)
        except paystack.PaystackError as e:
            raise GatewayError(str(e))
        b.refresh_from_db()
    return Response(BookingPublicSerializer(b).data)


@csrf_exempt
def paystack_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not paystack.valid_signature(request.body, request.headers.get("x-paystack-signature", "")):
        return HttpResponse(status=401)
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    if event.get("event") == "charge.success":
        ref = (event.get("data") or {}).get("reference", "")
        try:
            finalize(ref)
        except paystack.PaystackError:
            log.exception("Paystack verify failed for %s", ref)
            return HttpResponse(status=502)  # Paystack retries
    return HttpResponse(status=200)


def finalize(pay_reference: str):
    """Verify the transaction with Paystack and, once, turn the booking into a paid shipment. Idempotent."""
    if not Booking.objects.filter(pay_reference=pay_reference).exclude(status="paid").exists():
        return
    data = paystack.verify(pay_reference)  # network call outside the DB transaction
    with transaction.atomic():
        b = Booking.objects.select_for_update().filter(pay_reference=pay_reference).first()
        if b is None or b.status == "paid":
            return
        auth = data.get("authorization") or {}
        state = data.get("status")
        matches = (data.get("amount") == paystack.to_subunit(b.amount)
                   and (data.get("currency") or "").upper() == b.currency.upper() and data.get("channel") == "card")
        if state != "success" or not matches:
            if state == "success":  # money taken, but not what we asked for: flag for staff
                b.status = "failed"
                b.gateway_message = f"Mismatch: {data.get('amount')} {data.get('currency')} via {data.get('channel')}"
                log.error("Booking %s: %s", b.reference, b.gateway_message)
            else:
                if state in ("failed", "abandoned", "reversed"):
                    b.status = "failed"
                b.gateway_message = (data.get("gateway_response") or state or "")[:255]
            b.save(update_fields=["status", "gateway_message"])
            return
        b.card = f"{(auth.get('card_type') or 'Card').strip().upper()} •••• {auth.get('last4') or '----'}"
        b.gateway_message = data.get("gateway_response", "")[:255]
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
        note=f"Online booking {b.reference} · Paystack {b.pay_reference}",
    )
    return sh


class BookingAdminViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = BookingAdminSerializer

    def get_queryset(self):
        qs = Booking.objects.select_related("shipment")
        st = self.request.query_params.get("status", "").strip()
        return qs.filter(status=st) if st else qs
