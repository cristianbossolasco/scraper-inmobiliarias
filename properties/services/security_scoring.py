import json
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from properties.services.spatial import haversine_km, point_in_polygon


DEFAULT_SECURITY_GEOJSON = Path(settings.BASE_DIR) / "data" / "seguridad_hurlingham.geojson"
FALLBACK_SECURITY_GEOJSON = (
    Path(settings.BASE_DIR) / "data" / "geo" / "security_zones_hurlingham.geojson"
)
DEFAULT_SECURITY_POINTS_GEOJSON = (
    Path(settings.BASE_DIR) / "data" / "geo" / "security_points_hurlingham.geojson"
)


@dataclass(frozen=True)
class SecurityScore:
    coverage_score: float | None = None
    risk_score: float | None = None
    level: str = ""
    zone_label: str = ""
    source: str = "sin dato"
    evidence: dict | None = None

    @property
    def matched(self):
        return self.coverage_score is not None


def clamp_score(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0, min(100, parsed)), 2)


def risk_from_coverage(score):
    score = clamp_score(score)
    if score is None:
        return None
    return round(100 - score, 2)


def security_level_from_score(score):
    score = clamp_score(score)
    if score is None:
        return ""
    if score >= 65:
        return "alta"
    if score >= 45:
        return "media"
    return "baja"


def load_geojson(path):
    path = Path(path)
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"type": "FeatureCollection", "features": []}
    if payload.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": []}
    payload["features"] = payload.get("features") or []
    return payload


def load_security_zones(path=None):
    primary = Path(path) if path else DEFAULT_SECURITY_GEOJSON
    payload = load_geojson(primary)
    if payload["features"]:
        return {"path": str(primary), "features": payload["features"], "configured": True}
    fallback = FALLBACK_SECURITY_GEOJSON
    payload = load_geojson(fallback)
    return {
        "path": str(fallback),
        "features": payload["features"],
        "configured": bool(payload["features"]),
    }


def load_security_points(path=None):
    points_path = Path(path) if path else DEFAULT_SECURITY_POINTS_GEOJSON
    payload = load_geojson(points_path)
    return {
        "path": str(points_path),
        "features": payload["features"],
        "configured": bool(payload["features"]),
    }


def _feature_score(props):
    return clamp_score(props.get("score") or props.get("security_infrastructure_score"))


def _feature_label(props):
    return props.get("label") or props.get("zone_name") or props.get("name") or "Zona"


def _feature_source(props):
    return props.get("source") or props.get("source_name") or "manual"


def _feature_level(props, score):
    return props.get("security_level") or security_level_from_score(score)


def _point_matches_polygon(latitude, longitude, geometry):
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon" and coordinates:
        return point_in_polygon(latitude, longitude, coordinates[0])
    if geometry_type == "MultiPolygon":
        return any(
            polygon and point_in_polygon(latitude, longitude, polygon[0])
            for polygon in coordinates
        )
    if geometry_type == "Point" and len(coordinates) >= 2:
        return haversine_km(latitude, longitude, coordinates[1], coordinates[0]) <= 0.25
    return False


def nearby_security_points(latitude, longitude, point_features, radius_m=500, limit=8):
    matches = []
    for feature in point_features or []:
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coordinates) < 2:
            continue
        distance_m = haversine_km(latitude, longitude, coordinates[1], coordinates[0]) * 1000
        if distance_m > radius_m:
            continue
        props = feature.get("properties") or {}
        matches.append(
            {
                "distance_m": round(distance_m),
                "security_type": props.get("security_type") or "",
                "name": props.get("name") or props.get("address") or "",
                "source": props.get("source_name") or "",
            }
        )
    matches.sort(key=lambda item: item["distance_m"])
    counts = {}
    for item in matches:
        key = item["security_type"] or "otro"
        counts[key] = counts.get(key, 0) + 1
    return {"count": len(matches), "by_type": counts, "nearest": matches[:limit]}


def score_coordinates(latitude, longitude, zone_features, point_features=None):
    matches = []
    for feature in zone_features or []:
        geometry = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        if not _point_matches_polygon(latitude, longitude, geometry):
            continue
        score = _feature_score(props)
        matches.append(
            {
                "coverage_score": score,
                "risk_score": risk_from_coverage(score),
                "level": _feature_level(props, score),
                "zone_label": _feature_label(props),
                "source": _feature_source(props),
                "props": props,
            }
        )
    if not matches:
        return SecurityScore(evidence=nearby_security_points(latitude, longitude, point_features))
    matches.sort(
        key=lambda item: -1 if item["coverage_score"] is None else item["coverage_score"],
        reverse=True,
    )
    selected = matches[0]
    evidence = {
        "matched_zone": selected["zone_label"],
        "source_notes": selected["props"].get("source_notes") or "",
        "area_km2": selected["props"].get("area_km2"),
        "camera_count": selected["props"].get("camera_count"),
        "safe_stop_count": selected["props"].get("safe_stop_count"),
        "plate_reader_count": selected["props"].get("plate_reader_count"),
        "police_station_count": selected["props"].get("police_station_count"),
        "camera_coverage_250m_pct": selected["props"].get("camera_coverage_250m_pct"),
        "nearest_police_m": selected["props"].get("nearest_police_m"),
        "nearest_camera_m": selected["props"].get("nearest_camera_m"),
        "nearby_points": nearby_security_points(latitude, longitude, point_features),
    }
    return SecurityScore(
        coverage_score=selected["coverage_score"],
        risk_score=selected["risk_score"],
        level=selected["level"],
        zone_label=selected["zone_label"],
        source=selected["source"],
        evidence=evidence,
    )


def score_property_security(property_obj, zones=None, points=None):
    location = getattr(property_obj, "location", None)
    if not location:
        return SecurityScore(evidence={"reason": "sin ubicacion"})
    if zones is None:
        zones = load_security_zones()["features"]
    if points is None:
        points = load_security_points()["features"]
    return score_coordinates(location.latitude, location.longitude, zones, points)


def apply_security_score(property_obj, score, commit=True):
    property_obj.security_coverage_score = score.coverage_score
    property_obj.security_risk_score = score.risk_score
    property_obj.security_level = score.level or ""
    property_obj.security_zone_label = score.zone_label or ""
    property_obj.security_source = score.source or "sin dato"
    property_obj.security_evidence = score.evidence or {}
    if commit:
        from django.utils import timezone

        property_obj.security_scored_at = timezone.now()
        property_obj.save(update_fields=SECURITY_UPDATE_FIELDS)


SECURITY_UPDATE_FIELDS = [
    "security_coverage_score",
    "security_risk_score",
    "security_level",
    "security_zone_label",
    "security_source",
    "security_evidence",
    "security_scored_at",
]


def security_layers_payload(zone_path=None, point_path=None, max_points=1200):
    zone_dataset = load_security_zones(zone_path)
    point_dataset = load_security_points(point_path)
    zones = []
    for feature in zone_dataset["features"]:
        props = feature.get("properties") or {}
        coverage = _feature_score(props)
        zones.append(
            {
                "type": "Feature",
                "geometry": feature.get("geometry"),
                "properties": {
                    "label": _feature_label(props),
                    "coverage_score": coverage,
                    "risk_score": risk_from_coverage(coverage),
                    "security_level": _feature_level(props, coverage),
                    "source": _feature_source(props),
                    "camera_count": props.get("camera_count"),
                    "safe_stop_count": props.get("safe_stop_count"),
                    "plate_reader_count": props.get("plate_reader_count"),
                    "police_station_count": props.get("police_station_count"),
                },
            }
        )
    points = []
    for feature in point_dataset["features"][:max_points]:
        props = feature.get("properties") or {}
        points.append(
            {
                "type": "Feature",
                "geometry": feature.get("geometry"),
                "properties": {
                    "security_type": props.get("security_type") or "",
                    "name": props.get("name") or props.get("address") or "",
                    "zone": props.get("assigned_zone_name") or props.get("municipal_zone") or "",
                },
            }
        )
    return {
        "configured": zone_dataset["configured"],
        "path": zone_dataset["path"],
        "points_path": point_dataset["path"],
        "zones": {"type": "FeatureCollection", "features": zones},
        "points": {"type": "FeatureCollection", "features": points},
    }
