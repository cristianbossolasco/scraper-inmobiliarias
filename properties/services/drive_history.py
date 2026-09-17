"""Bounded trip ingestion and historical property snapshots; no network access."""

import math
import re
from datetime import datetime, timezone as datetime_timezone
from urllib.parse import urlsplit

from django.utils import timezone

from properties.models import DriveHistory, Property
from properties.services.drive_mode import (
    _address_payload,
    _price_short,
    _safe_https_url,
    _safe_image_url,
    _with_card_image,
)


MAX_PAYLOAD_BYTES = 6 * 1024 * 1024
MAX_POINTS = 1500
MAX_GROUPS = 3000
MAX_PROPERTIES = 3000
MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
TEXT_FIELDS = {
    "price_short": 80, "type": 30, "type_label": 80,
    "address_text": 500, "address_label": 120, "address_reliability": 40,
    "location_label": 120, "location_reliability": 40, "original_source_name": 160,
    "group_id": 100,
}
NUMBER_FIELDS = {
    "bedrooms": (0, 1000), "bathrooms": (0, 1000), "area_m2": (0, 100000000),
    "covered_area_m2": (0, 100000000), "land_area_m2": (0, 100000000),
    "latitude": (-90, 90), "longitude": (-180, 180),
}


class DriveHistoryValidationError(ValueError):
    pass


def _number(value, label, minimum, maximum, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DriveHistoryValidationError(f"{label} debe ser un número.")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise DriveHistoryValidationError(f"{label} está fuera de rango.")
    if integer and value != int(value):
        raise DriveHistoryValidationError(f"{label} debe ser entero.")
    return int(value) if integer else value


def _mapping(value, label, maximum):
    if not isinstance(value, dict) or len(value) > maximum:
        raise DriveHistoryValidationError(f"{label} no es válido o supera el límite.")
    return value


def _ids(value, label, maximum=MAX_PROPERTIES):
    if not isinstance(value, list) or len(value) > maximum:
        raise DriveHistoryValidationError(f"{label} supera el límite permitido.")
    return list(dict.fromkeys(
        _number(item, label, 1, 2147483647, integer=True) for item in value
    ))


def _filters(value):
    value = _mapping(value, "Filtros", 20)
    types = value.get("propertyTypes", [])
    if not isinstance(types, list) or len(types) > 20 or any(
        not isinstance(item, str) or item not in Property.Type.values for item in types
    ):
        raise DriveHistoryValidationError("Tipos de propiedad inválidos.")
    result = {
        "propertyTypes": list(dict.fromkeys(types)),
        "radiusM": _number(value.get("radiusM", 350), "Radio", 200, 1500, integer=True),
    }
    for field, maximum in (
        ("bedroomsMin", 12), ("priceMin", 5000000), ("priceMax", 5000000),
        ("coveredAreaMinM2", 100000), ("landAreaMinM2", 100000),
    ):
        raw = value.get(field)
        result[field] = None if raw is None else _number(raw, field, 0, maximum, integer=True)
    if (result["priceMin"] is not None and result["priceMax"] is not None
            and result["priceMin"] > result["priceMax"]):
        raise DriveHistoryValidationError("El precio mínimo supera al máximo.")
    return result


def _distance(a, b):
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b["longitude"] - a["longitude"])
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(haversine)))


def _snapshot(value, property_id):
    value = _mapping(value, "Ficha", 60)
    result = {"id": property_id}
    for field, maximum in TEXT_FIELDS.items():
        raw = value.get(field)
        if raw is not None:
            if not isinstance(raw, str) or len(raw) > maximum:
                raise DriveHistoryValidationError(f"{field} no es válido.")
            result[field] = raw
    for field, (minimum, maximum) in NUMBER_FIELDS.items():
        raw = value.get(field)
        result[field] = None if raw is None else _number(raw, field, minimum, maximum)
    for field in ("image_url", "original_url"):
        raw = value.get(field, "")
        if raw is not None and not isinstance(raw, str):
            raise DriveHistoryValidationError(f"{field} no es válido.")
        result[field] = _safe_https_url(raw or "")
    result["original_host"] = urlsplit(result["original_url"]).hostname or ""
    result["seen_before"] = value.get("seen_before") if isinstance(value.get("seen_before"), bool) else None
    return result


def normalize_session(payload):
    payload = _mapping(payload, "Recorrido", 40)
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise DriveHistoryValidationError("Identificador de recorrido inválido.")
    if payload.get("status", "ended") != "ended":
        raise DriveHistoryValidationError("Solo se pueden guardar recorridos finalizados.")
    latest = int(timezone.now().timestamp() * 1000) + 5 * 60 * 1000
    started = _number(payload.get("startedAt"), "Inicio", 1, latest, integer=True)
    ended = _number(payload.get("endedAt"), "Fin", started, latest, integer=True)
    if ended - started > MAX_DURATION_MS:
        raise DriveHistoryValidationError("El recorrido supera los siete días.")
    points = payload.get("points", [])
    if not isinstance(points, list) or len(points) > MAX_POINTS:
        raise DriveHistoryValidationError("La traza supera los 1500 puntos.")
    clean_points, distance = [], 0
    for raw in points:
        raw = _mapping(raw, "Punto", 10)
        point = {
            "latitude": _number(raw.get("latitude"), "Latitud", -90, 90),
            "longitude": _number(raw.get("longitude"), "Longitud", -180, 180),
            "accuracy": _number(raw.get("accuracy", 0), "Precisión", 0, 10000),
            "timestamp": _number(raw.get("timestamp"), "Hora del punto", max(1, started - 60000), ended + 60000, integer=True),
        }
        if clean_points:
            previous = clean_points[-1]
            elapsed = (point["timestamp"] - previous["timestamp"]) / 1000
            step = _distance(previous, point)
            if elapsed < 0 or step > max(250, elapsed * 75 + 100):
                raise DriveHistoryValidationError("La traza contiene saltos o tiempos inválidos.")
            distance += step
        clean_points.append(point)
    if distance > 2000000:
        raise DriveHistoryValidationError("La distancia del recorrido supera el límite.")
    if "distanceM" in payload:
        _number(payload["distanceM"], "Distancia", 0, 2000000)
    encounters, referenced = {}, set()
    for key, raw in _mapping(payload.get("encounters", {}), "Encuentros", MAX_GROUPS).items():
        if not isinstance(key, str) or not 1 <= len(key) <= 100 or key in {"__proto__", "constructor", "prototype"}:
            raise DriveHistoryValidationError("Grupo inválido.")
        raw = _mapping(raw, "Encuentro", 10)
        ids = _ids(raw.get("propertyIds", []), "Propiedades del grupo", MAX_PROPERTIES)
        referenced.update(ids)
        encounters[key] = {
            "firstSeenAt": _number(raw.get("firstSeenAt"), "Hora del encuentro", started - 60000, ended + 60000, integer=True),
            "minDistanceM": _number(raw.get("minDistanceM", 0), "Distancia al encuentro", 0, 10000),
            "propertyIds": ids,
        }
    favorites = _ids(payload.get("favoritePropertyIds", []), "Favoritas")
    referenced.update(favorites)
    if len(referenced) > MAX_PROPERTIES:
        raise DriveHistoryValidationError(f"El recorrido supera las {MAX_PROPERTIES} propiedades.")
    details = {}
    for key, value in _mapping(payload.get("details", {}), "Fichas", MAX_PROPERTIES).items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or len(key) > 10:
            raise DriveHistoryValidationError("Identificador de ficha inválido.")
        property_id = int(key)
        if property_id in referenced:
            details[str(property_id)] = _snapshot(value, property_id)
    return {
        "version": 3 if payload.get("version") == 3 else 2, "sessionId": session_id, "status": "ended",
        "startedAt": started, "endedAt": ended, "updatedAt": ended,
        "filters": _filters(payload.get("filters", {})), "points": clean_points,
        "distanceM": round(distance, 1), "encounters": encounters,
        "favoritePropertyIds": favorites, "details": details,
        "announcedGroupIds": [], "dismissedGroupIds": [],
    }


def _enrich_missing_details(session):
    ids = set(session["favoritePropertyIds"])
    for encounter in session["encounters"].values():
        ids.update(encounter["propertyIds"])
    missing = {pk for pk in ids if "address_text" not in session["details"].get(str(pk), {})}
    if not missing:
        return
    properties = _with_card_image(Property.objects.filter(
        pk__in=missing, operation="sale", is_hidden=False, status=Property.Status.ACTIVE,
    ).select_related("location").only(
        "id", "property_type", "currency", "price", "bedrooms", "address",
        "detected_address", "manual_overrides", "location__latitude", "location__longitude",
    ))
    for prop in properties:
        original = _safe_https_url(prop.drive_image_listing_url) or _safe_https_url(prop.drive_fallback_listing_url)
        snapshot = {
            "id": prop.pk, "price_short": _price_short(prop.currency, prop.price),
            "type": prop.property_type, "type_label": prop.get_property_type_display(),
            "bedrooms": prop.bedrooms, "original_url": original,
            "original_host": urlsplit(original).hostname or "",
            "image_url": _safe_image_url(prop.drive_image_raw, prop.drive_image_listing_url),
            **_address_payload(prop),
        }
        if hasattr(prop, "location"):
            snapshot.update(latitude=prop.location.latitude, longitude=prop.location.longitude)
        # Complete compact discovery snapshots without replacing the observed
        # price or coordinates with values changed since the trip.
        existing = session["details"].get(str(prop.pk), {})
        session["details"][str(prop.pk)] = {
            **snapshot, **{key: value for key, value in existing.items() if value is not None and value != ""},
        }


def save_session(owner, session):
    # Finalized trips are immutable: a retried or stale upload cannot replace a saved trip.
    existing = DriveHistory.objects.filter(owner=owner, client_session_id=session["sessionId"]).first()
    if existing:
        return existing, False
    _enrich_missing_details(session)
    return DriveHistory.objects.get_or_create(
        owner=owner, client_session_id=session["sessionId"],
        defaults={
            "started_at": datetime.fromtimestamp(session["startedAt"] / 1000, tz=datetime_timezone.utc),
            "ended_at": datetime.fromtimestamp(session["endedAt"] / 1000, tz=datetime_timezone.utc),
            "distance_m": session["distanceM"], "point_count": len(session["points"]),
            "encounter_count": len(session["encounters"]),
            "favorite_count": len(session["favoritePropertyIds"]),
            "filters": session["filters"], "session": session,
        },
    )


def history_summary(trip):
    return {
        "id": str(trip.id), "sessionId": trip.client_session_id,
        "startedAt": round(trip.started_at.timestamp() * 1000),
        "endedAt": round(trip.ended_at.timestamp() * 1000),
        "distanceM": trip.distance_m, "pointCount": trip.point_count,
        "encounterCount": trip.encounter_count, "favoriteCount": trip.favorite_count,
        "filters": trip.filters,
    }
