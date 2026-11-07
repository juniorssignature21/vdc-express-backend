from django.conf import settings
from rest_framework import serializers

from . import flutterwave
from .countries import COUNTRIES, NOT_SERVED, country_list
from .models import (
    COURIER_CONTACT_SERVICES, SERVICE_TYPES, STATUSES, Booking, ContactMessage, Payment, QuoteRequest, Shipment,
    ShipmentPhoto, TrackingEvent,
)
from .pricing import MAX_PIECE_KG, MAX_PIECES, MAX_SIDE_CM, PRODUCT_INFO, PRODUCTS, ShipmentSpec, starting_prices
from .utils import normalize_code

COUNTRY_CODES = list(COUNTRIES) + list(NOT_SERVED)  # embargoed ones get a clear "not served" message

LAT = {"min_value": -90, "max_value": 90, "allow_null": True, "required": False}
LNG = {"min_value": -180, "max_value": 180, "allow_null": True, "required": False}


# ------------------------------------------------------------------ events
class EventSerializer(serializers.ModelSerializer):
    lat = serializers.FloatField(**LAT)
    lng = serializers.FloatField(**LNG)
    occurred_at = serializers.DateTimeField(required=False)
    # input only: also move the parcel's live status/position to this event
    update_shipment = serializers.BooleanField(default=True, write_only=True)

    class Meta:
        model = TrackingEvent
        fields = ["id", "status", "title", "location", "lat", "lng", "note", "occurred_at", "update_shipment"]


# ------------------------------------------------------------------ payments
class LineItemSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=120)
    amount = serializers.FloatField()


class PaymentSerializer(serializers.ModelSerializer):
    items = serializers.ListField(child=LineItemSerializer(), required=False)  # stored as JSON
    vat_rate = serializers.FloatField(min_value=0, max_value=1, required=False)
    amount_paid = serializers.FloatField(min_value=0, allow_null=True, required=False)
    paid_at = serializers.DateTimeField(required=False)
    net = serializers.FloatField(read_only=True)
    vat = serializers.FloatField(read_only=True)
    total = serializers.FloatField(read_only=True)
    paid = serializers.FloatField(read_only=True)
    balance = serializers.FloatField(read_only=True)

    class Meta:
        model = Payment
        fields = ["id", "receipt_number", "method", "reference", "stamp", "items", "vat_rate", "amount_paid",
                  "payer_name", "note", "paid_at", "net", "vat", "total", "paid", "balance"]
        read_only_fields = ["receipt_number"]


# ------------------------------------------------------------------ photos
class PhotoSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    filename = serializers.SerializerMethodField()

    class Meta:
        model = ShipmentPhoto
        fields = ["id", "filename", "url", "caption", "created_at"]
        read_only_fields = ["created_at"]

    def get_url(self, obj):
        return obj.image.url if obj.image else ""

    def get_filename(self, obj):
        return obj.image.name.rsplit("/", 1)[-1] if obj.image else ""


# ------------------------------------------------------------------ shipments
class ShipmentListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shipment
        fields = ["id", "tracking_number", "status", "service_type", "international", "sender_name", "receiver_name",
                  "origin", "destination", "current_location", "eta", "source", "created_at", "updated_at"]


class ShipmentSerializer(serializers.ModelSerializer):
    """Admin create/update. tracking_number is only accepted on create (blank = auto-generate)."""
    tracking_number = serializers.CharField(max_length=32, required=False, allow_blank=True)
    origin_lat = serializers.FloatField(**LAT)
    origin_lng = serializers.FloatField(**LNG)
    dest_lat = serializers.FloatField(**LAT)
    dest_lng = serializers.FloatField(**LNG)
    current_lat = serializers.FloatField(**LAT)
    current_lng = serializers.FloatField(**LNG)
    progress = serializers.IntegerField(min_value=0, max_value=100, required=False)
    weight_kg = serializers.FloatField(min_value=0, required=False)
    length_cm = serializers.FloatField(min_value=0, required=False)
    width_cm = serializers.FloatField(min_value=0, required=False)
    height_cm = serializers.FloatField(min_value=0, required=False)
    declared_value = serializers.FloatField(min_value=0, required=False)
    insured_value = serializers.FloatField(min_value=0, required=False)
    chargeable_kg = serializers.FloatField(min_value=0, required=False)
    pieces = serializers.IntegerField(min_value=1, required=False)
    origin_country = serializers.ChoiceField(choices=COUNTRY_CODES, required=False, allow_blank=True)
    dest_country = serializers.ChoiceField(choices=COUNTRY_CODES, required=False, allow_blank=True)
    events = EventSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    photos = PhotoSerializer(many=True, read_only=True)

    class Meta:
        model = Shipment
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at", "source"]

    def validate(self, attrs):
        o = attrs.get("origin_country", getattr(self.instance, "origin_country", ""))
        d = attrs.get("dest_country", getattr(self.instance, "dest_country", ""))
        if o and d:
            attrs["international"] = o != d
        return attrs

    def validate_tracking_number(self, value):
        if self.instance is not None:
            return self.instance.tracking_number  # immutable after creation
        value = normalize_code(value)
        if value and Shipment.objects.filter(tracking_number__iexact=value).exists():
            raise serializers.ValidationError(f"Tracking number {value} already exists.")
        return value


# ------------------------------------------------------------------ quotes & contact
class QuoteCalcSerializer(serializers.Serializer):
    origin_country = serializers.ChoiceField(choices=COUNTRY_CODES, default="US")
    dest_country = serializers.ChoiceField(choices=COUNTRY_CODES, default="US")
    origin = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    destination = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    pieces = serializers.IntegerField(min_value=1, max_value=MAX_PIECES, default=1)
    weight_kg = serializers.FloatField(min_value=0, max_value=MAX_PIECE_KG, default=1)  # per piece
    length_cm = serializers.FloatField(min_value=0, max_value=MAX_SIDE_CM, default=0)
    width_cm = serializers.FloatField(min_value=0, max_value=MAX_SIDE_CM, default=0)
    height_cm = serializers.FloatField(min_value=0, max_value=MAX_SIDE_CM, default=0)
    is_document = serializers.BooleanField(default=False)
    insured_value = serializers.FloatField(min_value=0, default=0)
    signature = serializers.BooleanField(default=False)
    duties_paid = serializers.BooleanField(default=False)
    ship_date = serializers.DateField(required=False, allow_null=True)

    def spec(self) -> ShipmentSpec:
        d = self.validated_data
        return ShipmentSpec(**{k: d[k] for k in ShipmentSpec.__dataclass_fields__ if k in d})


class QuoteRequestSerializer(serializers.ModelSerializer):
    origin = serializers.CharField(min_length=2, max_length=120)
    destination = serializers.CharField(min_length=2, max_length=120)
    origin_country = serializers.ChoiceField(choices=COUNTRY_CODES, default="US")
    dest_country = serializers.ChoiceField(choices=COUNTRY_CODES, default="US")
    name = serializers.CharField(min_length=2, max_length=120)
    pieces = serializers.IntegerField(min_value=1, max_value=MAX_PIECES, default=1)
    weight_kg = serializers.FloatField(min_value=0, max_value=MAX_PIECE_KG, default=1)
    length_cm = serializers.FloatField(min_value=0, max_value=MAX_SIDE_CM, default=0)
    width_cm = serializers.FloatField(min_value=0, max_value=MAX_SIDE_CM, default=0)
    height_cm = serializers.FloatField(min_value=0, max_value=MAX_SIDE_CM, default=0)
    service_type = serializers.ChoiceField(choices=PRODUCTS, default="Express")

    class Meta:
        model = QuoteRequest
        fields = ["id", "reference", "name", "email", "phone", "message", "origin", "destination", "origin_country",
                  "dest_country", "international", "service_type", "pieces", "is_document", "weight_kg", "length_cm",
                  "width_cm", "height_cm", "chargeable_kg", "net", "vat", "total", "breakdown", "status", "created_at"]
        read_only_fields = ["reference", "international", "chargeable_kg", "net", "vat", "total", "breakdown", "status",
                            "created_at"]


class PartySerializer(serializers.Serializer):
    name = serializers.CharField(min_length=2, max_length=120)
    company = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    email = serializers.EmailField()
    phone = serializers.CharField(min_length=5, max_length=40)
    address1 = serializers.CharField(min_length=3, max_length=160)
    address2 = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    city = serializers.CharField(min_length=2, max_length=80)
    state = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")


class BookingSerializer(QuoteCalcSerializer):
    """Online booking: quote inputs + addresses, contents and the chosen product. Priced again on the server."""
    product = serializers.ChoiceField(choices=PRODUCTS)
    shipper = PartySerializer()
    receiver = PartySerializer()
    contents = serializers.CharField(min_length=3, max_length=255)
    customs_value = serializers.FloatField(min_value=0, default=0)
    customer_reference = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")
    accept_terms = serializers.BooleanField()

    def validate_accept_terms(self, value):
        if not value:
            raise serializers.ValidationError("Please confirm the shipment contains no prohibited items.")
        return value

    def validate(self, attrs):
        intl = attrs["origin_country"] != attrs["dest_country"]
        if intl and not attrs.get("is_document") and attrs.get("customs_value", 0) <= 0:
            raise serializers.ValidationError({"customs_value": "Enter the customs value of the goods."})
        return attrs


class BookingPublicSerializer(serializers.ModelSerializer):
    tracking_number = serializers.SerializerMethodField()
    receipt_number = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = ["reference", "status", "product", "amount", "currency", "charge_amount", "charge_currency",
                  "card", "gateway_message", "quote",
                  "tracking_number", "receipt_number", "created_at", "paid_at"]

    def get_tracking_number(self, b):
        return b.shipment.tracking_number if b.shipment_id else None

    def get_receipt_number(self, b):
        p = b.shipment.payments.first() if b.shipment_id else None
        return p.receipt_number if p else None


class BookingAdminSerializer(BookingPublicSerializer):
    class Meta(BookingPublicSerializer.Meta):
        fields = BookingPublicSerializer.Meta.fields + ["id", "email", "details", "attempts"]


class QuoteStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c[0] for c in QuoteRequest.QUOTE_STATUSES])


class ContactSerializer(serializers.ModelSerializer):
    name = serializers.CharField(min_length=2, max_length=120)
    message = serializers.CharField(min_length=5, max_length=5000)

    class Meta:
        model = ContactMessage
        fields = ["id", "name", "email", "subject", "message", "is_read", "created_at"]
        read_only_fields = ["is_read", "created_at"]


# ------------------------------------------------------------------ auth
class LoginSerializer(serializers.Serializer):
    email = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(min_length=8, trim_whitespace=False)


# ------------------------------------------------------------------ public read models (built from model instances)
def _point(label, lat, lng):
    return {"label": label, "lat": lat, "lng": lng}


def public_tracking(sh: Shipment, request=None) -> dict:
    """What customers see. Deliberately excludes contact details (emails, phones, full addresses)."""
    events = sorted(sh.events.all(), key=lambda e: (e.occurred_at, e.id), reverse=True)
    contact = sh.service_type in COURIER_CONTACT_SERVICES
    return {
        "tracking_number": sh.tracking_number,
        "status": sh.status,
        "status_label": sh.status_label,
        "service_type": sh.service_type,
        "delivery_by": PRODUCT_INFO.get(sh.service_type, {}).get("by", ""),
        "international": sh.international,
        "origin_country": sh.origin_country,
        "dest_country": sh.dest_country,
        "is_document": sh.is_document,
        "signature_required": sh.signature_required,
        "sender_name": sh.sender_name,
        "receiver_name": sh.receiver_name,
        "origin": _point(sh.origin, sh.origin_lat, sh.origin_lng),
        "destination": _point(sh.destination, sh.dest_lat, sh.dest_lng),
        "current": _point(sh.current_location, sh.current_lat, sh.current_lng),
        "progress": sh.progress,
        "contents": sh.contents,
        "weight_kg": sh.weight_kg,
        "pieces": sh.pieces,
        "courier_name": sh.courier_name,
        "courier_phone": sh.courier_phone if contact else "",
        "courier_contact": contact,
        "vehicle": sh.vehicle,
        "notice": sh.notice,
        "eta": sh.eta.isoformat() if sh.eta else None,
        "shipped_at": sh.shipped_at.isoformat() if sh.shipped_at else None,
        "updated_at": serializers.DateTimeField().to_representation(sh.updated_at),
        "events": EventSerializer(events, many=True).data,
        "photos": PhotoSerializer(sh.photos.all(), many=True).data,
        "receipts": [p.receipt_number for p in sh.payments.all()],
    }


def public_receipt(p: Payment) -> dict:
    t, sh = settings.TRACKPRO, p.shipment
    return {
        "payment": PaymentSerializer(p).data,
        "tracking_number": sh.tracking_number,
        "status_label": sh.status_label,
        "service_type": sh.service_type,
        "international": sh.international,
        "sender_name": sh.sender_name, "sender_address": sh.sender_address,
        "receiver_name": sh.receiver_name, "receiver_address": sh.receiver_address,
        "origin": sh.origin, "destination": sh.destination,
        "contents": sh.contents, "weight_kg": sh.weight_kg, "pieces": sh.pieces,
        "currency_symbol": t["CURRENCY_SYMBOL"], "currency_code": t["CURRENCY_CODE"], "tax_label": t["TAX_LABEL"],
        "incoterm": sh.incoterm if sh.international else "", "company_name": t["COMPANY_NAME"],
        "company_address": t["COMPANY_ADDRESS"], "company_email": t["COMPANY_EMAIL"],
        "company_phone": t["COMPANY_PHONE"],
    }


def site_settings() -> dict:
    t = settings.TRACKPRO
    prices = starting_prices()
    return {
        "company_name": t["COMPANY_NAME"], "company_phone": t["COMPANY_PHONE"], "company_email": t["COMPANY_EMAIL"],
        "company_address": t["COMPANY_ADDRESS"], "currency_symbol": t["CURRENCY_SYMBOL"],
        "currency_code": t["CURRENCY_CODE"], "vat_rate": t["VAT_RATE"], "tax_label": t["TAX_LABEL"],
        "fuel_surcharge": t["FUEL_SURCHARGE"], "domestic_country": t["DOMESTIC_COUNTRY"],
        "statuses": STATUSES, "service_types": SERVICE_TYPES,
        "products": [{"name": p, **PRODUCT_INFO[p], "from": prices[p]} for p in PRODUCTS],
        "international_from": prices["international"],
        "countries": country_list(),
        "limits": {"pieces": MAX_PIECES, "piece_kg": MAX_PIECE_KG, "side_cm": MAX_SIDE_CM},
        "payments_enabled": flutterwave.enabled(),
        "payment_currency": settings.FLUTTERWAVE["CURRENCY"],
        "payment_fx_rate": settings.FLUTTERWAVE["FX_RATE"],
        "stamps": ["PAID", "PART PAID", "CUSTOMS CLEARED", "REFUNDED", "UNPAID"],
        "max_upload_mb": settings.MAX_UPLOAD_MB,
    }
