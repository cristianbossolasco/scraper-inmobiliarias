import time
import threading

import requests
from django.conf import settings
from django.db.models import Q

from properties.models import GeocodeCache, PropertyLocation
from .normalization import (
    classify_address_precision,
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
    return normalize_street_number_address(property_obj.address or property_obj.detected_address or "")


def has_geocodable_address(property_obj):
    return bool(best_address(property_obj))


class Geocoder:
    _last_request_at = 0.0
    _rate_lock = threading.Lock()

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def build_query(self, property_obj):
        address = best_address(property_obj)
        parts = [
            address,
            property_obj.neighborhood or property_obj.detected_neighborhood,
            property_obj.locality or property_obj.detected_locality,
            "Partido de Hurlingham",
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
