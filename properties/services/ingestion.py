import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from properties.models import (
    Agency,
    Listing,
    ListingImage,
    ListingSnapshot,
    Property,
    PropertyLocation,
)
from .location_enrichment import enrich_location_data
from .agency_normalization import normalize_agency_name
from .normalization import (
    build_fingerprint,
    infer_property_type,
    is_plausible_property_address,
    known_neighborhood_name,
    locality_from_neighborhood,
    normalize_address,
    normalize_locality,
    normalize_neighborhood_name,
    normalize_street_number_address,
    normalize_whitespace,
    parse_decimal,
)


PROPERTY_FIELDS = (
    "property_type",
    "operation",
    "title",
    "description",
    "address",
    "normalized_address",
    "locality",
    "neighborhood",
    "currency",
    "price",
    "rooms",
    "bedrooms",
    "bathrooms",
    "garages",
    "toilets",
    "covered_area",
    "total_area",
    "land_area",
    "uncovered_area",
    "semicovered_area",
    "front_width",
    "lot_depth",
    "building_floors",
    "age_years",
    "features",
    "status",
    "detected_locality",
    "detected_neighborhood",
    "detected_address",
    "detected_latitude",
    "detected_longitude",
    "location_source",
    "location_confidence",
    "location_notes",
    "location_evidence",
)


DECIMAL_LIMITS = {
    "price": Decimal("99999999999999.99"),
    "bathrooms": Decimal("99.9"),
    "covered_area": Decimal("99999999.99"),
    "total_area": Decimal("99999999.99"),
    "land_area": Decimal("99999999.99"),
    "uncovered_area": Decimal("99999999.99"),
    "semicovered_area": Decimal("99999999.99"),
    "front_width": Decimal("999999.99"),
    "lot_depth": Decimal("999999.99"),
}


def _snapshot_payload(data):
    return {
        key: str(data.get(key)) if isinstance(data.get(key), Decimal) else data.get(key)
        for key in PROPERTY_FIELDS
    }


def _bounded_decimal(field, value):
    parsed = parse_decimal(value)
    if parsed is None:
        return None
    limit = DECIMAL_LIMITS[field]
    if abs(parsed) > limit:
        return None
    return parsed


def manual_override_fields(property_obj):
    overrides = property_obj.manual_overrides if isinstance(property_obj.manual_overrides, dict) else {}
    fields = {field for field in overrides if field in PROPERTY_FIELDS}
    if "address" in fields:
        fields.add("normalized_address")
    return fields


def canonicalize_listing_data(data, source=None):
    data = enrich_location_data(data)
    raw_locality = data.get("locality") or ""
    locality_zone = known_neighborhood_name(raw_locality)
    data["address"] = normalize_street_number_address(data.get("address"))
    if not is_plausible_property_address(data.get("address")):
        data["address"] = ""
    data["detected_address"] = normalize_street_number_address(data.get("detected_address"))
    if not is_plausible_property_address(data.get("detected_address")):
        data["detected_address"] = ""
    data["locality"] = normalize_locality(raw_locality)
    data["neighborhood"] = normalize_neighborhood_name(data.get("neighborhood"))
    data["detected_neighborhood"] = normalize_neighborhood_name(data.get("detected_neighborhood"))
    if not data["locality"] and locality_zone:
        if not data["neighborhood"]:
            data["neighborhood"] = locality_zone
        if not data["detected_neighborhood"]:
            data["detected_neighborhood"] = locality_zone
        data["locality"] = locality_from_neighborhood(locality_zone)
    if not data["locality"]:
        data["locality"] = "Hurlingham"
        if raw_locality:
            note = f"Localidad descartada por normalizacion: {raw_locality}"
            data["location_notes"] = normalize_whitespace(
                " ".join([data.get("location_notes") or "", note])
            )
    data["normalized_address"] = normalize_address(data.get("address")) if data.get("address") else ""
    data["property_type"] = data.get("property_type") or infer_property_type(
        data.get("title"), data.get("description")
    )
    for numeric_field in (
        "price",
        "covered_area",
        "total_area",
        "land_area",
        "uncovered_area",
        "semicovered_area",
        "front_width",
        "lot_depth",
        "bathrooms",
    ):
        data[numeric_field] = _bounded_decimal(numeric_field, data.get(numeric_field))
    if source is not None:
        data["fingerprint"] = build_fingerprint(data, source=source)
    return data


@transaction.atomic
def ingest_listing(source, data):
    data = canonicalize_listing_data(data, source=source)

    listing = Listing.objects.filter(
        source=source, external_id=str(data["external_id"])
    ).select_related("property").first()
    created = listing is None
    if listing:
        property_obj = listing.property
    else:
        property_obj = Property.objects.filter(fingerprint=data["fingerprint"]).first()
        if not property_obj:
            property_obj = Property.objects.create(
                fingerprint=data["fingerprint"],
                property_type=data["property_type"],
                operation=data.get("operation") or "sale",
                title=data.get("title") or "Propiedad sin título",
                description=data.get("description") or "",
                address=data.get("address") or "",
                normalized_address=data["normalized_address"],
                locality=data["locality"],
                neighborhood=data.get("neighborhood") or "",
                currency=data.get("currency") or "",
                price=data.get("price"),
                rooms=data.get("rooms"),
                bedrooms=data.get("bedrooms"),
                bathrooms=data.get("bathrooms"),
                garages=data.get("garages"),
                toilets=data.get("toilets"),
                covered_area=data.get("covered_area"),
                total_area=data.get("total_area"),
                land_area=data.get("land_area"),
                uncovered_area=data.get("uncovered_area"),
                semicovered_area=data.get("semicovered_area"),
                front_width=data.get("front_width"),
                lot_depth=data.get("lot_depth"),
                building_floors=data.get("building_floors"),
                age_years=data.get("age_years"),
                features=data.get("features") or [],
                status=data.get("status") or Property.Status.ACTIVE,
                detected_locality=data.get("detected_locality") or "",
                detected_neighborhood=data.get("detected_neighborhood") or "",
                detected_address=data.get("detected_address") or "",
                detected_latitude=data.get("detected_latitude"),
                detected_longitude=data.get("detected_longitude"),
                location_source=data.get("location_source") or Property.LocationSource.UNKNOWN,
                location_confidence=data.get("location_confidence") or Property.LocationConfidence.UNKNOWN,
                location_notes=data.get("location_notes") or "",
                location_evidence=data.get("location_evidence") or {},
            )

    protected_fields = manual_override_fields(property_obj)
    for field in PROPERTY_FIELDS:
        if field in protected_fields:
            continue
        value = data.get(field)
        if value not in (None, "", []):
            setattr(property_obj, field, value)
    property_obj.last_seen_at = timezone.now()
    property_obj.save()

    agency = None
    agency_name = normalize_agency_name(data.get("agency"))
    if agency_name:
        agency, _ = Agency.objects.get_or_create(
            name=agency_name,
            defaults={"website": data.get("agency_url") or ""},
        )
    listing, _ = Listing.objects.update_or_create(
        source=source,
        external_id=str(data["external_id"]),
        defaults={
            "property": property_obj,
            "agency": agency,
            "url": data["url"],
            "source_status": data.get("source_status") or "",
            "active": True,
            "missing_runs": 0,
            "raw_data": data.get("raw_data") or {},
        },
    )

    for position, image_url in enumerate(dict.fromkeys(data.get("images") or [])):
        ListingImage.objects.get_or_create(
            listing=listing, url=image_url, defaults={"position": position}
        )

    payload = _snapshot_payload(data)
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    ListingSnapshot.objects.get_or_create(
        listing=listing,
        content_hash=content_hash,
        defaults={
            "price": data.get("price"),
            "currency": data.get("currency") or "",
            "status": data.get("status") or "",
            "payload": payload,
        },
    )

    if data.get("latitude") is not None and data.get("longitude") is not None:
        current = getattr(property_obj, "location", None)
        if not current or not current.manually_corrected:
            PropertyLocation.objects.update_or_create(
                property=property_obj,
                defaults={
                    "latitude": float(data["latitude"]),
                    "longitude": float(data["longitude"]),
                    "precision": data.get("location_precision") or "exact",
                    "query": data.get("address") or "",
                    "provider": source.slug,
                    "confidence": 1,
                    "outside_target": False,
                },
            )
    return listing, created


def mark_missing(source, seen_external_ids):
    stale = Listing.objects.filter(source=source, active=True).exclude(
        external_id__in=seen_external_ids
    )
    for listing in stale:
        listing.missing_runs += 1
        if listing.missing_runs >= 2:
            listing.active = False
            listing.property.status = Property.Status.REMOVED
            listing.property.save(update_fields=["status"])
        listing.save(update_fields=["missing_runs", "active"])


@transaction.atomic
def mark_listing_removed(source, url=None, external_id=None):
    selector = Q()
    if url:
        selector |= Q(url=url)
    if external_id:
        selector |= Q(external_id=str(external_id))
    if not selector:
        return None

    listing = (
        Listing.objects.select_related("property")
        .filter(Q(source=source) & selector)
        .first()
    )
    if not listing:
        return None

    listing.active = False
    listing.source_status = "removed"
    listing.missing_runs = max(listing.missing_runs, 2)
    listing.save(update_fields=["active", "source_status", "missing_runs"])

    if not Listing.objects.filter(property=listing.property, active=True).exists():
        listing.property.status = Property.Status.REMOVED
        listing.property.save(update_fields=["status"])
    return listing
