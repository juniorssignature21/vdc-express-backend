"""Django admin (at /django-admin/) – a raw data view alongside the React admin panel."""
from django.contrib import admin

from .models import Booking, ContactMessage, Payment, QuoteRequest, Shipment, ShipmentPhoto, TrackingEvent


class EventInline(admin.TabularInline):
    model = TrackingEvent
    extra = 0


class PaymentInline(admin.StackedInline):
    model = Payment
    extra = 0


class PhotoInline(admin.TabularInline):
    model = ShipmentPhoto
    extra = 0


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["tracking_number", "status", "service_type", "origin", "destination", "receiver_name", "updated_at"]
    list_filter = ["status", "service_type", "international", "source"]
    search_fields = ["tracking_number", "sender_name", "receiver_name", "origin", "destination"]
    inlines = [EventInline, PaymentInline, PhotoInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["receipt_number", "shipment", "stamp", "method", "paid_at"]
    list_filter = ["stamp", "method"]
    search_fields = ["receipt_number", "shipment__tracking_number"]


@admin.register(QuoteRequest)
class QuoteRequestAdmin(admin.ModelAdmin):
    list_display = ["reference", "name", "origin", "destination", "service_type", "total", "status", "created_at"]
    list_filter = ["status", "service_type"]


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ["name", "email", "subject", "is_read", "created_at"]
    list_filter = ["is_read"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["reference", "email", "product", "amount", "currency", "status", "card", "shipment", "created_at"]
    list_filter = ["status", "product"]
    search_fields = ["reference", "pay_reference", "email"]
    readonly_fields = ["pay_reference", "quote", "details", "paid_at", "created_at"]
