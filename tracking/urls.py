from django.urls import include, path
from rest_framework.routers import SimpleRouter

from . import bookings, views

router = SimpleRouter(trailing_slash=False)
router.register("admin/shipments", views.ShipmentViewSet, basename="admin-shipments")
router.register("admin/quotes", views.QuoteAdminViewSet, basename="admin-quotes")
router.register("admin/messages", views.MessageAdminViewSet, basename="admin-messages")
router.register("admin/bookings", bookings.BookingAdminViewSet, basename="admin-bookings")

urlpatterns = [
    # public
    path("settings", views.settings_view),
    path("track/<str:tracking_number>", views.track_view),
    path("receipts", views.receipts_view),
    path("quote/calculate", bookings.quote_calculate_view),
    path("bookings", bookings.BookingCreateView.as_view()),
    path("bookings/verify", bookings.booking_verify_view),
    path("bookings/<str:reference>/pay", bookings.BookingRetryView.as_view()),
    path("stripe/webhook", bookings.stripe_webhook),
    path("quotes", views.QuoteRequestCreateView.as_view()),
    path("contact", views.ContactCreateView.as_view()),
    # admin
    path("admin/login", views.LoginView.as_view()),
    path("admin/me", views.me_view),
    path("admin/password", views.password_view),
    path("admin/stats", views.stats_view),
    path("", include(router.urls)),
]
