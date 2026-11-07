"""Quotes and online booking with card-only payment (Flutterwave Standard hosted checkout).

Flow: POST /api/bookings (priced on the server) -> Flutterwave card checkout -> customer returns to
/book/confirm?status=...&tx_ref=...&transaction_id=... -> GET /api/bookings/verify re-checks the transaction with
Flutterwave -> shipment + PAID receipt. The webhook (POST /api/flutterwave/webhook) runs the same idempotent
finalisation, so a closed browser tab still books.
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

from . import flutterwave
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
    b.charge_amount, b.charge_currency = flutterwave.charge_for(b.amount)  # rate fixed per attempt
    b.save(update_fields=["charge_amount", "charge_currency"])
    shipper = (b.details or {}).get("shipper") or {}
    try:
        data = flutterwave.initialize(
            email=b.email, name=shipper.get("name", ""), phone=shipper.get("phone", ""),
            amount=b.charge_amount, currency=b.charge_currency, reference=b.pay_reference,
            redirect_url=f"{settings.FRONTEND_URL}/book/confirm",
            title=settings.FLUTTERWAVE["CHECKOUT_TITLE"], logo=settings.FLUTTERWAVE["CHECKOUT_LOGO"],
            description=f"{settings.TRACKPRO['COMPANY_NAME']} · {b.product} shipment · booking {b.reference}",
            meta={"booking": b.reference, "product": b.product, "price": f"{b.amount:.2f} {b.currency}"},
        )
    except flutterwave.GatewayError as e:
        b.gateway_message = str(e)[:255]
        b.save(update_fields=["gateway_message"])
        raise GatewayError(str(e))
    return {"reference": b.reference, "authorization_url": data["link"]}


class BookingCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_form"

    def post(self, request):
        if not flutterwave.enabled():
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
    """Start a fresh card checkout for a booking that wasn't paid (charge references are single-use)."""
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
    q = request.query_params
    ref = (q.get("tx_ref") or q.get("reference") or "").strip()
    if not ref:
        raise ValidationError("Missing payment reference.")
    b = Booking.objects.filter(pay_reference=ref).first() or Booking.objects.filter(reference__iexact=ref).first()
    if b is None:
        raise NotFound("We couldn't find that booking.")
    if b.status != "paid":
        try:
            finalize(b.pay_reference, returned_status=q.get("status", ""))
        except flutterwave.GatewayError as e:
            raise GatewayError(str(e))
        b.refresh_from_db()
    return Response(BookingPublicSerializer(b).data)


@csrf_exempt
def flutterwave_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not flutterwave.valid_webhook(request.headers.get("verif-hash", "")):
        return HttpResponse(status=401)
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    if event.get("event") == "charge.completed":
        ref = (event.get("data") or {}).get("tx_ref", "")
        try:
            finalize(ref)  # always re-queries Flutterwave; the webhook body is only a trigger
        except flutterwave.NotFound:
            pass
        except flutterwave.GatewayError:
            log.exception("Flutterwave verify failed for %s", ref)
            return HttpResponse(status=502)  # Flutterwave retries (if retries are on in the dashboard)
    return HttpResponse(status=200)


def _amount_ok(paid, expected: float) -> bool:
    try:
        return float(paid) >= float(expected) - 0.005
    except (TypeError, ValueError):
        return False


def finalize(pay_reference: str, returned_status: str = ""):
    """Verify the transaction with Flutterwave and, once, turn the booking into a paid shipment. Idempotent;
    a booking marked failed is still completed if a late successful payment for it turns up."""
    if not pay_reference or not Booking.objects.filter(pay_reference=pay_reference).exclude(status="paid").exists():
        return
    try:
        data = flutterwave.verify(pay_reference)  # network call outside the DB transaction
    except flutterwave.NotFound:
        if returned_status in ("cancelled", "failed"):  # closed the checkout without paying
            Booking.objects.filter(pay_reference=pay_reference, status="awaiting_payment").update(
                status="failed", gateway_message="Payment cancelled")
            return
        raise
    with transaction.atomic():
        b = Booking.objects.select_for_update().filter(pay_reference=pay_reference).first()
        if b is None or b.status == "paid":
            return
        state = data.get("status")
        charge_amount = b.charge_amount if b.charge_amount is not None else b.amount
        charge_currency = b.charge_currency or b.currency
        matches = (data.get("tx_ref") == pay_reference
                   and _amount_ok(data.get("charged_amount", data.get("amount")), charge_amount)
                   and (data.get("currency") or "").upper() == charge_currency.upper()
                   and data.get("payment_type") == "card")
        if state != "successful" or not matches:
            if state == "successful":  # money taken, but not what we asked for: flag for staff
                b.status = "failed"
                b.gateway_message = (f"Mismatch: {data.get('charged_amount', data.get('amount'))} "
                                     f"{data.get('currency')} via {data.get('payment_type')}")
                log.error("Booking %s: %s", b.reference, b.gateway_message)
            else:
                if state == "failed":
                    b.status = "failed"
                b.gateway_message = str(data.get("processor_response") or state or "")[:255]
            b.save(update_fields=["status", "gateway_message"])
            return
        b.card = flutterwave.card_label(data)
        b.gateway_message = str(data.get("processor_response") or "Approved")[:255]
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
        note=f"Online booking {b.reference} · Flutterwave {b.pay_reference}" + (
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
