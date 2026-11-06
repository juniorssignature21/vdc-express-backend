"""python manage.py seed  – creates the first admin account and (optionally) demo shipments. Safe to re-run."""
from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tracking.models import Payment, Shipment, TrackingEvent


def ensure_admin(stdout=None):
    User = get_user_model()
    if User.objects.filter(is_staff=True).exists():
        return False
    email = settings.TRACKPRO["ADMIN_EMAIL"]
    User.objects.create_superuser(username=email.split("@")[0] or "admin", email=email,
                                  password=settings.TRACKPRO["ADMIN_PASSWORD"], first_name="Administrator")
    if stdout:
        stdout.write(f"Created admin {email}")
    return True


def _create(fields, events, payments):
    now = timezone.now()
    sh = Shipment.objects.create(**fields)
    TrackingEvent.objects.bulk_create([
        TrackingEvent(shipment=sh, status=s, title=t, location=loc, lat=lat, lng=lng, note=note,
                      occurred_at=now - timedelta(hours=h))
        for h, s, t, loc, lat, lng, note in events
    ])
    for p in payments:
        hours = p.pop("hours_ago")
        Payment.objects.create(shipment=sh, paid_at=now - timedelta(hours=hours), **p)


@transaction.atomic
def seed_demo(stdout=None):
    if Shipment.objects.exists():
        return False
    today = date.today()

    # 1 — domestic Express, in transit on I-45/I-55 towards Chicago
    _create(dict(
        tracking_number="4829137563", status="in_transit", service_type="Express",
        origin_country="US", dest_country="US", chargeable_kg=4.5, source="online",
        sender_name="Amelia Hart", sender_phone="+1 713 555 0111", sender_email="amelia@example.com",
        sender_address="1200 Main St, Houston, TX 77002, United States",
        receiver_name="Daniel Brooks", receiver_phone="+1 312 555 0102", receiver_email="daniel@example.com",
        receiver_address="233 S Wacker Dr, Chicago, IL 60606, United States",
        origin="Houston, TX", origin_lat=29.7604, origin_lng=-95.3698,
        destination="Chicago, IL", dest_lat=41.8781, dest_lng=-87.6298,
        current_location="Memphis, TN gateway", current_lat=35.1495, current_lng=-90.0490, progress=62,
        contents="Leather boots", weight_kg=2.4, length_cm=40, width_cm=30, height_cm=18, declared_value=180,
        courier_name="Marcus Reid", courier_phone="+1 832 555 0123", vehicle="plane", signature_required=True,
        eta=today + timedelta(days=1), shipped_at=today - timedelta(days=1),
    ), [
        (30, "pending", "Shipment information received", "Houston, TX", 29.7604, -95.3698, "Booked online, paid by card"),
        (26, "picked_up", "Picked up", "Downtown Houston, TX", 29.7589, -95.3677, "Collected by courier"),
        (20, "at_hub", "Processed at facility", "Houston, TX service center", 29.9902, -95.3368, ""),
        (14, "in_transit", "Departed facility", "Houston, TX service center", 29.9902, -95.3368, "Linehaul to Memphis gateway"),
        (1, "in_transit", "Arrived at sort facility", "Memphis, TN gateway", 35.1495, -90.0490, "On schedule"),
    ], [dict(
        receipt_number="RCP-2611-482913", method="Card", reference="VISA •••• 4242", stamp="PAID", vat_rate=0,
        items=[{"label": "Express transport (4.5 kg)", "amount": 41.20}, {"label": "Fuel surcharge (18%)", "amount": 7.42},
               {"label": "Direct signature", "amount": 7.50}],
        payer_name="Amelia Hart", hours_ago=30,
    )])

    # 2 — export to Nigeria, in customs clearance at Lagos
    _create(dict(
        tracking_number="5710042181", status="customs", service_type="Express", international=True,
        origin_country="US", dest_country="NG", incoterm="DAP", chargeable_kg=8.0,
        sender_name="Grace Okafor", sender_phone="+1 281 555 0144", sender_email="grace@example.com",
        sender_address="8 Westheimer Rd, Houston, TX 77006, United States",
        receiver_name="Chinedu Okafor", receiver_phone="+234 803 555 0199", receiver_email="chinedu@example.com",
        receiver_address="8 Admiralty Way, Lekki, Lagos, Nigeria",
        origin="Houston, TX", origin_lat=29.7604, origin_lng=-95.3698,
        destination="Lagos, Nigeria", dest_lat=6.5244, dest_lng=3.3792,
        current_location="Lagos airport (LOS)", current_lat=6.5774, current_lng=3.3210, progress=80,
        contents="Laptop and accessories", weight_kg=4.0, length_cm=50, width_cm=40, height_cm=20, declared_value=1250,
        courier_name="Tunde Bello", courier_phone="+234 803 555 0123", vehicle="plane",
        notice="Your shipment is in customs clearance. Duties and taxes are payable by the receiver (DAP).",
        eta=today + timedelta(days=2), shipped_at=today - timedelta(days=3),
    ), [
        (75, "pending", "Shipment information received", "Houston, TX", 29.7604, -95.3698, ""),
        (70, "picked_up", "Picked up", "Houston, TX", 29.7604, -95.3698, ""),
        (62, "at_hub", "Processed at facility", "Houston, TX (IAH) gateway", 29.9902, -95.3368, "Export documents checked"),
        (52, "in_transit", "Departed facility", "Houston, TX (IAH) gateway", 29.9902, -95.3368, "Flight IAH → LOS"),
        (30, "at_hub", "Arrived at facility", "Lagos airport (LOS)", 6.5774, 3.3210, ""),
        (24, "customs", "Customs clearance", "Lagos airport (LOS)", 6.5774, 3.3210, "Awaiting duty payment by receiver"),
    ], [dict(
        receipt_number="RCP-2611-571004", method="Card", reference="MC •••• 5100", stamp="PAID", vat_rate=0,
        items=[{"label": "Express transport (8 kg)", "amount": 280.00}, {"label": "Fuel surcharge (18%)", "amount": 50.40},
               {"label": "Shipment value protection ($1,250.00)", "amount": 15.00}],
        payer_name="Grace Okafor", hours_ago=75,
    )])

    # 3 — Same Day, delivered
    _create(dict(
        tracking_number="3065581201", status="delivered", service_type="Same Day",
        origin_country="US", dest_country="US", chargeable_kg=0.5,
        sender_name="Fatima Saleh", sender_email="fatima@example.com", sender_address="910 Louisiana St, Houston, TX 77002",
        receiver_name="Oliver Grant", receiver_address="2228 Mechanic St, Galveston, TX 77550",
        origin="Houston, TX", origin_lat=29.7604, origin_lng=-95.3698,
        destination="Galveston, TX", dest_lat=29.3013, dest_lng=-94.7977,
        current_location="Mechanic St, Galveston, TX", current_lat=29.3070, current_lng=-94.7930, progress=100,
        contents="Documents", is_document=True, weight_kg=0.4, courier_name="Priya Shah", courier_phone="+1 832 555 0456",
        vehicle="bike", eta=today - timedelta(days=2), shipped_at=today - timedelta(days=2),
    ), [
        (54, "pending", "Shipment information received", "Houston, TX", 29.7604, -95.3698, ""),
        (53, "picked_up", "Picked up", "Downtown Houston, TX", 29.7580, -95.3650, ""),
        (52, "out_for_delivery", "With delivery courier", "I-45 southbound", 29.5000, -95.1000, "Dedicated courier"),
        (50, "delivered", "Delivered", "Mechanic St, Galveston, TX", 29.3070, -94.7930, "Signed for by O. Grant"),
    ], [dict(
        receipt_number="RCP-2611-306558", method="Card", reference="VISA •••• 1881", stamp="PAID", vat_rate=0,
        items=[{"label": "Same Day transport (0.5 kg)", "amount": 45.00}, {"label": "Fuel surcharge (18%)", "amount": 8.10}],
        payer_name="Fatima Saleh", hours_ago=54,
    )])
    if stdout:
        stdout.write("Loaded 3 demo shipments")
    return True


class Command(BaseCommand):
    help = "Create the first admin account and load demo shipments into an empty database."

    def add_arguments(self, parser):
        parser.add_argument("--no-demo", action="store_true", help="Only create the admin account.")

    def handle(self, *args, **opts):
        if not ensure_admin(self.stdout):
            self.stdout.write("Admin account already exists")
        if not opts["no_demo"] and settings.TRACKPRO["SEED_DEMO_DATA"]:
            if not seed_demo(self.stdout):
                self.stdout.write("Shipments already exist – demo data skipped")
