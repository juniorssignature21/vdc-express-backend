from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from .models import (
    STATUSES, ContactMessage, Payment, QuoteRequest, Shipment, ShipmentPhoto, TrackingEvent,
)
from .pricing import QuoteError, ShipmentSpec, calc_quote, option_for
from .serializers import (
    ContactSerializer, EventSerializer, LoginSerializer, PasswordChangeSerializer, PaymentSerializer,
    PhotoSerializer, QuoteRequestSerializer, QuoteStatusSerializer, ShipmentListSerializer,
    ShipmentSerializer, public_receipt, public_tracking, site_settings,
)
from .utils import new_quote_reference, new_receipt_number, new_tracking_number, normalize_code

ACTIVE = ("pending", "picked_up", "in_transit", "at_hub", "customs", "out_for_delivery", "on_hold")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _vat_rate():
    return settings.TRACKPRO["VAT_RATE"]


def _shipments_full():
    return Shipment.objects.prefetch_related("events", "payments", "photos")


# ================================================================== public
@api_view(["GET"])
@permission_classes([AllowAny])
def settings_view(request):
    return Response(site_settings())


@api_view(["GET"])
@permission_classes([AllowAny])
def track_view(request, tracking_number):
    sh = _shipments_full().filter(tracking_number__iexact=normalize_code(tracking_number)).first()
    if sh is None:
        raise NotFound("We couldn't find a parcel with that tracking number.")
    return Response(public_tracking(sh, request))


@api_view(["GET"])
@permission_classes([AllowAny])
def receipts_view(request):
    """Look up receipts by tracking number (?id=) and/or receipt number (?r=)."""
    tracking, receipt = request.query_params.get("id", ""), request.query_params.get("r", "")
    if not (tracking.strip() or receipt.strip()):
        raise ValidationError("Enter a tracking number or a receipt number.")
    qs = Payment.objects.select_related("shipment").order_by("-paid_at")
    if receipt.strip():
        qs = qs.filter(receipt_number__iexact=normalize_code(receipt))
    if tracking.strip():
        qs = qs.filter(shipment__tracking_number__iexact=normalize_code(tracking))
    payments = list(qs)
    if not payments:
        raise NotFound("No receipt matches those details.")
    return Response([public_receipt(p) for p in payments])


class QuoteRequestCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "public_form"

    def post(self, request):
        s = QuoteRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            q = calc_quote(ShipmentSpec(d["origin_country"], d["dest_country"], d["pieces"], d["weight_kg"],
                                        d["length_cm"], d["width_cm"], d["height_cm"], d.get("is_document", False)))
            o = option_for(q, d["service_type"])
        except QuoteError as e:
            raise ValidationError(str(e))
        obj = s.save(reference=new_quote_reference(), international=q["route"]["international"],
                     is_document=q["is_document"], chargeable_kg=q["chargeable_kg"], net=o["net"], vat=o["tax"],
                     total=o["total"], breakdown=o["lines"])
        return Response(QuoteRequestSerializer(obj).data, status=status.HTTP_201_CREATED)


class ContactCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "public_form"

    def post(self, request):
        s = ContactSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        s.save()
        return Response({"ok": True, "message": "Thanks – we'll get back to you within the hour."},
                        status=status.HTTP_201_CREATED)


# ================================================================== admin auth
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        User = get_user_model()
        ident = s.validated_data["email"].strip()
        user = User.objects.filter(Q(email__iexact=ident) | Q(username__iexact=ident), is_staff=True, is_active=True).first()
        if user is None or not user.check_password(s.validated_data["password"]):
            raise AuthenticationFailed("Incorrect email or password.")
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        return Response({
            "access_token": str(AccessToken.for_user(user)), "token_type": "bearer",
            "name": user.get_full_name() or user.username, "email": user.email,
        })


@api_view(["GET"])
@permission_classes([IsAdminUser])
def me_view(request):
    u = request.user
    return Response({"id": u.id, "name": u.get_full_name() or u.username, "email": u.email})


@api_view(["POST"])
@permission_classes([IsAdminUser])
def password_view(request):
    s = PasswordChangeSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    user = request.user
    if not user.check_password(s.validated_data["current_password"]):
        raise ValidationError({"current_password": "Current password is incorrect."})
    try:
        validate_password(s.validated_data["new_password"], user)
    except DjangoValidationError as e:
        raise ValidationError({"new_password": list(e.messages)})
    user.set_password(s.validated_data["new_password"])
    user.save()
    return Response({"ok": True})


# ================================================================== admin dashboard
@api_view(["GET"])
@permission_classes([IsAdminUser])
def stats_view(request):
    by_status = {r["status"]: r["n"] for r in Shipment.objects.values("status").annotate(n=Count("id"))}
    revenue = sum(p.paid for p in Payment.objects.exclude(stamp="REFUNDED"))
    recent = Shipment.objects.order_by("-updated_at")[:8]
    return Response({
        "shipments": sum(by_status.values()),
        "active": sum(v for k, v in by_status.items() if k in ACTIVE),
        "delivered": by_status.get("delivered", 0),
        "exceptions": by_status.get("exception", 0) + by_status.get("returned", 0),
        "revenue": round(revenue, 2),
        "new_quotes": QuoteRequest.objects.filter(status="new").count(),
        "unread_messages": ContactMessage.objects.filter(is_read=False).count(),
        "by_status": by_status,
        "recent": ShipmentListSerializer(recent, many=True).data,
    })


# ================================================================== admin shipments
class ShipmentPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response({"items": data, "total": self.page.paginator.count,
                         "page": self.page.number, "page_size": self.get_page_size(self.request)})


def apply_event(sh: Shipment, ev: TrackingEvent):
    """Move the shipment's live status/position to `ev`, unless a newer event already exists."""
    newer = sh.events.exclude(pk=ev.pk).filter(occurred_at__gt=ev.occurred_at).exists()
    if newer:
        return  # back-dated scan: keep the live position
    sh.status = ev.status
    if ev.location:
        sh.current_location = ev.location
    if ev.lat is not None and ev.lng is not None:
        sh.current_lat, sh.current_lng = ev.lat, ev.lng
    if ev.status == "delivered":
        sh.progress = 100
        if sh.dest_lat is not None and ev.lat is None:
            sh.current_lat, sh.current_lng = sh.dest_lat, sh.dest_lng
    sh.save()


class ShipmentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                      viewsets.GenericViewSet):
    permission_classes = [IsAdminUser]
    pagination_class = ShipmentPagination
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        if self.action == "list":
            qs = Shipment.objects.all()
            q = self.request.query_params.get("q", "").strip()
            st = self.request.query_params.get("status", "").strip()
            if q:
                qs = qs.filter(Q(tracking_number__icontains=q) | Q(sender_name__icontains=q) |
                               Q(receiver_name__icontains=q) | Q(origin__icontains=q) | Q(destination__icontains=q))
            if st:
                qs = qs.filter(status=st)
            return qs.order_by("-updated_at")
        return _shipments_full()

    def get_serializer_class(self):
        return ShipmentListSerializer if self.action == "list" else ShipmentSerializer

    def _full(self, pk, code=status.HTTP_200_OK):
        return Response(ShipmentSerializer(_shipments_full().get(pk=pk)).data, status=code)

    # ---- create / update
    @transaction.atomic
    def create(self, request):
        s = ShipmentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        number = s.validated_data.pop("tracking_number", "") or new_tracking_number()
        sh = s.save(tracking_number=number)
        if sh.current_lat is None and sh.origin_lat is not None:
            sh.current_lat, sh.current_lng = sh.origin_lat, sh.origin_lng
            sh.current_location = sh.current_location or sh.origin
            sh.save()
        sh.events.create(status=sh.status, title=STATUSES[sh.status], location=sh.current_location or sh.origin,
                         lat=sh.current_lat, lng=sh.current_lng, note="Shipment created", occurred_at=timezone.now())
        return self._full(sh.pk, status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        sh = self.get_object()
        s = ShipmentSerializer(sh, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        return self._full(sh.pk)

    update = partial_update

    # ---- tracking events
    @action(detail=True, methods=["post"], url_path="events")
    @transaction.atomic
    def add_event(self, request, pk=None):
        sh = self.get_object()
        s = EventSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        move = s.validated_data.pop("update_shipment", True)
        d = s.validated_data
        ev = s.save(shipment=sh, title=(d.get("title") or "").strip() or STATUSES[d["status"]],
                    occurred_at=d.get("occurred_at") or timezone.now())
        if move:
            apply_event(sh, ev)
        else:
            sh.save(update_fields=["updated_at"])
        return self._full(sh.pk, status.HTTP_201_CREATED)

    @action(detail=True, methods=["put", "patch", "delete"], url_path=r"events/(?P<event_id>\d+)")
    @transaction.atomic
    def event_detail(self, request, pk=None, event_id=None):
        sh = self.get_object()
        ev = get_object_or_404(TrackingEvent, pk=event_id, shipment=sh)
        if request.method == "DELETE":
            ev.delete()
            return self._full(sh.pk)
        s = EventSerializer(ev, data=request.data, partial=request.method == "PATCH")
        s.is_valid(raise_exception=True)
        move = s.validated_data.pop("update_shipment", True)
        title = (s.validated_data.get("title", ev.title) or "").strip()
        ev = s.save(title=title or STATUSES[s.validated_data.get("status", ev.status)])
        if move:
            apply_event(sh, ev)
        return self._full(sh.pk)

    # ---- payments / receipts
    @action(detail=True, methods=["post"], url_path="payments")
    def add_payment(self, request, pk=None):
        sh = self.get_object()
        s = PaymentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        s.save(shipment=sh, receipt_number=new_receipt_number(),
               paid_at=s.validated_data.get("paid_at") or timezone.now(),
               vat_rate=s.validated_data.get("vat_rate", _vat_rate()))
        return self._full(sh.pk, status.HTTP_201_CREATED)

    @action(detail=True, methods=["put", "patch", "delete"], url_path=r"payments/(?P<payment_id>\d+)")
    def payment_detail(self, request, pk=None, payment_id=None):
        sh = self.get_object()
        p = get_object_or_404(Payment, pk=payment_id, shipment=sh)
        if request.method == "DELETE":
            p.delete()
            return self._full(sh.pk)
        s = PaymentSerializer(p, data=request.data, partial=request.method == "PATCH")
        s.is_valid(raise_exception=True)
        s.save()
        return self._full(sh.pk)

    # ---- pricing: charge lines for a new receipt
    @action(detail=True, methods=["get"])
    def quote(self, request, pk=None):
        sh = self.get_object()
        home = settings.TRACKPRO["DOMESTIC_COUNTRY"]
        oc = sh.origin_country or home
        dc = sh.dest_country or (oc if not sh.international else "")
        pieces = max(sh.pieces, 1)
        try:
            q = calc_quote(ShipmentSpec(
                oc, dc, pieces, sh.weight_kg / pieces, sh.length_cm, sh.width_cm, sh.height_cm, sh.is_document,
                sh.insured_value, sh.signature_required, sh.incoterm == "DDP"))
            return Response(option_for(q, sh.service_type))
        except QuoteError as e:
            raise ValidationError(str(e))

    # ---- photos
    @action(detail=True, methods=["post"], url_path="photos")
    @transaction.atomic
    def upload_photos(self, request, pk=None):
        sh = self.get_object()
        files = request.FILES.getlist("files")
        if not files:
            raise ValidationError({"files": "Choose at least one image."})
        for f in files:
            if f.content_type not in ALLOWED_IMAGE_TYPES:
                return Response({"detail": f"{f.name}: only JPEG, PNG, WebP or GIF images are allowed."},
                                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
            if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                return Response({"detail": f"{f.name}: images must be {settings.MAX_UPLOAD_MB:g} MB or smaller."},
                                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        caption = (request.data.get("caption") or "")[:160]
        for f in files:
            ShipmentPhoto.objects.create(shipment=sh, image=f, caption=caption)
        return self._full(sh.pk, status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"photos/(?P<photo_id>\d+)")
    def photo_detail(self, request, pk=None, photo_id=None):
        sh = self.get_object()
        ph = get_object_or_404(ShipmentPhoto, pk=photo_id, shipment=sh)
        if request.method == "DELETE":
            ph.delete()  # file removed by post_delete signal
        else:
            s = PhotoSerializer(ph, data={"caption": request.data.get("caption", "")}, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        return self._full(sh.pk)


# ================================================================== admin quotes & messages
class QuoteAdminViewSet(mixins.ListModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = QuoteRequestSerializer

    def get_queryset(self):
        qs = QuoteRequest.objects.all()
        st = self.request.query_params.get("status", "").strip()
        return qs.filter(status=st) if st else qs

    def partial_update(self, request, pk=None):
        q = self.get_object()
        s = QuoteStatusSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        q.status = s.validated_data["status"]
        q.save(update_fields=["status"])
        return Response(QuoteRequestSerializer(q).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def convert(self, request, pk=None):
        """Create a shipment (with an UNPAID receipt) from a quote request."""
        q = self.get_object()
        sh = Shipment.objects.create(
            tracking_number=new_tracking_number(), status="pending", service_type=q.service_type,
            international=q.international, origin_country=q.origin_country, dest_country=q.dest_country,
            is_document=q.is_document, pieces=q.pieces, chargeable_kg=q.chargeable_kg,
            sender_name=q.name, sender_email=q.email, sender_phone=q.phone,
            origin=q.origin, destination=q.destination, current_location=q.origin,
            weight_kg=round(q.weight_kg * q.pieces, 2), length_cm=q.length_cm, width_cm=q.width_cm, height_cm=q.height_cm,
            shipped_at=date.today(),
        )
        sh.events.create(status="pending", title="Shipment information received", location=q.origin,
                         note=f"Booked from quote {q.reference}", occurred_at=timezone.now())
        sh.payments.create(
            receipt_number=new_receipt_number(), method="Pending", stamp="UNPAID",
            items=[{"label": ln["label"], "amount": ln["amount"]} for ln in q.breakdown]
            or [{"label": f"{q.service_type} ({q.chargeable_kg:g} kg)", "amount": q.net}],
            vat_rate=_vat_rate(), amount_paid=0, payer_name=q.name, paid_at=timezone.now(),
        )
        q.status = "booked"
        q.save(update_fields=["status"])
        return Response(ShipmentSerializer(_shipments_full().get(pk=sh.pk)).data, status=status.HTTP_201_CREATED)


class MessageAdminViewSet(mixins.ListModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = ContactSerializer
    queryset = ContactMessage.objects.all()

    def partial_update(self, request, pk=None):
        m = self.get_object()
        raw = request.query_params.get("is_read", request.data.get("is_read", "true"))
        m.is_read = str(raw).lower() in ("1", "true", "yes", "on")
        m.save(update_fields=["is_read"])
        return Response(ContactSerializer(m).data)
