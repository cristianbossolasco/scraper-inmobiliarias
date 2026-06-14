from dataclasses import dataclass
from decimal import Decimal
import re
from urllib.parse import urlparse

from properties.models import Property
from properties.services.normalization import fold_text


@dataclass(frozen=True)
class QualityResult:
    field: str
    value: object
    valid: bool
    reason: str = ""
    category: str = "range"


BASE_RANGES = {
    "rooms": (0, 15),
    "bedrooms": (0, 12),
    "bathrooms": (0, 10),
    "garages": (0, 8),
    "covered_area": (10, 3000),
    "land_area": (20, 10000),
    "total_area": (20, 10000),
}

LARGE_LAND_TYPES = {Property.Type.LAND, Property.Type.COUNTRY_HOUSE, Property.Type.OTHER}
USD_PRICE_RANGE = (1000, 5000000)
TREND_MIN_COMPARABLES = 5


def folded_text(property_obj):
    return " ".join(
        [
            fold_text(property_obj.title or ""),
            fold_text(property_obj.description or ""),
            fold_text(property_obj.property_type or ""),
        ]
    )


def folded_title(property_obj):
    return fold_text(property_obj.title or "")


def is_garage_like(property_obj):
    title = folded_title(property_obj)
    if not re.search(r"\b(cochera|garage|garaje|cocheras)\b", title):
        return False
    residential_terms = r"\b(casa|chalet|depto|departamento|ph|duplex|dúplex|triplex|monoambiente|ambientes?)\b"
    return not re.search(residential_terms, title)


def is_large_commercial_like(property_obj):
    return bool(re.search(r"\b(galpon|galpón|deposito|depósito|nave|fraccion|fracción|industrial)\b", folded_text(property_obj)))


def is_listing_url(url):
    path = urlparse(url or "").path.lower().rstrip("/")
    return bool(
        re.search(r"/inmuebles(?:-|$)|/inmuebles-[^/]+\.html$|/pagina-\d+|/ciudad/|/tipo-de-propiedad/", path)
    )


def is_rental_url(url):
    path = urlparse(url or "").path.lower()
    return bool(re.search(r"\balquiler\b|/alquiler-|/alquiler/|/alcl", path))


def has_listing_page_url(property_obj):
    return any(is_listing_url(listing.url) for listing in property_obj.listings.all())


def numeric(value):
    if value in (None, ""):
        return None
    return float(value)


def range_for(property_obj, field):
    if is_garage_like(property_obj) and field in {"covered_area", "land_area", "total_area"}:
        return 8, 100
    if is_large_commercial_like(property_obj) and field in {"covered_area", "land_area", "total_area"}:
        return 10, 100000
    if field in {"land_area", "total_area"} and property_obj.property_type in LARGE_LAND_TYPES:
        return 20, 100000
    return BASE_RANGES[field]


def validate_field(property_obj, field):
    value = getattr(property_obj, field)
    parsed = numeric(value)
    if parsed is None:
        return QualityResult(field, value, False, "sin dato")
    minimum, maximum = range_for(property_obj, field)
    if not minimum <= parsed <= maximum:
        return QualityResult(field, value, False, f"fuera de rango {minimum}-{maximum}")
    return QualityResult(field, value, True)


def valid_value(property_obj, field):
    result = validate_field(property_obj, field)
    return result.value if result.valid else None


def valid_price(property_obj):
    if property_obj.price is None:
        return None
    if property_obj.currency == "USD":
        price = float(property_obj.price)
        if USD_PRICE_RANGE[0] <= price <= USD_PRICE_RANGE[1]:
            return property_obj.price
        return None
    return property_obj.price


def valid_area(property_obj):
    for field in ("covered_area", "total_area", "land_area"):
        value = valid_value(property_obj, field)
        if value:
            return value
    return None


def valid_comparable_area(property_obj):
    if property_obj.property_type == Property.Type.LAND:
        return valid_value(property_obj, "land_area") or valid_value(property_obj, "total_area")
    for field in ("covered_area", "total_area"):
        value = valid_value(property_obj, field)
        if value:
            return value
    return valid_value(property_obj, "land_area")


def age_band_key(age_years):
    if age_years is None:
        return "unknown"
    try:
        age = int(age_years)
    except (TypeError, ValueError):
        return "unknown"
    if age <= 5:
        return "0-5"
    if age <= 20:
        return "6-20"
    if age <= 40:
        return "21-40"
    return "41+"


def age_band_label(age_years):
    labels = {
        "0-5": "0-5 anos",
        "6-20": "6-20 anos",
        "21-40": "21-40 anos",
        "41+": "41+ anos",
        "unknown": "antiguedad sin dato",
    }
    return labels[age_band_key(age_years)]


def comparable_group_key(property_obj):
    return (
        property_obj.property_type or Property.Type.OTHER,
        property_obj.condition_category or Property.ConditionCategory.UNKNOWN,
        age_band_key(property_obj.age_years),
    )


def valid_price_per_m2(property_obj):
    price = valid_price(property_obj)
    area = valid_area(property_obj)
    if property_obj.currency != "USD" or price is None or not area:
        return None
    return (Decimal(price) / Decimal(area)).quantize(Decimal("0.01"))


def property_anomalies(property_obj):
    anomalies = []
    if property_obj.operation and property_obj.operation != "sale":
        anomalies.append(
            QualityResult(
                "operation",
                property_obj.operation,
                False,
                "no es venta",
                "operation",
            )
        )
    if has_listing_page_url(property_obj):
        anomalies.append(
            QualityResult(
                "url",
                property_obj.primary_listing.url if property_obj.primary_listing else "",
                False,
                "parece pagina de listado, no ficha",
                "listing_page",
            )
        )
    for field in BASE_RANGES:
        result = validate_field(property_obj, field)
        if result.value not in (None, "") and not result.valid:
            anomalies.append(result)
    if property_obj.currency == "USD" and property_obj.price is not None and valid_price(property_obj) is None:
        anomalies.append(
            QualityResult(
                "price",
                property_obj.price,
                False,
                f"fuera de rango {USD_PRICE_RANGE[0]}-{USD_PRICE_RANGE[1]}",
                "price",
            )
        )
    for listing in property_obj.listings.all():
        if listing.source_status == "metric_conflict_review":
            conflicts = (listing.raw_data or {}).get("guarnieri_metric_conflicts") or []
            reason = "tabla estructurada contradice descripcion"
            if conflicts:
                fields = ", ".join(sorted({item.get("field", "dato") for item in conflicts}))
                reason = f"{reason}: {fields}"
            anomalies.append(
                QualityResult(
                    "source_status",
                    listing.source_status,
                    False,
                    reason,
                    "source_conflict",
                )
            )
    return anomalies


def curated_metric_values(properties, field):
    return [valid_value(item, field) for item in properties if valid_value(item, field) is not None]


def curated_price_values(properties):
    return [valid_price(item) for item in properties if valid_price(item) is not None]


def curated_price_m2_values(properties):
    return [valid_price_per_m2(item) for item in properties if valid_price_per_m2(item) is not None]
