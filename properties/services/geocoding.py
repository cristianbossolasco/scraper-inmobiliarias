import re
import time
import threading

import requests
from django.conf import settings
from django.db.models import Q

from properties.models import GeocodeCache, PropertyLocation
from .normalization import (
    classify_address_precision,
    fold_text,
    is_plausible_property_address,
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


class Geocoder:
    _last_request_at = 0.0
    _rate_lock = threading.Lock()

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def clean_query_address(self, address):
        address = normalize_street_number_address(address)
        parts = [normalize_whitespace(part) for part in address.split(",")]
        filtered = []
        for part in parts:
            folded = fold_text(part)
            if not part:
                continue
            if re.fullmatch(r"b?\d{4}", folded):
                continue
            if folded in {
                "argentina",
                "buenos aires",
                "provincia de buenos aires",
                "partido de hurlingham",
            }:
                continue
            filtered.append(part)
        return ", ".join(dict.fromkeys(filtered)) or address

    def build_query(self, property_obj):
        address = best_address(property_obj)
        if not address:
            return ""
        query_address = self.clean_query_address(address)
        folded_query = fold_text(query_address)
        parts = [
            query_address,
            "" if "hurlingham" in folded_query else (property_obj.locality or property_obj.detected_locality),
            "Buenos Aires",
            "Argentina",
        ]
        return ", ".join(dict.fromkeys(filter(None, map(normalize_whitespace, parts))))

    def geocode_property(self, property_obj, force=False):
        current = getattr(property_obj, "location", None)
        if current and current.manually_corrected:
            return current
        if current and not force and current.provider != "nominatim" and current.is_exact:
            return current

        query = self.build_query(property_obj)
        if not query:
            return None
        cache = GeocodeCache.objects.filter(query=query).first()
        if cache:
            return self._apply(property_obj, query, cache)

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
        cache = GeocodeCache.objects.create(
            query=query,
            latitude=float(result["lat"]) if result else None,
            longitude=float(result["lon"]) if result else None,
            precision=precision if result else "",
            confidence=float(result.get("importance", 0)),
            provider_payload=result,
        )
        return self._apply(property_obj, query, cache)

    def geocode_property_from_cache(self, property_obj, force=False):
        current = getattr(property_obj, "location", None)
        if current and current.manually_corrected:
            return current
        if current and not force:
            return current

        query = self.build_query(property_obj)
        if not query:
            return None
        cache = GeocodeCache.objects.filter(query=query).first()
        if not cache:
            return None
        return self._apply(property_obj, query, cache)

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
