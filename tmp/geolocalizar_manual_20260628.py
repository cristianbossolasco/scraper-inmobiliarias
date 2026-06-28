import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests
from django.conf import settings
from django.utils import timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from properties.models import GeocodeCache, Property, PropertyLocation, PropertyLocationIntelligence
from properties.services.geocoding import Geocoder, best_address
from properties.services.normalization import (
    address_alias_variants,
    canonical_geocoding_locality,
    classify_address_precision,
    fold_text,
    is_plausible_property_address,
    normalize_address,
    normalize_street_number_address,
    normalize_whitespace,
)
from properties.services.territory_hierarchy import infer_territory_for_point
from properties.services.zone_inference import infer_zone_for_point


IDS = [3181, 5711, 5714, 5715, 5716, 5720, 5722, 5725, 5728, 5733, 5735]
SCRIPT_TAG = "geolocalizar_manual_20260628"
DB_PATH = ROOT / "db.sqlite3"
BACKUP_PATH = ROOT / "tmp" / "db.sqlite3.backup_geolocalizar_manual_20260628"


def safe(value):
    return normalize_whitespace(str(value or ""))


def add(summary, key, value):
    summary.setdefault(key, []).append(value)


def location_or_none(property_obj):
    try:
        return property_obj.location
    except PropertyLocation.DoesNotExist:
        return None


def intel_or_none(property_obj):
    try:
        return property_obj.location_intelligence
    except PropertyLocationIntelligence.DoesNotExist:
        return None


def snapshot(property_obj):
    location = location_or_none(property_obj)
    intel = intel_or_none(property_obj)
    return {
        "id": property_obj.pk,
        "title": property_obj.title,
        "address": property_obj.address,
        "detected_address": property_obj.detected_address,
        "locality": property_obj.locality,
        "detected_locality": property_obj.detected_locality,
        "neighborhood": property_obj.neighborhood,
        "detected_neighborhood": property_obj.detected_neighborhood,
        "zone": property_obj.inferred_zone,
        "intel_zone": intel.zone_name if intel else "",
        "location": None
        if not location
        else {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "provider": location.provider,
            "precision": location.precision,
            "manually_corrected": location.manually_corrected,
            "outside_target": location.outside_target,
            "query": location.query,
        },
        "manual_overrides": property_obj.manual_overrides,
        "corrected_at": property_obj.data_manually_corrected_at.isoformat()
        if property_obj.data_manually_corrected_at
        else "",
    }


def street_aliases(address):
    values = [address, *address_alias_variants(address)]
    expanded = []
    for value in values:
        expanded.append(value)
        expanded.append(value.replace("Int. Mustoni", "Intendente Mustoni"))
        expanded.append(value.replace("Int Mustoni", "Intendente Mustoni"))
        expanded.append(value.replace("R. Prack", "Roberto Prack"))
        expanded.append(value.replace("R Prack", "Roberto Prack"))
    result = []
    for value in expanded:
        value = normalize_whitespace(value)
        if value and value not in result:
            result.append(value)
    return result


def queries_for_address(geocoder, property_obj, address):
    address = normalize_street_number_address(address)
    if not is_plausible_property_address(address):
        return []
    clean_address = geocoder.clean_query_address(address)
    variants = []
    for alias in street_aliases(clean_address):
        variants.append(alias)
        variants.extend(geocoder._intersection_variants(alias))
    localities = []
    for candidate in (
        property_obj.locality,
        property_obj.detected_locality,
        canonical_geocoding_locality(address),
        "Hurlingham",
        "Villa Tesei",
        "William C. Morris",
    ):
        candidate = canonical_geocoding_locality(candidate)
        if candidate and candidate not in localities:
            localities.append(candidate)
    queries = []
    for variant in variants:
        folded_variant = fold_text(variant)
        for locality in localities:
            parts = [
                variant,
                "" if "hurlingham" in folded_variant else locality,
                "Buenos Aires",
                "Argentina",
            ]
            query = ", ".join(dict.fromkeys(filter(None, map(normalize_whitespace, parts))))
            if query and query not in queries:
                queries.append(query)
    return queries


def query_candidates(property_obj):
    geocoder = Geocoder()
    queries = list(geocoder.query_candidates(property_obj))
    for extra_address in (property_obj.address, property_obj.detected_address):
        for query in queries_for_address(geocoder, property_obj, extra_address or ""):
            if query not in queries:
                queries.append(query)
    return queries


def bounds_contains(latitude, longitude):
    bounds = settings.HURLINGHAM_BOUNDS
    return (
        bounds["south"] <= latitude <= bounds["north"]
        and bounds["west"] <= longitude <= bounds["east"]
    )


def apply_location(property_obj, latitude, longitude, provider, query, confidence, summary, apply):
    current = location_or_none(property_obj)
    if current and current.manually_corrected:
        add(summary, "manual_location_preserved", property_obj.pk)
        return current
    outside = not bounds_contains(float(latitude), float(longitude))
    if apply:
        location, _ = PropertyLocation.objects.update_or_create(
            property=property_obj,
            defaults={
                "latitude": float(latitude),
                "longitude": float(longitude),
                "precision": classify_address_precision(best_address(property_obj)),
                "query": query or "",
                "provider": provider,
                "confidence": float(confidence or 0),
                "outside_target": outside,
                "manually_corrected": False,
            },
        )
        evidence = dict(property_obj.location_evidence or {})
        evidence[SCRIPT_TAG] = {
            "provider": provider,
            "query": query or "",
            "latitude": float(latitude),
            "longitude": float(longitude),
            "outside_target": outside,
        }
        property_obj.detected_latitude = float(latitude)
        property_obj.detected_longitude = float(longitude)
        property_obj.location_source = Property.LocationSource.INFERRED
        property_obj.location_confidence = (
            Property.LocationConfidence.LOW if outside else Property.LocationConfidence.HIGH
        )
        property_obj.location_evidence = evidence
        property_obj.save(
            update_fields=[
                "detected_latitude",
                "detected_longitude",
                "location_source",
                "location_confidence",
                "location_evidence",
            ]
        )
        return location
    return PropertyLocation(
        property=property_obj,
        latitude=float(latitude),
        longitude=float(longitude),
        precision=classify_address_precision(best_address(property_obj)),
        query=query or "",
        provider=provider,
        confidence=float(confidence or 0),
        outside_target=outside,
        manually_corrected=False,
    )


def apply_zone(property_obj, location, reason, summary, apply):
    if not location or location.outside_target:
        add(
            summary,
            "zone_skipped",
            {"id": property_obj.pk, "reason": "sin_ubicacion_valida_o_fuera_target"},
        )
        return ""
    match = infer_zone_for_point(location.latitude, location.longitude, max_distance_m=100)
    zone = safe(match.get("zone"))
    if not zone:
        add(
            summary,
            "zone_skipped",
            {
                "id": property_obj.pk,
                "reason": "coordenada_sin_match_zona",
                "nearest": (match.get("evidence") or {}).get("nearest_zone"),
                "distance_m": match.get("distance_m"),
            },
        )
        return ""
    territory = infer_territory_for_point(
        location.latitude,
        location.longitude,
        coordinate_source=location.provider,
        source_zone=property_obj.neighborhood or property_obj.detected_neighborhood or "",
        source_locality=property_obj.locality or property_obj.detected_locality or "",
        extra_evidence={
            "curacion": SCRIPT_TAG,
            "reason": reason,
            "query": location.query,
            "zone_method": match.get("method"),
            "zone_distance_m": match.get("distance_m"),
        },
    )
    evidence = dict(territory.evidence if territory else {})
    evidence.update(match.get("evidence") or {})
    now = timezone.now()
    if apply:
        property_obj.inferred_partido = safe(territory.partido if territory else property_obj.inferred_partido)
        property_obj.inferred_locality = safe(territory.locality if territory else property_obj.inferred_locality)
        property_obj.inferred_zone = zone
        property_obj.inferred_neighborhood = zone
        property_obj.territory_confidence = territory.confidence if territory else "high"
        property_obj.territory_source_method = territory.source_method if territory else "coordinates"
        property_obj.territory_needs_review = False
        property_obj.territory_evidence = evidence
        property_obj.territory_inferred_at = now
        property_obj.zone_conflict = False
        property_obj.zone_needs_review = False
        property_obj.zone_inference_evidence = evidence
        property_obj.zone_inferred_at = now
        property_obj.save(
            update_fields=[
                "inferred_partido",
                "inferred_locality",
                "inferred_zone",
                "inferred_neighborhood",
                "territory_confidence",
                "territory_source_method",
                "territory_needs_review",
                "territory_evidence",
                "territory_inferred_at",
                "zone_conflict",
                "zone_needs_review",
                "zone_inference_evidence",
                "zone_inferred_at",
            ]
        )
        PropertyLocationIntelligence.objects.update_or_create(
            property=property_obj,
            defaults={
                "partido_name": property_obj.inferred_partido,
                "locality_name": property_obj.inferred_locality,
                "zone_name": zone,
                "match_method": PropertyLocationIntelligence.MatchMethod.COORDINATES,
                "confidence": property_obj.territory_confidence or "high",
            },
        )
    add(
        summary,
        "zone_applied",
        {
            "id": property_obj.pk,
            "zone": zone,
            "provider": location.provider,
            "lat": location.latitude,
            "lon": location.longitude,
            "method": match.get("method"),
            "distance_m": match.get("distance_m"),
        },
    )
    return zone


def fetch_geocode(geocoder, query, property_obj):
    with Geocoder._rate_lock:
        elapsed = time.monotonic() - Geocoder._last_request_at
        if elapsed < 1:
            time.sleep(1 - elapsed)
        response = geocoder.session.get(
            settings.NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "ar",
                "addressdetails": 1,
            },
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
            timeout=20,
        )
        Geocoder._last_request_at = time.monotonic()
    response.raise_for_status()
    results = response.json()
    result = results[0] if results else {}
    cache, _ = GeocodeCache.objects.update_or_create(
        query=query,
        defaults={
            "latitude": float(result["lat"]) if result else None,
            "longitude": float(result["lon"]) if result else None,
            "precision": classify_address_precision(best_address(property_obj)) if result else "",
            "confidence": float(result.get("importance", 0)) if result else 0,
            "provider_payload": result,
        },
    )
    return cache


def geolocate_property(property_obj, summary, apply):
    queries = query_candidates(property_obj)
    add(summary, "query_candidates", {"id": property_obj.pk, "queries": queries})
    current = location_or_none(property_obj)
    if current and current.manually_corrected:
        add(summary, "location_source", {"id": property_obj.pk, "provider": current.provider, "manual": True})
        return apply_zone(property_obj, current, "pin manual existente", summary, apply)
    if current and not current.outside_target:
        add(summary, "location_source", {"id": property_obj.pk, "provider": current.provider, "manual": False})
        return apply_zone(property_obj, current, "ubicacion existente", summary, apply)

    geocoder = Geocoder()
    for query in queries:
        cache = GeocodeCache.objects.filter(query=query, latitude__isnull=False, longitude__isnull=False).first()
        if not cache:
            continue
        location = apply_location(
            property_obj,
            cache.latitude,
            cache.longitude,
            "nominatim_cache",
            query,
            cache.confidence,
            summary,
            apply,
        )
        add(summary, "geocode_cache_hit", {"id": property_obj.pk, "query": query})
        if not location.outside_target:
            return apply_zone(property_obj, location, "cache de geocoding", summary, apply)
        add(summary, "geocode_outside_target", {"id": property_obj.pk, "query": query})

    if not apply:
        local = dry_run_local_reference(property_obj)
        if local:
            add(summary, "local_reference_candidate", local)
        else:
            add(summary, "geocode_external_required", property_obj.pk)
        return ""

    location = geocoder._local_reference(property_obj, queries)
    if location:
        add(
            summary,
            "local_reference_location",
            {
                "id": property_obj.pk,
                "lat": location.latitude,
                "lon": location.longitude,
                "query": location.query,
            },
        )
        return apply_zone(property_obj, location, "referencia local misma calle", summary, apply)

    for query in queries:
        try:
            cache = fetch_geocode(geocoder, query, property_obj)
        except (requests.RequestException, ValueError, KeyError) as exc:
            add(summary, "geocode_errors", {"id": property_obj.pk, "query": query, "error": repr(exc)})
            continue
        if cache.latitude is None or cache.longitude is None:
            continue
        location = apply_location(
            property_obj,
            cache.latitude,
            cache.longitude,
            "nominatim",
            query,
            cache.confidence,
            summary,
            apply,
        )
        add(
            summary,
            "geocode_external_location",
            {"id": property_obj.pk, "query": query, "lat": location.latitude, "lon": location.longitude},
        )
        if not location.outside_target:
            return apply_zone(property_obj, location, "geocoding externo controlado", summary, apply)
        add(summary, "geocode_outside_target", {"id": property_obj.pk, "query": query})

    add(summary, "geocode_no_result", property_obj.pk)
    return ""


def dry_run_local_reference(property_obj):
    # Mirrors Geocoder._local_reference without writing a PropertyLocation.
    from properties.services.geocoding import address_number, street_key

    address = best_address(property_obj)
    number = address_number(address)
    key = street_key(address)
    address_precision = classify_address_precision(address)
    allow_street_reference = number is None and address_precision in {
        PropertyLocation.Precision.INTERSECTION,
        PropertyLocation.Precision.STREET,
    }
    if not key:
        return None
    candidates = []
    for other in (
        Property.objects.exclude(pk=property_obj.pk)
        .filter(location__isnull=False)
        .select_related("location")
    ):
        other_address = best_address(other)
        if other.location.outside_target:
            continue
        if street_key(other_address) != key:
            continue
        other_number = address_number(other_address)
        if number is not None and other_number is not None:
            delta = abs(number - other_number)
        elif number is None and other_number is None:
            delta = 0
        elif allow_street_reference:
            delta = 0
        else:
            delta = 99999
        if delta <= 250:
            candidates.append((delta, other))
    if not candidates:
        return None
    delta, reference = min(candidates, key=lambda item: item[0])
    return {
        "id": property_obj.pk,
        "reference_id": reference.pk,
        "reference_address": best_address(reference),
        "delta_number": delta,
        "lat": reference.location.latitude,
        "lon": reference.location.longitude,
        "provider": reference.location.provider,
    }


def backup_db(summary):
    target = BACKUP_PATH
    if target.exists():
        stamp = timezone.now().strftime("%H%M%S")
        target = target.with_name(f"{target.name}_{stamp}")
    shutil.copy2(DB_PATH, target)
    summary["backup"] = str(target)


def run(apply):
    summary = {
        "mode": "apply" if apply else "dry_run",
        "script": SCRIPT_TAG,
        "ids": IDS,
        "started_at": timezone.now().isoformat(),
    }
    properties = list(Property.objects.filter(pk__in=IDS).order_by("id"))
    found_ids = [p.pk for p in properties]
    summary["missing_ids"] = [pk for pk in IDS if pk not in found_ids]
    summary["before"] = [snapshot(p) for p in properties]
    if apply:
        backup_db(summary)
    for property_obj in properties:
        geolocate_property(property_obj, summary, apply)
    for property_obj in Property.objects.filter(pk__in=IDS).order_by("id"):
        property_obj.refresh_from_db()
        add(summary, "after", snapshot(property_obj))
    summary["finished_at"] = timezone.now().isoformat()
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    if args.dry_run == args.apply:
        parser.error("usar exactamente uno de --dry-run o --apply")
    summary = run(apply=args.apply)
    payload = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
