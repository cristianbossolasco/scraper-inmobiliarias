import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from properties.services.spatial import (
    haversine_km,
    point_in_polygon,
    point_to_polygon_distance_m,
)


DEFAULT_INTEGRATED_ZONES = (
    Path(settings.BASE_DIR) / "data" / "geo" / "integrated_location_value_zones_hurlingham.geojson"
)
DEFAULT_FLOOD_RISK = (
    Path(settings.BASE_DIR) / "data" / "geo" / "flood" / "flood_risk_hurlingham.geojson"
)
DEFAULT_RENABAP = Path(settings.BASE_DIR) / "data" / "geo" / "renabap" / "renabap_hurlingham.geojson"
DEFAULT_TRANSPORT_ZONES = (
    Path(settings.BASE_DIR) / "data" / "geo" / "transport" / "transport_zones_hurlingham.geojson"
)


COMPONENT_FIELDS = {
    "transport_score": "transport_access_score",
    "education_score": "education_access_score",
    "health_score": "health_access_score",
    "environmental_penalty_score": "environmental_penalty_score",
    "development_potential_score": "development_potential_score",
    "urban_informality_score": "urban_informality_score",
}
RISK_FIELDS = {
    "flood_penalty_score": "flood_penalty_score",
    "in_flood_risk_zone": "in_flood_risk_zone",
    "flood_risk_level": "flood_risk_level",
    "flood_risk_overlap_pct": "flood_risk_overlap_pct",
    "nearest_renabap_m": "nearest_renabap_m",
    "inside_renabap": "inside_renabap",
    "renabap_area_overlap_m2": "renabap_area_overlap_m2",
    "renabap_families_nearby": "renabap_families_nearby",
}
NEAREST_FIELDS = (
    "nearest_sube_point_m",
    "nearest_school_m",
    "nearest_health_center_m",
    "nearest_train_station_m",
    "nearest_official_bus_route_m",
    "nearest_primary_school_m",
    "nearest_secondary_school_m",
    "nearest_hospital_m",
)


@dataclass(frozen=True)
class LocationIntelligenceScore:
    overall_score: float | None = None
    level: str = ""
    zone_name: str = ""
    match_method: str = "none"
    confidence: str = ""
    transport_score: float | None = None
    education_score: float | None = None
    health_score: float | None = None
    flood_penalty_score: float | None = None
    urban_informality_score: float | None = None
    environmental_penalty_score: float | None = None
    development_potential_score: float | None = None
    in_flood_risk_zone: bool | None = None
    nearest_renabap_m: float | None = None
    nearest_sube_point_m: float | None = None
    nearest_school_m: float | None = None
    nearest_health_center_m: float | None = None
    components: dict | None = None
    risks: dict | None = None
    evidence: dict | None = None
    source_signature: str = ""

    @property
    def matched(self):
        return self.overall_score is not None and self.match_method != "none"


def _file_signature(path):
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None, None)
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def location_intelligence_signature(zone_path=None):
    return "|".join(
        ":".join(str(part) for part in _file_signature(path))
        for path in (
            zone_path or DEFAULT_INTEGRATED_ZONES,
            DEFAULT_FLOOD_RISK,
            DEFAULT_RENABAP,
        )
    )


@lru_cache(maxsize=32)
def _read_geojson_cached(path_str, mtime_ns, size):
    if mtime_ns is None:
        return {"type": "FeatureCollection", "features": []}, "missing"
    try:
        payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"type": "FeatureCollection", "features": []}, f"json_invalid:{exc.msg}"
    except OSError as exc:
        return {"type": "FeatureCollection", "features": []}, f"read_error:{exc}"
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": []}, "not_feature_collection"
    payload["features"] = payload.get("features") or []
    return payload, ""


def load_geojson(path):
    path_str, mtime_ns, size = _file_signature(path)
    payload, error = _read_geojson_cached(path_str, mtime_ns, size)
    return {
        "path": path_str,
        "features": payload.get("features") or [],
        "metadata": payload.get("metadata") or {},
        "configured": bool(payload.get("features")),
        "error": error,
        "signature": ":".join(str(part) for part in (path_str, mtime_ns, size)),
    }


def load_location_zones(path=None):
    return load_geojson(path or DEFAULT_INTEGRATED_ZONES)


def _numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score(value):
    parsed = _numeric(value)
    if parsed is None:
        return None
    return round(max(0, min(100, parsed)), 2)


def _level(score, explicit=""):
    if explicit:
        return explicit
    parsed = _score(score)
    if parsed is None:
        return ""
    if parsed >= 70:
        return "alta"
    if parsed >= 60:
        return "media_alta"
    if parsed >= 45:
        return "media"
    return "baja"


def _plain(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return "".join(char for char in text if not unicodedata.combining(char))


def _zone_candidates(property_obj):
    names = [
        getattr(property_obj, "detected_neighborhood", ""),
        getattr(property_obj, "neighborhood", ""),
        getattr(property_obj, "inferred_neighborhood", ""),
        getattr(property_obj, "locality", ""),
        getattr(property_obj, "detected_locality", ""),
    ]
    return {_plain(name) for name in names if _plain(name)}


def _geometry_contains(latitude, longitude, geometry):
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon" and coordinates:
        return point_in_polygon(latitude, longitude, coordinates[0])
    if geometry_type == "MultiPolygon":
        return any(
            polygon and point_in_polygon(latitude, longitude, polygon[0])
            for polygon in coordinates
        )
    return False


def _coordinate_pairs(coordinates):
    if not isinstance(coordinates, list):
        return
    if (
        len(coordinates) >= 2
        and isinstance(coordinates[0], (int, float))
        and isinstance(coordinates[1], (int, float))
    ):
        yield coordinates[0], coordinates[1]
        return
    for item in coordinates:
        yield from _coordinate_pairs(item)


def _geometry_bbox(geometry):
    bbox = geometry.get("bbox")
    if bbox and len(bbox) >= 4:
        return tuple(bbox[:4])
    pairs = list(_coordinate_pairs(geometry.get("coordinates") or []))
    if not pairs:
        return None
    longitudes = [pair[0] for pair in pairs]
    latitudes = [pair[1] for pair in pairs]
    return (min(longitudes), min(latitudes), max(longitudes), max(latitudes))


def _feature_bbox(feature):
    if "_bbox" not in feature:
        feature["_bbox"] = _geometry_bbox(feature.get("geometry") or {})
    return feature.get("_bbox")


def _point_in_bbox(latitude, longitude, bbox):
    if not bbox:
        return True
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon


def _bbox_distance_m(latitude, longitude, bbox):
    if not bbox:
        return 0
    min_lon, min_lat, max_lon, max_lat = bbox
    clamped_lat = min(max(latitude, min_lat), max_lat)
    clamped_lon = min(max(longitude, min_lon), max_lon)
    return haversine_km(latitude, longitude, clamped_lat, clamped_lon) * 1000


def _feature_contains(latitude, longitude, feature):
    if not _point_in_bbox(latitude, longitude, _feature_bbox(feature)):
        return False
    return _geometry_contains(latitude, longitude, feature.get("geometry") or {})


def _geometry_distance_m(latitude, longitude, geometry):
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Point" and len(coordinates) >= 2:
        return haversine_km(latitude, longitude, coordinates[1], coordinates[0]) * 1000
    if geometry_type == "Polygon" and coordinates:
        return point_to_polygon_distance_m(latitude, longitude, coordinates[0])
    if geometry_type == "MultiPolygon":
        distances = [
            point_to_polygon_distance_m(latitude, longitude, polygon[0])
            for polygon in coordinates
            if polygon
        ]
        distances = [distance for distance in distances if distance is not None]
        return min(distances) if distances else None
    return None


def _best_coordinate_match(latitude, longitude, features):
    for feature in features or []:
        if _feature_contains(latitude, longitude, feature):
            return feature
    return None


def _best_zone_match(property_obj, features):
    candidates = _zone_candidates(property_obj)
    if not candidates:
        return None
    for feature in features or []:
        props = feature.get("properties") or {}
        if _plain(props.get("zone_name") or props.get("label") or props.get("name")) in candidates:
            return feature
    return None


def _nearest_feature(latitude, longitude, features, max_distance_m=None):
    nearest = None
    for feature in features or []:
        if max_distance_m is not None:
            bbox_distance = _bbox_distance_m(latitude, longitude, _feature_bbox(feature))
            if bbox_distance > max_distance_m:
                continue
        distance = _geometry_distance_m(latitude, longitude, feature.get("geometry") or {})
        if distance is None:
            continue
        if max_distance_m is not None and distance > max_distance_m:
            continue
        if nearest is None or distance < nearest["distance_m"]:
            nearest = {
                "distance_m": round(distance, 1),
                "feature": feature,
            }
    return nearest


def _exact_risk_context(latitude, longitude, zone_name=""):
    flood_dataset = load_geojson(DEFAULT_FLOOD_RISK)
    renabap_dataset = load_geojson(DEFAULT_RENABAP)
    flood_matches = []
    flood_features = flood_dataset["features"]
    normalized_zone = _plain(zone_name)
    if normalized_zone:
        zoned_features = [
            feature
            for feature in flood_features
            if _plain((feature.get("properties") or {}).get("assigned_zone_name")) == normalized_zone
        ]
        if zoned_features:
            flood_features = zoned_features
    for feature in flood_features:
        if _feature_contains(latitude, longitude, feature):
            props = feature.get("properties") or {}
            flood_matches.append(
                {
                    "risk_score": _score(props.get("risk_score")),
                    "peligrosidad": props.get("peligrosid") or "",
                    "source": props.get("source_name") or "",
                }
            )
    flood_matches.sort(key=lambda item: item.get("risk_score") or 0, reverse=True)

    nearest_renabap = _nearest_feature(latitude, longitude, renabap_dataset["features"], 1000)
    renabap = None
    if nearest_renabap:
        props = nearest_renabap["feature"].get("properties") or {}
        renabap = {
            "distance_m": nearest_renabap["distance_m"],
            "name": props.get("nombre_barrio") or "",
            "families": props.get("cantidad_familias_aproximada"),
            "source": props.get("source_name") or "",
            "note": "Contexto urbano e infraestructura; no es juicio de valor de la propiedad.",
        }
    return {
        "flood_matches": flood_matches[:3],
        "nearest_renabap": renabap,
        "flood_source_configured": flood_dataset["configured"],
        "renabap_source_configured": renabap_dataset["configured"],
    }


def _payload_from_props(props, match_method, source_signature, exact_context=None):
    overall = _score(props.get("overall_location_value_score"))
    components = {
        public_key: _score(props.get(source_key))
        for public_key, source_key in COMPONENT_FIELDS.items()
        if props.get(source_key) not in (None, "")
    }
    risks = {
        public_key: props.get(source_key)
        for public_key, source_key in RISK_FIELDS.items()
        if props.get(source_key) not in (None, "")
    }
    nearest = {
        key: props.get(key)
        for key in NEAREST_FIELDS
        if props.get(key) not in (None, "")
    }
    exact_context = exact_context or {}
    if exact_context.get("flood_matches"):
        risks["exact_flood_matches"] = exact_context["flood_matches"]
    if exact_context.get("nearest_renabap"):
        risks["exact_nearest_renabap"] = exact_context["nearest_renabap"]

    evidence = {
        "matched_zone": props.get("zone_name") or "",
        "match_method": match_method,
        "source_name": "Local generated integration",
        "source_path": str(DEFAULT_INTEGRATED_ZONES),
        "data_confidence": props.get("data_confidence") or "",
        "generated_at": props.get("generated_at") or "",
        "score_methodology": props.get("score_methodology") or "",
        "nearest": nearest,
        "counts": {
            "schools": props.get("schools_count"),
            "health_points": props.get("health_points_count"),
            "sube_points": props.get("sube_points_count"),
            "official_bus_lines": props.get("official_bus_lines_count"),
            "parcel_count": props.get("parcel_count"),
            "census_tract_count": props.get("census_tract_count"),
        },
        "risk_context": risks,
        "data_notes": [
            "Crimen municipal se muestra como contexto separado y no entra al score territorial operativo.",
            "RENABAP se usa como contexto urbano e infraestructura, no como juicio de valor.",
        ],
    }
    return LocationIntelligenceScore(
        overall_score=overall,
        level=_level(overall, props.get("location_value_level") or ""),
        zone_name=props.get("zone_name") or "",
        match_method=match_method,
        confidence=props.get("data_confidence") or "",
        transport_score=components.get("transport_score"),
        education_score=components.get("education_score"),
        health_score=components.get("health_score"),
        flood_penalty_score=_score(props.get("flood_penalty_score")),
        urban_informality_score=components.get("urban_informality_score"),
        environmental_penalty_score=components.get("environmental_penalty_score"),
        development_potential_score=components.get("development_potential_score"),
        in_flood_risk_zone=props.get("in_flood_risk_zone"),
        nearest_renabap_m=_numeric(props.get("nearest_renabap_m")),
        nearest_sube_point_m=_numeric(props.get("nearest_sube_point_m")),
        nearest_school_m=_numeric(props.get("nearest_school_m")),
        nearest_health_center_m=_numeric(props.get("nearest_health_center_m")),
        components=components,
        risks=risks,
        evidence=evidence,
        source_signature=source_signature,
    )


def score_property_location_intelligence(property_obj, zones=None, source_signature=""):
    dataset = None
    if zones is None:
        dataset = load_location_zones()
        zones = dataset["features"]
        source_signature = dataset["signature"]
    location = getattr(property_obj, "location", None)
    exact_context = None
    if location:
        feature = _best_coordinate_match(location.latitude, location.longitude, zones)
        if feature:
            props = feature.get("properties") or {}
            exact_context = _exact_risk_context(
                location.latitude,
                location.longitude,
                props.get("zone_name") or "",
            )
            return _payload_from_props(
                props,
                "coordinates",
                source_signature,
                exact_context,
            )
    feature = _best_zone_match(property_obj, zones)
    if feature:
        return _payload_from_props(
            feature.get("properties") or {},
            "zone",
            source_signature,
        )
    return LocationIntelligenceScore(
        evidence={
            "reason": "sin match territorial",
            "has_location": bool(location),
            "zone_candidates": sorted(_zone_candidates(property_obj)),
        },
        source_signature=source_signature,
    )


def location_intelligence_values(record):
    if not record:
        return {}
    return {
        "overall_score": record.overall_score,
        "level": record.level,
        "zone_name": record.zone_name,
        "match_method": record.match_method,
        "transport_score": record.transport_score,
        "education_score": record.education_score,
        "health_score": record.health_score,
        "flood_penalty_score": record.flood_penalty_score,
        "urban_informality_score": record.urban_informality_score,
        "in_flood_risk_zone": record.in_flood_risk_zone,
    }


def apply_location_intelligence_score(property_obj, score, commit=True):
    from django.utils import timezone

    values = {
        "overall_score": score.overall_score,
        "level": score.level or "",
        "zone_name": score.zone_name or "",
        "match_method": score.match_method or "none",
        "confidence": score.confidence or "",
        "transport_score": score.transport_score,
        "education_score": score.education_score,
        "health_score": score.health_score,
        "flood_penalty_score": score.flood_penalty_score,
        "urban_informality_score": score.urban_informality_score,
        "environmental_penalty_score": score.environmental_penalty_score,
        "development_potential_score": score.development_potential_score,
        "in_flood_risk_zone": score.in_flood_risk_zone,
        "nearest_renabap_m": score.nearest_renabap_m,
        "nearest_sube_point_m": score.nearest_sube_point_m,
        "nearest_school_m": score.nearest_school_m,
        "nearest_health_center_m": score.nearest_health_center_m,
        "components": score.components or {},
        "risks": score.risks or {},
        "evidence": score.evidence or {},
        "source_signature": score.source_signature or "",
        "scored_at": timezone.now(),
    }
    from properties.models import PropertyLocationIntelligence

    if commit:
        record, _created = PropertyLocationIntelligence.objects.update_or_create(
            property=property_obj,
            defaults=values,
        )
        return record

    record = getattr(property_obj, "location_intelligence", None)
    if record is None:
        record = PropertyLocationIntelligence(property=property_obj)
        property_obj.location_intelligence = record
    for key, value in values.items():
        setattr(record, key, value)
    return record


def _sanitize_zone_feature(feature):
    props = feature.get("properties") or {}
    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": {
            "zone_name": props.get("zone_name") or "",
            "overall_score": _score(props.get("overall_location_value_score")),
            "level": _level(
                props.get("overall_location_value_score"),
                props.get("location_value_level") or "",
            ),
            "transport_score": _score(props.get("transport_access_score")),
            "education_score": _score(props.get("education_access_score")),
            "health_score": _score(props.get("health_access_score")),
            "flood_penalty_score": _score(props.get("flood_penalty_score")),
            "in_flood_risk_zone": props.get("in_flood_risk_zone"),
            "urban_informality_score": _score(props.get("urban_informality_score")),
            "nearest_sube_point_m": props.get("nearest_sube_point_m"),
            "nearest_school_m": props.get("nearest_school_m"),
            "nearest_health_center_m": props.get("nearest_health_center_m"),
            "nearest_renabap_m": props.get("nearest_renabap_m"),
            "data_confidence": props.get("data_confidence") or "",
        },
    }


def _sanitize_point_feature(feature, kind):
    props = feature.get("properties") or {}
    label_keys = {
        "education": ("establecimiento_nombre", "direccion"),
        "health": ("nrs", "dom"),
        "sube": ("Ubicación", "Dirección"),
        "renabap": ("nombre_barrio", "localidad"),
        "flood": ("peligrosid", "assigned_zone_name"),
    }
    primary, secondary = label_keys.get(kind, ("name", "address"))
    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": {
            "kind": kind,
            "label": props.get(primary) or props.get(secondary) or "",
            "zone": props.get("assigned_zone_name") or "",
            "source": props.get("source_name") or "",
            "data_confidence": props.get("data_confidence") or "",
            "risk_score": props.get("risk_score"),
        },
    }


def location_intelligence_layers_payload(zone_path=None, include=None, max_features=1200):
    include = set(include or [])
    zone_dataset = load_location_zones(zone_path)
    zones = [_sanitize_zone_feature(feature) for feature in zone_dataset["features"]]
    payload = {
        "configured": zone_dataset["configured"],
        "path": zone_dataset["path"],
        "signature": zone_dataset["signature"],
        "zones": {"type": "FeatureCollection", "features": zones},
        "layers": {},
        "notes": {
            "renabap": "Contexto urbano e infraestructura; no es juicio de valor de la propiedad.",
            "crime": "Crimen municipal queda separado del score territorial.",
        },
    }
    optional_paths = {
        "flood": DEFAULT_FLOOD_RISK,
        "renabap": DEFAULT_RENABAP,
        "transport": DEFAULT_TRANSPORT_ZONES,
        "sube": Path(settings.BASE_DIR) / "data" / "geo" / "transport" / "sube_points_hurlingham.geojson",
        "education": Path(settings.BASE_DIR) / "data" / "geo" / "education" / "education_points_hurlingham.geojson",
        "health": Path(settings.BASE_DIR) / "data" / "geo" / "health" / "health_points_hurlingham.geojson",
    }
    for key, path in optional_paths.items():
        if key not in include:
            continue
        dataset = load_geojson(path)
        features = [
            _sanitize_point_feature(feature, key)
            for feature in dataset["features"][:max_features]
        ]
        payload["layers"][key] = {
            "configured": dataset["configured"],
            "path": dataset["path"],
            "truncated": len(dataset["features"]) > max_features,
            "feature_count": len(dataset["features"]),
            "geojson": {"type": "FeatureCollection", "features": features},
        }
    return payload
