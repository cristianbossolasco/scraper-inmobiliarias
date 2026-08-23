import math
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.db.models import Exists, OuterRef, Q

from properties.models import Listing, Property, PropertyLocation
from properties.services.data_quality import USD_PRICE_RANGE
from properties.services.spatial import haversine_km, radius_bbox, rtree_property_ids


DEFAULT_RADIUS_M = 350
MIN_RADIUS_M = 200
MAX_RADIUS_M = 1000
MAX_RESULTS = 250
ALERT_GROUP_THRESHOLD = 5


class DriveModeValidationError(ValueError):
    pass


def _finite_number(payload, key, *, required=False):
    value = payload.get(key)
    if value in (None, ""):
        if required:
            raise DriveModeValidationError(f"{key} es obligatorio.")
        return None
    if isinstance(value, bool):
        raise DriveModeValidationError(f"{key} debe ser numerico.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DriveModeValidationError(f"{key} debe ser numerico.") from None
    if not math.isfinite(number):
        raise DriveModeValidationError(f"{key} debe ser finito.")
    return number


def _decimal(payload, key):
    value = payload.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise DriveModeValidationError(f"{key} debe ser numerico.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DriveModeValidationError(f"{key} debe ser numerico.") from None
    if not number.is_finite() or number < 0:
        raise DriveModeValidationError(f"{key} debe ser un numero positivo.")
    return number


def parse_drive_query(payload):
    if not isinstance(payload, dict):
        raise DriveModeValidationError("El cuerpo JSON debe ser un objeto.")

    latitude = _finite_number(payload, "latitude", required=True)
    longitude = _finite_number(payload, "longitude", required=True)
    if not -90 <= latitude <= 90:
        raise DriveModeValidationError("latitude esta fuera de rango.")
    if not -180 <= longitude <= 180:
        raise DriveModeValidationError("longitude esta fuera de rango.")

    radius_value = payload.get("radius_m", DEFAULT_RADIUS_M)
    if isinstance(radius_value, bool):
        raise DriveModeValidationError("radius_m debe ser numerico.")
    try:
        radius_m = int(radius_value)
    except (TypeError, ValueError):
        raise DriveModeValidationError("radius_m debe ser numerico.") from None
    if not MIN_RADIUS_M <= radius_m <= MAX_RADIUS_M:
        raise DriveModeValidationError(
            f"radius_m debe estar entre {MIN_RADIUS_M} y {MAX_RADIUS_M}."
        )

    property_types = payload.get("property_types") or []
    if not isinstance(property_types, list) or any(
        not isinstance(item, str) for item in property_types
    ):
        raise DriveModeValidationError("property_types debe ser una lista de textos.")
    allowed_types = {value for value, _label in Property.Type.choices}
    property_types = list(dict.fromkeys(property_types))
    invalid_types = sorted(set(property_types) - allowed_types)
    if invalid_types:
        raise DriveModeValidationError(
            f"Tipos de propiedad invalidos: {', '.join(invalid_types)}."
        )

    price_min = _decimal(payload, "price_min")
    price_max = _decimal(payload, "price_max")
    if price_min is not None and price_max is not None and price_min > price_max:
        raise DriveModeValidationError("price_min no puede ser mayor que price_max.")

    return {
        "latitude": latitude,
        "longitude": longitude,
        "radius_m": radius_m,
        "property_types": property_types,
        "price_min": price_min,
        "price_max": price_max,
    }


def _price_short(currency, value):
    if value is None:
        return "Consultar"
    number = Decimal(value)
    absolute = abs(number)
    if absolute >= Decimal("1000000"):
        compact = number / Decimal("1000000")
        rendered = f"{compact:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        suffix = "M"
    elif absolute >= Decimal("1000"):
        compact = number / Decimal("1000")
        rendered = f"{compact:.0f}"
        suffix = "k"
    else:
        rendered = f"{number:.0f}"
        suffix = ""
    return f"{currency} {rendered}{suffix}".strip()


def _location_reliability(property_obj):
    location = property_obj.location
    if (
        location.precision == PropertyLocation.Precision.MANUAL
        or location.manually_corrected
        or location.provider == "manual"
    ):
        return "confirmed"
    address_evidence = " ".join(
        [
            property_obj.address or "",
            property_obj.detected_address or "",
            location.query or "",
        ]
    )
    if location.provider == "nominatim" and re.search(r"\b\d{1,6}\b", address_evidence):
        return "address"
    return "published"


def _area_m2(property_obj):
    value = property_obj.covered_area or property_obj.total_area or property_obj.land_area
    return float(value) if value is not None else None


def _eligible_queryset(candidate_ids, query):
    active_listing = Listing.objects.filter(property_id=OuterRef("pk"), active=True)
    valid_price_filter = Q(
        currency="USD",
        price__gte=USD_PRICE_RANGE[0],
        price__lte=USD_PRICE_RANGE[1],
    ) | (~Q(currency="USD") & Q(price__gt=0))
    confidence_filter = (
        Q(location__precision=PropertyLocation.Precision.MANUAL)
        | Q(location__manually_corrected=True)
        | Q(location_confidence__in=[Property.LocationConfidence.HIGH, Property.LocationConfidence.MEDIUM])
    )
    queryset = (
        Property.objects.filter(
            pk__in=candidate_ids,
            operation="sale",
            status=Property.Status.ACTIVE,
            is_hidden=False,
            location__outside_target=False,
            location__precision__in=[
                PropertyLocation.Precision.EXACT,
                PropertyLocation.Precision.MANUAL,
            ],
        )
        .filter(valid_price_filter)
        .filter(confidence_filter)
        .annotate(has_active_listing=Exists(active_listing))
        .filter(has_active_listing=True)
        .select_related("location")
        .only(
            "id",
            "title",
            "property_type",
            "currency",
            "price",
            "bedrooms",
            "bathrooms",
            "covered_area",
            "total_area",
            "land_area",
            "address",
            "detected_address",
            "location_confidence",
            "is_favorite",
            "location__latitude",
            "location__longitude",
            "location__precision",
            "location__provider",
            "location__query",
            "location__manually_corrected",
        )
        .order_by("pk")
    )
    if query["property_types"]:
        queryset = queryset.filter(property_type__in=query["property_types"])
    if query["price_min"] is not None:
        queryset = queryset.filter(price__gte=query["price_min"])
    if query["price_max"] is not None:
        queryset = queryset.filter(price__lte=query["price_max"])
    return queryset


def nearby_drive_properties(payload):
    query = parse_drive_query(payload)
    radius_km = query["radius_m"] / 1000
    candidate_ids = rtree_property_ids(
        *radius_bbox(query["latitude"], query["longitude"], radius_km)
    )
    properties = []
    for property_obj in _eligible_queryset(candidate_ids, query):
        distance_m = round(
            haversine_km(
                query["latitude"],
                query["longitude"],
                property_obj.location.latitude,
                property_obj.location.longitude,
            )
            * 1000
        )
        if distance_m > query["radius_m"]:
            continue
        group_id = (
            f"{property_obj.location.latitude:.6f},"
            f"{property_obj.location.longitude:.6f}"
        )
        properties.append(
            {
                "id": property_obj.pk,
                "latitude": property_obj.location.latitude,
                "longitude": property_obj.location.longitude,
                "distance_m": distance_m,
                "currency": property_obj.currency,
                "price": float(property_obj.price),
                "price_short": _price_short(property_obj.currency, property_obj.price),
                "type": property_obj.property_type,
                "type_label": property_obj.get_property_type_display(),
                "bedrooms": property_obj.bedrooms,
                "bathrooms": float(property_obj.bathrooms) if property_obj.bathrooms is not None else None,
                "area_m2": _area_m2(property_obj),
                "location_reliability": _location_reliability(property_obj),
                "is_favorite": property_obj.is_favorite,
                "group_id": group_id,
            }
        )

    properties.sort(key=lambda item: (item["distance_m"], item["id"]))
    groups = defaultdict(list)
    for item in properties:
        groups[item["group_id"]].append(item)
    for group_items in groups.values():
        group_count = len(group_items)
        group_suspicious = group_count >= ALERT_GROUP_THRESHOLD
        currencies = {item["currency"] for item in group_items}
        group_price_short = ""
        if len(currencies) == 1:
            minimum = min(group_items, key=lambda item: item["price"])
            group_price_short = minimum["price_short"]
        for item in group_items:
            item["group_count"] = group_count
            item["group_suspicious"] = group_suspicious
            item["group_price_short"] = group_price_short

    truncated = len(properties) > MAX_RESULTS
    properties = properties[:MAX_RESULTS]
    return {
        "center": {
            "latitude": query["latitude"],
            "longitude": query["longitude"],
        },
        "radius_m": query["radius_m"],
        "count": len(properties),
        "truncated": truncated,
        "properties": properties,
    }
