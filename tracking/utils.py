import secrets

from django.utils import timezone


def _unique(model, field, make):
    for _ in range(50):
        value = make()
        if not model.objects.filter(**{field: value}).exists():
            return value
    raise RuntimeError("Could not generate a unique reference")


def waybill_check_digit(body: str) -> int:
    """Mod-7 check digit, as used on express air waybills (e.g. DHL Express 10-digit waybills)."""
    return int(body) % 7


def is_valid_waybill(number: str) -> bool:
    return len(number) == 10 and number.isdigit() and waybill_check_digit(number[:9]) == int(number[9])


def new_tracking_number():
    """10-digit waybill number: 9 digits + mod-7 check digit, e.g. 4829137563."""
    from .models import Shipment

    def make():
        body = str(secrets.randbelow(9 * 10**8) + 10**8)
        return f"{body}{waybill_check_digit(body)}"
    return _unique(Shipment, "tracking_number", make)


def new_booking_reference():
    from .models import Booking
    return _unique(Booking, "reference", lambda: f"BK-{timezone.now():%y%m}-{secrets.token_hex(3).upper()}")


def new_pay_reference(booking_reference: str, attempt: int) -> str:
    return f"{booking_reference}-{attempt}-{secrets.token_hex(2).upper()}"


def new_receipt_number():
    """RCP-YYMM-NNNNNN"""
    from .models import Payment
    stamp = timezone.now().strftime("%y%m")
    return _unique(Payment, "receipt_number", lambda: f"RCP-{stamp}-{secrets.randbelow(900000) + 100000}")


def new_quote_reference():
    from .models import QuoteRequest
    return _unique(QuoteRequest, "reference", lambda: f"Q-{secrets.token_hex(3).upper()}")


def normalize_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")
