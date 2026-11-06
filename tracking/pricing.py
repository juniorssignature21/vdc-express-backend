"""Shipping prices and delivery dates, following express-integrator (DHL Express-style) rules.

* Products: Same Day, Express 9:00, Express 12:00, Express, Economy Select.
* Route: domestic, export (leaving the home market), import (arriving) or third-country, priced by rate zone.
* Chargeable weight per piece = the higher of actual and volumetric weight (L x W x H / 5000), rounded UP to the next
  0.5 kg up to 30 kg and to the next whole kg above that, minimum 0.5 kg.
* Rate card: price for the first 0.5 kg + each additional 0.5 kg up to 30 kg, then a per-kg rate.
* Surcharges: time-definite premium, fuel surcharge (% of transport), oversize / overweight piece, elevated-risk
  destination, Duty Tax Paid (DDP), direct signature and shipment value protection (insurance).
* Delivery date: business days (Mon–Fri) from the first possible pickup day, honouring the daily cut-off.

The frontend never prices on its own: it calls /api/quote/calculate, so this module is the single source of truth.
"""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from .countries import COUNTRIES, ELEVATED_RISK, NOT_SERVED, ZONE_NAMES, country_name

SAME_DAY, EXPRESS_9, EXPRESS_12, EXPRESS, ECONOMY = "Same Day", "Express 9:00", "Express 12:00", "Express", "Economy Select"
PRODUCTS = [SAME_DAY, EXPRESS_9, EXPRESS_12, EXPRESS, ECONOMY]
PRODUCT_INFO = {
    SAME_DAY: {"code": "SDX", "icon": "bi-rocket-takeoff-fill", "by": "end of day",
               "blurb": "Dedicated courier, collected and delivered today. Domestic only."},
    EXPRESS_9: {"code": "X09", "icon": "bi-alarm-fill", "by": "9:00 AM",
                "blurb": "Time-definite: delivered by 9:00 AM on the next possible business day."},
    EXPRESS_12: {"code": "X12", "icon": "bi-clock-fill", "by": "12:00 PM",
                 "blurb": "Time-definite: delivered by noon on the next possible business day."},
    EXPRESS: {"code": "XPD", "icon": "bi-lightning-charge-fill", "by": "end of day",
              "blurb": "Delivered by end of the next possible business day."},
    ECONOMY: {"code": "ECX", "icon": "bi-truck", "by": "end of day",
              "blurb": "Day-definite, lower-cost service for heavier, less urgent shipments."},
}
# Products whose couriers customers may call / chat from the tracking page.
COURIER_CONTACT_PRODUCTS = (SAME_DAY, EXPRESS_9, EXPRESS_12, EXPRESS)
LEGACY_PRODUCTS = {"Standard": ECONOMY, "Economy": ECONOMY}  # services used before the product range changed

VOLUMETRIC_DIVISOR = 5000
MIN_CHARGEABLE_KG = 0.5
MAX_PIECE_KG = 150           # online booking limits; heavier freight goes through a sales quote
MAX_SIDE_CM = 150
MAX_PIECES = 20
DOCUMENT_MAX_KG = 2.0
OVERSIZE_SIDE_CM = 100       # any side above this -> oversize piece surcharge
OVERWEIGHT_PIECE_KG = 70     # any piece above this -> overweight piece surcharge

# Express rate cards: zone -> (first 0.5 kg, each additional 0.5 kg up to 30 kg, per kg above 30 kg), USD export
EXPRESS_RATES = {
    0: (22.00, 2.40, 4.20),
    1: (45.00, 6.50, 11.00),
    2: (58.00, 8.50, 14.00),
    3: (64.00, 9.50, 15.50),
    4: (70.00, 10.50, 17.00),
    5: (66.00, 10.00, 16.00),
    6: (78.00, 12.00, 19.00),
    7: (85.00, 13.00, 21.00),
    8: (92.00, 14.50, 23.00),
}
SAME_DAY_RATES = (45.00, 3.00, 5.00)
ECONOMY_FACTOR = 0.72
DOCUMENT_FACTOR = 0.85       # international document (no customs value) rates
DIRECTION_FACTOR = {"domestic": 1.0, "export": 1.0, "import": 1.10, "third_country": 1.30}
TIME_DEFINITE_PREMIUM = {EXPRESS_9: (25.00, 45.00), EXPRESS_12: (12.00, 22.00)}  # (domestic, international)
TIME_DEFINITE_ZONES = {0, 1, 2, 3, 5}  # 9:00 / 12:00 offered to major markets only

OVERSIZE_FEE = 45.00         # per piece
OVERWEIGHT_FEE = 95.00       # per piece
ELEVATED_RISK_FEE = 30.00    # per shipment
DDP_FEE = 25.00              # Duty Tax Paid service fee (duties themselves are billed after clearance)
SIGNATURE_FEE = 7.50
INSURANCE_RATE, INSURANCE_MIN, INSURANCE_MAX_VALUE = 0.01, 15.00, 25000

# Business-day transit (min, max)
DOMESTIC_TRANSIT = {SAME_DAY: (0, 0), EXPRESS_9: (1, 1), EXPRESS_12: (1, 1), EXPRESS: (1, 1), ECONOMY: (2, 4)}
EXPRESS_TRANSIT = {1: (1, 2), 2: (1, 3), 3: (2, 3), 4: (2, 4), 5: (2, 3), 6: (2, 4), 7: (3, 5), 8: (3, 6)}
ECONOMY_EXTRA = (2, 4)
CUTOFF_HOUR = 17
SAME_DAY_CUTOFF_HOUR = 12


class QuoteError(ValueError):
    """Shipment can't be priced online (bad route, too heavy, embargoed destination...)."""


@dataclass
class Route:
    origin_country: str
    dest_country: str
    direction: str  # domestic | export | import | third_country
    zone: int

    @property
    def international(self):
        return self.direction != "domestic"


@dataclass
class Option:
    product: str
    available: bool
    reason: str = ""
    lines: list = field(default_factory=list)
    net: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    transit_days: tuple = (0, 0)
    ship_date: date | None = None
    delivery_date: date | None = None

    def as_dict(self):
        info = PRODUCT_INFO[self.product]
        lo, hi = self.transit_days
        if hi == 0:
            transit = "Today"
        elif lo == hi:
            transit = f"{hi} business day{'s' if hi > 1 else ''}"
        else:
            transit = f"{lo}–{hi} business days"
        return {
            "product": self.product, "code": info["code"], "icon": info["icon"], "blurb": info["blurb"],
            "available": self.available, "reason": self.reason, "lines": self.lines,
            "net": self.net, "tax": self.tax, "total": self.total, "transit": transit,
            "delivery_by": info["by"], "ship_date": _iso(self.ship_date), "delivery_date": _iso(self.delivery_date),
        }


def _iso(d):
    return d.isoformat() if d else None


def home_country():
    return settings.TRACKPRO["DOMESTIC_COUNTRY"]


def resolve_route(origin_country: str, dest_country: str) -> Route:
    o, d, home = (origin_country or "").upper(), (dest_country or "").upper(), home_country()
    for code in (o, d):
        if code in NOT_SERVED:
            raise QuoteError(f"We can't ship to or from {NOT_SERVED[code]} at the moment.")
        if code not in COUNTRIES:
            raise QuoteError("Choose a valid origin and destination country.")
    if o == d:
        if o != home:
            raise QuoteError(f"Domestic shipments within {country_name(o)} aren't available online. Contact us for a quote.")
        return Route(o, d, "domestic", 0)
    if o == home:
        return Route(o, d, "export", COUNTRIES[d]["zone"])
    if d == home:
        return Route(o, d, "import", COUNTRIES[o]["zone"])
    return Route(o, d, "third_country", max(COUNTRIES[o]["zone"], COUNTRIES[d]["zone"]))


def round_weight(kg: float) -> float:
    kg = max(kg, MIN_CHARGEABLE_KG)
    return math.ceil(kg * 2 - 1e-9) / 2 if kg <= 30 else float(math.ceil(kg - 1e-9))


def transport_charge(card, chargeable_kg: float) -> float:
    first, extra_half, per_kg = card
    w = min(chargeable_kg, 30)
    price = first + math.ceil((w - 0.5) * 2 - 1e-9) * extra_half
    if chargeable_kg > 30:
        price += (chargeable_kg - 30) * per_kg
    return price


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def earliest_ship_date(product: str, now: datetime | None = None) -> date:
    now = (now or datetime.now(ZoneInfo(settings.TRACKPRO["TIME_ZONE"])))
    cutoff = SAME_DAY_CUTOFF_HOUR if product == SAME_DAY else CUTOFF_HOUR
    d = now.date()
    if now.weekday() >= 5 or now.hour >= cutoff:
        d = add_business_days(d, 1)
    return d


@dataclass
class ShipmentSpec:
    origin_country: str
    dest_country: str
    pieces: int = 1
    weight_kg: float = 1          # per piece
    length_cm: float = 0
    width_cm: float = 0
    height_cm: float = 0
    is_document: bool = False
    insured_value: float = 0
    signature: bool = False
    duties_paid: bool = False     # DDP: shipper pays duties & taxes
    ship_date: date | None = None


def calc_quote(spec: ShipmentSpec, tax_rate: float | None = None, now: datetime | None = None) -> dict:
    """Price every product for the shipment. Raises QuoteError when it can't be shipped online at all."""
    tax_rate = settings.TRACKPRO["VAT_RATE"] if tax_rate is None else tax_rate
    route = resolve_route(spec.origin_country, spec.dest_country)
    pieces = int(spec.pieces or 1)
    dims = [max(float(v or 0), 0) for v in (spec.length_cm, spec.width_cm, spec.height_cm)]
    actual = max(float(spec.weight_kg or 0), 0)

    if not 1 <= pieces <= MAX_PIECES:
        raise QuoteError(f"Online bookings take 1 to {MAX_PIECES} pieces.")
    if actual > MAX_PIECE_KG:
        raise QuoteError(f"Each piece can weigh up to {MAX_PIECE_KG} kg online. Contact us for freight.")
    if max(dims) > MAX_SIDE_CM:
        raise QuoteError(f"No side can be longer than {MAX_SIDE_CM} cm online. Contact us for freight.")
    if spec.insured_value and spec.insured_value > INSURANCE_MAX_VALUE:
        raise QuoteError(f"Shipment value protection covers up to ${INSURANCE_MAX_VALUE:,.0f}.")

    volumetric = dims[0] * dims[1] * dims[2] / VOLUMETRIC_DIVISOR
    piece_chargeable = round_weight(max(actual, volumetric))
    chargeable = round_weight(piece_chargeable * pieces)
    is_document = bool(spec.is_document) and route.international
    if is_document and chargeable > DOCUMENT_MAX_KG:
        raise QuoteError(f"Document shipments can weigh up to {DOCUMENT_MAX_KG:g} kg. Send it as a package instead.")

    warnings = []
    if route.international and not is_document:
        warnings.append("Goods need a commercial invoice. Duties and taxes may apply at destination"
                        + (" and will be billed to you after clearance." if spec.duties_paid else " (paid by the receiver)."))

    options = [_price(p, route, chargeable, pieces, dims, actual, is_document, spec, tax_rate, now) for p in PRODUCTS]
    return {
        "route": {
            "origin_country": route.origin_country, "origin_name": country_name(route.origin_country),
            "dest_country": route.dest_country, "dest_name": country_name(route.dest_country),
            "direction": route.direction, "international": route.international,
            "zone": route.zone, "zone_name": ZONE_NAMES[route.zone],
        },
        "pieces": pieces,
        "actual_kg": round(actual * pieces, 2),
        "volumetric_kg": round(volumetric * pieces, 2),
        "chargeable_kg": chargeable,
        "is_document": is_document,
        "tax_rate": tax_rate,
        "warnings": warnings,
        "options": [o.as_dict() for o in options],
    }


def _price(product, route, chargeable, pieces, dims, actual, is_document, spec, tax_rate, now) -> Option:
    intl = route.international
    if product == SAME_DAY and intl:
        return Option(product, False, "Domestic only")
    if product == SAME_DAY and chargeable > 30:
        return Option(product, False, "Up to 30 kg")
    if product in TIME_DEFINITE_PREMIUM and route.zone not in TIME_DEFINITE_ZONES:
        return Option(product, False, f"Not offered to {ZONE_NAMES[route.zone]}")

    if product == SAME_DAY:
        transport = transport_charge(SAME_DAY_RATES, chargeable)
    else:
        transport = transport_charge(EXPRESS_RATES[route.zone], chargeable)
        if product == ECONOMY:
            transport *= ECONOMY_FACTOR
    transport *= DIRECTION_FACTOR[route.direction]
    if is_document:
        transport *= DOCUMENT_FACTOR

    lines = [{"code": "transport", "label": f"{product} transport ({chargeable:g} kg)", "amount": transport}]
    fuel_base = transport
    if product in TIME_DEFINITE_PREMIUM:
        premium = TIME_DEFINITE_PREMIUM[product][1 if intl else 0]
        lines.append({"code": "time_definite", "label": f"{PRODUCT_INFO[product]['by']} delivery premium", "amount": premium})
        fuel_base += premium
    fuel_pct = settings.TRACKPRO["FUEL_SURCHARGE"]
    if fuel_pct:
        lines.append({"code": "fuel", "label": f"Fuel surcharge ({fuel_pct * 100:g}%)", "amount": fuel_base * fuel_pct})
    if max(dims) > OVERSIZE_SIDE_CM:
        lines.append({"code": "oversize", "label": f"Oversize piece × {pieces}", "amount": OVERSIZE_FEE * pieces})
    if actual > OVERWEIGHT_PIECE_KG:
        lines.append({"code": "overweight", "label": f"Overweight piece × {pieces}", "amount": OVERWEIGHT_FEE * pieces})
    if intl and (route.dest_country in ELEVATED_RISK or route.origin_country in ELEVATED_RISK):
        lines.append({"code": "elevated_risk", "label": "Elevated risk destination", "amount": ELEVATED_RISK_FEE})
    if intl and spec.duties_paid and not is_document:
        lines.append({"code": "ddp", "label": "Duty Tax Paid (DDP) service", "amount": DDP_FEE})
    if spec.signature:
        lines.append({"code": "signature", "label": "Direct signature", "amount": SIGNATURE_FEE})
    if spec.insured_value and spec.insured_value > 0:
        lines.append({"code": "insurance", "label": f"Shipment value protection (${spec.insured_value:,.2f})",
                      "amount": max(spec.insured_value * INSURANCE_RATE, INSURANCE_MIN)})

    for ln in lines:
        ln["amount"] = round(ln["amount"], 2)
    net = round(sum(ln["amount"] for ln in lines), 2)
    tax = round(net * tax_rate, 2)

    if intl:
        lo, hi = EXPRESS_TRANSIT[route.zone]
        if product == ECONOMY:
            lo, hi = lo + ECONOMY_EXTRA[0], hi + ECONOMY_EXTRA[1]
    else:
        lo, hi = DOMESTIC_TRANSIT[product]
    first = earliest_ship_date(product, now)
    ship = spec.ship_date if spec.ship_date and spec.ship_date >= first else first
    return Option(product, True, "", lines, net, tax, round(net + tax, 2), (lo, hi), ship, add_business_days(ship, hi))


def option_for(quote: dict, product: str) -> dict:
    product = LEGACY_PRODUCTS.get(product, product)
    for o in quote["options"]:
        if o["product"] == product:
            if not o["available"]:
                raise QuoteError(f"{product} isn't available for this shipment: {o['reason']}.")
            return o
    raise QuoteError(f"Unknown product: {product}")


def starting_prices() -> dict:
    """'From' price per product (0.5 kg, cheapest lane) for marketing pages."""
    fuel = 1 + settings.TRACKPRO["FUEL_SURCHARGE"]
    return {
        SAME_DAY: round(SAME_DAY_RATES[0] * fuel, 2),
        EXPRESS_9: round((EXPRESS_RATES[0][0] + TIME_DEFINITE_PREMIUM[EXPRESS_9][0]) * fuel, 2),
        EXPRESS_12: round((EXPRESS_RATES[0][0] + TIME_DEFINITE_PREMIUM[EXPRESS_12][0]) * fuel, 2),
        EXPRESS: round(EXPRESS_RATES[0][0] * fuel, 2),
        ECONOMY: round(EXPRESS_RATES[0][0] * ECONOMY_FACTOR * fuel, 2),
        "international": round(EXPRESS_RATES[1][0] * ECONOMY_FACTOR * fuel, 2),
    }
