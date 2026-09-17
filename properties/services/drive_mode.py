import math
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.db.models import Exists, OuterRef, Q, Subquery

from properties.models import Listing, ListingImage, Property, PropertyLocation
from properties.services.data_quality import USD_PRICE_RANGE
from properties.services.spatial import haversine_km, radius_bbox, rtree_property_ids


DEFAULT_RADIUS_M = 350
MIN_RADIUS_M = 200
MAX_RADIUS_M = 1500
MAX_RESULTS = 250
ALERT_GROUP_THRESHOLD = 5
MAX_IMAGE_URL_LENGTH = 2000
MAX_AREA_M2 = Decimal("100000")
MAX_BEDROOMS = 12
PROPERTY_TYPE_LABELS = dict(Property.Type.choices)


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


def _bounded_decimal(payload, key, maximum):
    number = _decimal(payload, key)
    if number is not None and number > maximum:
        raise DriveModeValidationError(f"{key} esta fuera de rango.")
    return number


def _bounded_integer(payload, key, maximum):
    number = _decimal(payload, key)
    if number is None:
        return None
    if number != number.to_integral_value() or number > maximum:
        raise DriveModeValidationError(f"{key} debe ser un entero entre 0 y {maximum}.")
    return int(number)


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

    price_min = _bounded_decimal(payload, "price_min", USD_PRICE_RANGE[1])
    price_max = _bounded_decimal(payload, "price_max", USD_PRICE_RANGE[1])
    if price_min is not None and price_max is not None and price_min > price_max:
        raise DriveModeValidationError("price_min no puede ser mayor que price_max.")

    price_currency = str(payload.get("price_currency") or "USD").strip().upper()
    if price_currency != "USD":
        raise DriveModeValidationError("price_currency debe ser USD.")

    bedrooms_min = _bounded_integer(payload, "bedrooms_min", MAX_BEDROOMS)
    covered_area_min_m2 = _bounded_decimal(
        payload,
        "covered_area_min_m2",
        MAX_AREA_M2,
    )
    land_area_min_m2 = _bounded_decimal(
        payload,
        "land_area_min_m2",
        MAX_AREA_M2,
    )

    return {
        "latitude": latitude,
        "longitude": longitude,
        "radius_m": radius_m,
        "property_types": property_types,
        "price_currency": price_currency,
        "price_min": price_min,
        "price_max": price_max,
        "bedrooms_min": bedrooms_min,
        "covered_area_min_m2": covered_area_min_m2,
        "land_area_min_m2": land_area_min_m2,
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


def _location_label(reliability):
    return {
        "confirmed": "Ubicación confirmada manualmente",
        "address": "Ubicación calculada por dirección",
        "published": "Ubicación publicada; puede ser aproximada",
    }[reliability]


def _address_payload(property_obj):
    canonical = (property_obj.address or "").strip()
    detected = (property_obj.detected_address or "").strip()
    manual_overrides = property_obj.manual_overrides or {}
    if canonical:
        reliability = "confirmed" if "address" in manual_overrides else "published"
        text = canonical
    elif detected:
        reliability = "detected"
        text = detected
    else:
        reliability = "unavailable"
        text = ""
    labels = {
        "confirmed": "Dirección confirmada manualmente",
        "published": "Dirección publicada; puede ser aproximada",
        "detected": "Dirección detectada en el aviso; puede ser aproximada",
        "unavailable": "Dirección no publicada",
    }
    return {
        "address_text": text,
        "address_reliability": reliability,
        "address_label": labels[reliability],
    }


def _safe_https_url(absolute):
    absolute = (absolute or "").strip()
    if len(absolute) > MAX_IMAGE_URL_LENGTH:
        return ""
    try:
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or (port is not None and not 1 <= port <= 65535)
    ):
        return ""
    netloc = parsed.hostname
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _safe_image_url(raw_url, listing_url):
    raw_url = (raw_url or "").strip()
    listing_url = (listing_url or "").strip()
    if not raw_url:
        return ""
    return _safe_https_url(urljoin(listing_url, raw_url))


def _area_m2(property_obj):
    value = property_obj.covered_area or property_obj.total_area or property_obj.land_area
    return float(value) if value is not None else None


def _truncate_grouped_properties(group_items, limit=MAX_RESULTS):
    selected = []
    truncated = False
    for items in group_items:
        remaining = limit - len(selected)
        if remaining <= 0:
            truncated = True
            break
        if len(items) <= remaining:
            selected.extend(items)
            continue
        truncated = True
        if len(items) >= ALERT_GROUP_THRESHOLD and not selected:
            selected.extend(items[:remaining])
        break
    returned_counts = defaultdict(int)
    for item in selected:
        returned_counts[item["group_id"]] += 1
    for item in selected:
        returned = returned_counts[item["group_id"]]
        item["group_returned_count"] = returned
        item["group_truncated"] = returned < item["group_count"]
    return selected, truncated


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
        .filter(Exists(active_listing))
        .order_by("pk")
    )
    if query["property_types"]:
        queryset = queryset.filter(property_type__in=query["property_types"])
    if query["price_min"] is not None:
        queryset = queryset.filter(
            currency=query["price_currency"],
            price__gte=query["price_min"],
        )
    if query["price_max"] is not None:
        queryset = queryset.filter(
            currency=query["price_currency"],
            price__lte=query["price_max"],
        )
    if query["bedrooms_min"] is not None:
        queryset = queryset.filter(bedrooms__gte=query["bedrooms_min"])
    if query["covered_area_min_m2"] is not None:
        queryset = queryset.filter(covered_area__gte=query["covered_area_min_m2"])
    if query["land_area_min_m2"] is not None:
        queryset = queryset.filter(land_area__gte=query["land_area_min_m2"])
    return queryset


def _with_card_image(queryset):
    image_queryset = ListingImage.objects.filter(
        listing__property_id=OuterRef("pk"),
        listing__active=True,
    ).exclude(
        url__istartswith="http://",
    ).exclude(
        url__istartswith="javascript:",
    ).exclude(
        url__istartswith="data:",
    ).exclude(
        url__istartswith="file:",
    ).exclude(
        url__istartswith="ftp:",
    ).order_by(
        "-listing__last_seen_at",
        "listing_id",
        "position",
        "pk",
    )
    fallback_listing = Listing.objects.filter(
        property_id=OuterRef("pk"),
        active=True,
        url__istartswith="https://",
    ).order_by("-last_seen_at", "pk")
    return queryset.annotate(
        drive_image_raw=Subquery(image_queryset.values("url")[:1]),
        drive_image_listing_url=Subquery(image_queryset.values("listing__url")[:1]),
        drive_image_source_name=Subquery(
            image_queryset.values("listing__source__name")[:1]
        ),
        drive_fallback_listing_url=Subquery(fallback_listing.values("url")[:1]),
        drive_fallback_source_name=Subquery(
            fallback_listing.values("source__name")[:1]
        ),
    )


def drive_property_card(property_id):
    query = {
        "property_types": [],
        "price_currency": "USD",
        "price_min": None,
        "price_max": None,
        "bedrooms_min": None,
        "covered_area_min_m2": None,
        "land_area_min_m2": None,
    }
    property_obj = _with_card_image(
        _eligible_queryset([property_id], query)
        .select_related("location")
        .only(
            "id",
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
            "manual_overrides",
            "is_favorite",
            "location__precision",
            "location__provider",
            "location__query",
            "location__manually_corrected",
        )
    ).first()
    if property_obj is None:
        return None
    location_reliability = _location_reliability(property_obj)
    preferred_original_url = _safe_https_url(property_obj.drive_image_listing_url)
    if preferred_original_url:
        original_url = preferred_original_url
        original_source_name = property_obj.drive_image_source_name or ""
    else:
        original_url = _safe_https_url(property_obj.drive_fallback_listing_url)
        original_source_name = property_obj.drive_fallback_source_name or ""
    result = {
        "id": property_obj.pk,
        "price_short": _price_short(property_obj.currency, property_obj.price),
        "type": property_obj.property_type,
        "type_label": property_obj.get_property_type_display(),
        "bedrooms": property_obj.bedrooms,
        "bathrooms": (
            float(property_obj.bathrooms)
            if property_obj.bathrooms is not None
            else None
        ),
        "area_m2": _area_m2(property_obj),
        "covered_area_m2": (
            float(property_obj.covered_area)
            if property_obj.covered_area is not None
            else None
        ),
        "land_area_m2": (
            float(property_obj.land_area)
            if property_obj.land_area is not None
            else None
        ),
        "location_reliability": location_reliability,
        "location_label": _location_label(location_reliability),
        "image_url": _safe_image_url(
            property_obj.drive_image_raw,
            property_obj.drive_image_listing_url,
        ),
        "original_url": original_url,
        "original_host": urlsplit(original_url).hostname if original_url else "",
        "original_source_name": original_source_name,
        "is_favorite": property_obj.is_favorite,
    }
    result.update(_address_payload(property_obj))
    return result


def nearby_drive_properties(payload):
    query = parse_drive_query(payload)
    radius_km = query["radius_m"] / 1000
    candidate_ids = rtree_property_ids(
        *radius_bbox(query["latitude"], query["longitude"], radius_km)
    )
    properties = []
    nearby_rows = _eligible_queryset(candidate_ids, query).values(
        "id",
        "property_type",
        "currency",
        "price",
        "location__latitude",
        "location__longitude",
    )
    for row in nearby_rows:
        latitude = row["location__latitude"]
        longitude = row["location__longitude"]
        distance_m = round(
            haversine_km(
                query["latitude"],
                query["longitude"],
                latitude,
                longitude,
            )
            * 1000
        )
        if distance_m > query["radius_m"]:
            continue
        group_id = f"{latitude:.6f},{longitude:.6f}"
        properties.append(
            {
                "id": row["id"],
                "latitude": latitude,
                "longitude": longitude,
                "distance_m": distance_m,
                "currency": row["currency"],
                "price": float(row["price"]),
                "price_short": _price_short(row["currency"], row["price"]),
                "type": row["property_type"],
                "type_label": PROPERTY_TYPE_LABELS.get(
                    row["property_type"],
                    "Otro",
                ),
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

    properties, truncated = _truncate_grouped_properties(list(groups.values()))
    return {
        "center": {
            "latitude": query["latitude"],
            "longitude": query["longitude"],
        },
        "radius_m": query["radius_m"],
        "applied_filters": {
            "property_types": query["property_types"],
            "price_currency": query["price_currency"],
            "price_min": float(query["price_min"]) if query["price_min"] is not None else None,
            "price_max": float(query["price_max"]) if query["price_max"] is not None else None,
            "bedrooms_min": query["bedrooms_min"],
            "covered_area_min_m2": (
                float(query["covered_area_min_m2"])
                if query["covered_area_min_m2"] is not None
                else None
            ),
            "land_area_min_m2": (
                float(query["land_area_min_m2"])
                if query["land_area_min_m2"] is not None
                else None
            ),
        },
        "count": len(properties),
        "truncated": truncated,
        "properties": properties,
    }
