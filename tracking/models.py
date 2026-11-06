from django.db import models

from .pricing import COURIER_CONTACT_PRODUCTS, PRODUCTS

# Shipment status codes (express-integrator checkpoint style), in journey order.
# "attempted", "on_hold", "exception" and "returned" are off-path states.
STATUSES = {
    "pending": "Shipment information received",
    "picked_up": "Picked up",
    "at_hub": "Processed at facility",
    "in_transit": "In transit",
    "customs": "Customs clearance",
    "out_for_delivery": "With delivery courier",
    "delivered": "Delivered",
    "attempted": "Delivery attempted",
    "on_hold": "On hold",
    "exception": "Exception",
    "returned": "Returned to shipper",
}
STATUS_CHOICES = list(STATUSES.items())
SERVICE_TYPES = PRODUCTS
SERVICE_CHOICES = [(s, s) for s in SERVICE_TYPES]
INCOTERM_CHOICES = [("DAP", "DAP – receiver pays duties & taxes"), ("DDP", "DDP – shipper pays duties & taxes")]
STAMPS = ["PAID", "PART PAID", "CUSTOMS CLEARED", "REFUNDED", "UNPAID"]
STAMP_CHOICES = [(s, s) for s in STAMPS]
VEHICLE_CHOICES = [(v, v.title()) for v in ("truck", "van", "bike", "plane", "ship")]
COURIER_CONTACT_SERVICES = COURIER_CONTACT_PRODUCTS


class Shipment(models.Model):
    tracking_number = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="pending")
    service_type = models.CharField(max_length=32, choices=SERVICE_CHOICES, default="Express")
    international = models.BooleanField(default=False)
    origin_country = models.CharField(max_length=2, blank=True)
    dest_country = models.CharField(max_length=2, blank=True)
    is_document = models.BooleanField(default=False)
    incoterm = models.CharField(max_length=3, choices=INCOTERM_CHOICES, default="DAP")
    signature_required = models.BooleanField(default=False)
    insured_value = models.FloatField(default=0)
    customer_reference = models.CharField(max_length=60, blank=True)
    source = models.CharField(max_length=10, default="admin")  # admin | online

    sender_name = models.CharField(max_length=120, blank=True)
    sender_phone = models.CharField(max_length=60, blank=True)
    sender_email = models.CharField(max_length=255, blank=True)
    sender_address = models.CharField(max_length=255, blank=True)
    receiver_name = models.CharField(max_length=120, blank=True)
    receiver_phone = models.CharField(max_length=60, blank=True)
    receiver_email = models.CharField(max_length=255, blank=True)
    receiver_address = models.CharField(max_length=255, blank=True)

    origin = models.CharField(max_length=120, blank=True)
    origin_lat = models.FloatField(null=True, blank=True)
    origin_lng = models.FloatField(null=True, blank=True)
    destination = models.CharField(max_length=120, blank=True)
    dest_lat = models.FloatField(null=True, blank=True)
    dest_lng = models.FloatField(null=True, blank=True)
    current_location = models.CharField(max_length=160, blank=True)
    current_lat = models.FloatField(null=True, blank=True)
    current_lng = models.FloatField(null=True, blank=True)
    progress = models.PositiveSmallIntegerField(default=0)  # 0-100, shown on the location ring

    contents = models.CharField(max_length=255, blank=True)
    weight_kg = models.FloatField(default=1)
    length_cm = models.FloatField(default=0)
    width_cm = models.FloatField(default=0)
    height_cm = models.FloatField(default=0)
    pieces = models.PositiveIntegerField(default=1)
    declared_value = models.FloatField(default=0)  # customs value for international goods
    chargeable_kg = models.FloatField(default=0)

    courier_name = models.CharField(max_length=120, blank=True)
    courier_phone = models.CharField(max_length=60, blank=True)
    vehicle = models.CharField(max_length=20, choices=VEHICLE_CHOICES, default="truck")

    notice = models.TextField(blank=True)  # optional banner on the tracking page
    eta = models.DateField(null=True, blank=True)
    shipped_at = models.DateField(null=True, blank=True)  # pickup / ship date
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.tracking_number

    @property
    def status_label(self):
        return STATUSES.get(self.status, self.status)


class TrackingEvent(models.Model):
    shipment = models.ForeignKey(Shipment, related_name="events", on_delete=models.CASCADE)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES)
    title = models.CharField(max_length=160, blank=True)
    location = models.CharField(max_length=160, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    note = models.TextField(blank=True)
    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["occurred_at", "id"]

    def __str__(self):
        return f"{self.shipment} · {self.title}"


class Payment(models.Model):
    shipment = models.ForeignKey(Shipment, related_name="payments", on_delete=models.CASCADE)
    receipt_number = models.CharField(max_length=32, unique=True)
    method = models.CharField(max_length=40, default="Card")
    reference = models.CharField(max_length=80, blank=True)
    stamp = models.CharField(max_length=32, choices=STAMP_CHOICES, default="PAID")
    # [{"label": "Shipping", "amount": 12.5}, ...] – net amounts, VAT added on top
    items = models.JSONField(default=list, blank=True)
    vat_rate = models.FloatField(default=0.2)
    amount_paid = models.FloatField(null=True, blank=True)  # null = paid in full
    payer_name = models.CharField(max_length=120, blank=True)
    note = models.TextField(blank=True)
    paid_at = models.DateTimeField()

    class Meta:
        ordering = ["paid_at", "id"]

    def __str__(self):
        return self.receipt_number

    @property
    def net(self):
        return round(sum(float(i.get("amount") or 0) for i in self.items), 2)

    @property
    def vat(self):
        return round(self.net * self.vat_rate, 2)

    @property
    def total(self):
        return round(self.net + self.vat, 2)

    @property
    def paid(self):
        return round(self.total if self.amount_paid is None else self.amount_paid, 2)

    @property
    def balance(self):
        return round(max(self.total - self.paid, 0), 2)


class ShipmentPhoto(models.Model):
    shipment = models.ForeignKey(Shipment, related_name="photos", on_delete=models.CASCADE)
    image = models.FileField(upload_to="packages/")
    caption = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class QuoteRequest(models.Model):
    QUOTE_STATUSES = [(s, s.title()) for s in ("new", "contacted", "booked", "closed")]

    reference = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=60, blank=True)
    message = models.TextField(blank=True)
    origin = models.CharField(max_length=120)
    destination = models.CharField(max_length=120)
    international = models.BooleanField(default=False)
    origin_country = models.CharField(max_length=2, blank=True)
    dest_country = models.CharField(max_length=2, blank=True)
    service_type = models.CharField(max_length=32, choices=SERVICE_CHOICES)
    pieces = models.PositiveIntegerField(default=1)
    is_document = models.BooleanField(default=False)
    weight_kg = models.FloatField()
    length_cm = models.FloatField(default=0)
    width_cm = models.FloatField(default=0)
    height_cm = models.FloatField(default=0)
    chargeable_kg = models.FloatField()
    net = models.FloatField()
    vat = models.FloatField()
    total = models.FloatField()
    breakdown = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=QUOTE_STATUSES, default="new")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.reference


class ContactMessage(models.Model):
    name = models.CharField(max_length=120)
    email = models.EmailField()
    subject = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name}: {self.subject}"


class Booking(models.Model):
    """An online booking. The shipment is only created once Paystack confirms the card payment."""
    STATES = [(s, s.replace("_", " ").title()) for s in ("awaiting_payment", "paid", "failed")]

    reference = models.CharField(max_length=32, unique=True)          # ours, shown to the customer
    pay_reference = models.CharField(max_length=64, unique=True)      # current Paystack transaction reference
    status = models.CharField(max_length=20, choices=STATES, default="awaiting_payment")
    email = models.EmailField()
    product = models.CharField(max_length=32, choices=SERVICE_CHOICES)
    amount = models.FloatField()
    currency = models.CharField(max_length=3)
    quote = models.JSONField(default=dict)    # priced option + route, frozen at booking time
    details = models.JSONField(default=dict)  # validated booking form
    shipment = models.OneToOneField(Shipment, null=True, blank=True, on_delete=models.SET_NULL, related_name="booking")
    card = models.CharField(max_length=60, blank=True)
    gateway_message = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.reference
