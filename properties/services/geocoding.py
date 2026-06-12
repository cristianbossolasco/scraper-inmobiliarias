import re
import time
import threading

import requests
from django.conf import settings
from django.db.models import Q

from properties.models import GeocodeCache, Property, PropertyLocation
from .normalization import (
    address_alias_variants,
    canonical_geocoding_locality,
    classify_address_precision,
    fold_text,
    is_plausible_property_address,
    normalize_address,
    normalize_street_number_address,
    normalize_whitespace,
)


def geocodable_address_q():
    return (
        Q(address__isnull=False)
        & ~Q(address="")
        | Q(detected_address__isnull=False)
        & ~Q(detected_address="")
    )


def best_address(property_obj):
    for value in (property_obj.address, property_obj.detected_address):
        address = normalize_street_number_address(value or "")
        if is_plausible_property_address(address):
            return address
    return ""


def has_geocodable_address(property_obj):
    return bool(best_address(property_obj))


def address_number(address):
    match = re.search(r"\b(\d{2,5})\b", normalize_street_number_address(address or ""))
    return int(match.group(1)) if match else None


def street_key(address):
    aliases = address_alias_variants(address)
    normalized = normalize_address(aliases[0] if aliases else address)
    normalized = re.sub(r"\b\d{2,5}\b.*$", "", normalized)
    normalized = re.sub(r"\b(?:entre|e/|esquina|y)\b.*$", "", normalized)
    return normalize_whitespace(normalized)


class Geocoder:
    _last_request_at = 0.0
    _rate_lock = threading.Lock()

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def clean_query_address(self, address):
        address = normalize_street_number_address(address)
        address = re.sub(
            r"(\b\d{2,5}\b)(?:\s*[-–,]?\s*hurlingham)?\s*[-–,]?\s+(?:barrio|b[°º]\.?)\s+[^,;/|]+",
            r"\1",
            address,
            flags=re.I,
        )
        parts = [normalize_whitespace(part) for part in address.split(",")]
        filtered = []
        for part in parts:
            folded = fold_text(part)
            if not part:
                continue
            if re.fullmatch(r"b?\d{4}[a-z]{0,4}", folded):
                continue
            if folded in {
                "argentina",
                "buenos aires",
                "provincia de buenos aires",
                "partido de hurlingham",
                "hurlingham",
                "villa tesei",
                "william c morris",
                "william morris",
            }:
                continue
            filtered.append(part)
        if not filtered:
            return address
        first = filtered[0]
        first = re.sub(r"\.?\s+\bentre\b.*$", "", first, flags=re.I)
        first = re.sub(
            r"\s+\b(?:hurlingham|villa\s+tesei|william\s+c\.?\s+morris|william\s+morris)\b\s*$",
            "",
            first,
            flags=re.I,
        )
        return normalize_whitespace(first.strip(" ,-")) or address

    def build_query(self, property_obj):
        candidates = self.query_candidates(property_obj)
        return candidates[0] if candidates else ""

    def query_candidates(self, property_obj):
        address = best_address(property_obj)
        if not address:
            return []
        query_address = self.clean_query_address(address)
        locality = canonical_geocoding_locality(
            property_obj.locality,
            property_obj.detected_locality,
            property_obj.neighborhood,
            property_obj.detected_neighborhood,
        )
        address_locality = canonical_geocoding_locality(address)
        address_variants = [query_address, *address_alias_variants(query_address)]
        intersection_variants = []
        for variant in address_variants:
            intersection_variants.extend(self._intersection_variants(variant))
        address_variants.extend(intersection_variants)

        candidates = []
        localities = [locality]
        if address_locality and address_locality not in localities:
            localities.append(address_locality)
        if locality != "Hurlingham":
            localities.append("Hurlingham")
        for variant in address_variants:
            folded_variant = fold_text(variant)
            for candidate_locality in localities:
                parts = [
                    variant,
                    "" if "hurlingham" in folded_variant else candidate_locality,
                    "Buenos Aires",
                    "Argentina",
                ]
                query = ", ".join(dict.fromkeys(filter(None, map(normalize_whitespace, parts))))
                if query and query not in candidates:
                    candidates.append(query)
        return candidates

    def _intersection_variants(self, address):
        if not re.search(r"\b(?:y|e/|entre|esquina)\b", address, re.I):
            return []
        variants = []
        text = normalize_whitespace(address)
        match = re.match(r"(.+?)\s+e/\s*(.+?)\s+y\s+(.+)$", text, re.I)
        if match:
            variants.append(f"{match.group(1)} esquina {match.group(2)}")
            variants.append(f"{match.group(1)} esquina {match.group(3)}")
            return [normalize_whitespace(value) for value in variants if value]
        if "esquina" not in fold_text(text) and "batlle y ordonez" not in fold_text(text):
            match = re.match(r"(.+?)\s+y\s+(.+)$", text, re.I)
            if match:
                variants.append(f"{match.group(1)} esquina {match.group(2)}")
        return [normalize_whitespace(value) for value in variants if value]

    def geocode_property(self, property_obj, force=False):
        current = getattr(property_obj, "location", None)
        if current and current.manually_corrected:
            return current
        if current and not force and current.provider != "nominatim" and current.is_exact:
            return current

        queries = self.query_candidates(property_obj)
        if not queries:
            return None
        cached_location = self._from_cached_candidates(property_obj, queries)
        if cached_location:
            return cached_location

        for query in queries:
            if GeocodeCache.objects.filter(query=query).exists():
                continue
            cache = self._fetch(query, property_obj)
            location = self._apply(property_obj, query, cache)
            if location:
                return location
        return self._local_reference(property_obj, queries)

    def _fetch(self, query, property_obj):
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 1:
                time.sleep(1 - elapsed)
            response = self.session.get(
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
            self._last_request_at = time.monotonic()
        response.raise_for_status()
        results = response.json()
        result = results[0] if results else {}
        precision = classify_address_precision(best_address(property_obj))
        return GeocodeCache.objects.create(
            query=query,
            latitude=float(result["lat"]) if result else None,
            longitude=float(result["lon"]) if result else None,
            precision=precision if result else "",
            confidence=float(result.get("importance", 0)),
            provider_payload=result,
        )

    def geocode_property_from_cache(self, property_obj, force=False):
        current = getattr(property_obj, "location", None)
        if current and current.manually_corrected:
            return current
        if current and not force:
            return current

        queries = self.query_candidates(property_obj)
        if not queries:
            return None
        return self._from_cached_candidates(property_obj, queries) or self._local_reference(
            property_obj, queries
        )

    def _from_cached_candidates(self, property_obj, queries):
        for query in queries:
            cache = GeocodeCache.objects.filter(query=query).first()
            if not cache:
                continue
            location = self._apply(property_obj, query, cache)
            if location:
                return location
        return None

    def _apply(self, property_obj, query, cache):
        if cache.latitude is None or cache.longitude is None:
            return None
        bounds = settings.HURLINGHAM_BOUNDS
        outside = not (
            bounds["south"] <= cache.latitude <= bounds["north"]
            and bounds["west"] <= cache.longitude <= bounds["east"]
        )
        location, _ = PropertyLocation.objects.update_or_create(
            property=property_obj,
            defaults={
                "latitude": cache.latitude,
                "longitude": cache.longitude,
                "precision": classify_address_precision(best_address(property_obj)),
                "query": query,
                "provider": "nominatim",
                "confidence": cache.confidence,
                "outside_target": outside,
                "manually_corrected": False,
            },
        )
        return location

    def _local_reference(self, property_obj, queries):
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
        precision = (
            PropertyLocation.Precision.EXACT
            if number is not None and delta <= 50
            else (
                PropertyLocation.Precision.INTERSECTION
                if address_precision == PropertyLocation.Precision.INTERSECTION
                else PropertyLocation.Precision.STREET
            )
        )
        ref_location = reference.location
        location, _ = PropertyLocation.objects.update_or_create(
            property=property_obj,
            defaults={
                "latitude": ref_location.latitude,
                "longitude": ref_location.longitude,
                "precision": precision,
                "query": queries[0] if queries else "",
                "provider": "local_reference",
                "confidence": max(0.2, 0.75 - (delta / 500 if delta != 99999 else 0.5)),
                "outside_target": ref_location.outside_target,
                "manually_corrected": False,
            },
        )
        evidence = dict(property_obj.location_evidence or {})
        evidence["local_reference"] = {
            "property_id": reference.pk,
            "address": best_address(reference),
            "delta_number": delta,
            "provider": ref_location.provider,
        }
        property_obj.location_evidence = evidence
        property_obj.save(update_fields=["location_evidence"])
        return location
