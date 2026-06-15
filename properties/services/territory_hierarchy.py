import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from properties.services.normalization import normalize_locality, normalize_neighborhood_name
from properties.services.spatial import point_in_polygon, point_to_polygon_distance_m
from properties.services.zone_names import canonicalize_unified_zone_name


LAYER_FILES = {
    "partido": "01_partido_hurlingham.geojson",
    "localidades": "02_localidades_hurlingham.geojson",
    "zonas": "03_zonas_hurlingham_final.geojson",
}


@dataclass(frozen=True)
class TerritoryPolygon:
    level: str
    name: str
    rings: list
    feature_id: str = ""
    parent_locality: str = ""
    source_confidence: str = ""
    source_method: str = ""
    relation_id: str = ""
    needs_manual_review: bool = False

    def covers(self, latitude, longitude):
        if not self.rings or not point_in_polygon(latitude, longitude, self.rings[0]):
            return False
        return not any(point_in_polygon(latitude, longitude, hole) for hole in self.rings[1:])

    def distance_m(self, latitude, longitude):
        if not self.rings:
            return None
        return point_to_polygon_distance_m(latitude, longitude, self.rings[0])


@dataclass(frozen=True)
class TerritoryIndex:
    partido: list[TerritoryPolygon] = field(default_factory=list)
    localidades: list[TerritoryPolygon] = field(default_factory=list)
    zonas: list[TerritoryPolygon] = field(default_factory=list)
    signature: str = ""


@dataclass
class TerritoryInferenceResult:
    partido: str = ""
    locality: str = ""
    zone: str = ""
    confidence: str = ""
    source_method: str = ""
    needs_review: bool = False
    evidence: dict = field(default_factory=dict)


def default_geo_dir():
    return Path(settings.BASE_DIR) / "data" / "geo"


def _file_signature(path):
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None, None)
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def territory_hierarchy_signature(geo_dir=None):
    base = Path(geo_dir) if geo_dir else default_geo_dir()
    return "|".join(
        ":".join(str(part) for part in _file_signature(base / filename))
        for filename in LAYER_FILES.values()
    )


def load_territory_index(geo_dir=None):
    base = Path(geo_dir) if geo_dir else default_geo_dir()
    return _load_territory_index_cached(str(base), territory_hierarchy_signature(base))


@lru_cache(maxsize=8)
def _load_territory_index_cached(geo_dir_str, signature):
    geo_dir = Path(geo_dir_str)
    payloads = {
        key: _read_geojson(geo_dir / filename)
        for key, filename in LAYER_FILES.items()
    }
    return TerritoryIndex(
        partido=_polygons_from_payload(payloads["partido"], "partido"),
        localidades=_polygons_from_payload(payloads["localidades"], "localidad"),
        zonas=_polygons_from_payload(payloads["zonas"], "zona"),
        signature=signature,
    )


def infer_property_territory(property_obj, geo_dir=None):
    latitude, longitude, coordinate_source, coordinate_evidence = _coordinates_from_property(property_obj)
    if latitude is None or longitude is None:
        return TerritoryInferenceResult(
            needs_review=True,
            evidence={
                "reason": "no_coordinates",
                "coordinate_source": coordinate_source or "",
                "signature": territory_hierarchy_signature(geo_dir),
            },
        )
    return infer_territory_for_point(
        latitude,
        longitude,
        geo_dir=geo_dir,
        coordinate_source=coordinate_source,
        extra_evidence=coordinate_evidence,
        source_zone=property_source_zone(property_obj),
        source_locality=property_source_locality(property_obj),
    )


def infer_territory_for_point(
    latitude,
    longitude,
    *,
    geo_dir=None,
    coordinate_source="",
    extra_evidence=None,
    source_zone="",
    source_locality="",
):
    index = load_territory_index(geo_dir)
    partido = _first_covering(index.partido, latitude, longitude)
    locality = _first_covering(index.localidades, latitude, longitude)
    zone = _first_covering(index.zonas, latitude, longitude)
    nearest_zone = _nearest(index.zonas, latitude, longitude)

    evidence = {
        **(extra_evidence or {}),
        "coordinate_source": coordinate_source or "",
        "latitude": latitude,
        "longitude": longitude,
        "signature": index.signature,
        "source_zone": source_zone or "",
        "source_locality": source_locality or "",
    }
    if partido:
        evidence["partido_feature_id"] = partido.feature_id
    if locality:
        evidence["locality_feature_id"] = locality.feature_id
    if zone:
        evidence["zone_feature_id"] = zone.feature_id
        evidence["zone_relation_id"] = zone.relation_id
    if nearest_zone:
        evidence["nearest_zone"] = nearest_zone[1].name
        evidence["nearest_zone_distance_m"] = round(nearest_zone[0], 2)

    normalized_zone = canonicalize_unified_zone_name(zone.name) if zone else ""
    normalized_locality = normalize_locality(locality.name) if locality else ""
    confidence = zone.source_confidence if zone else locality.source_confidence if locality else partido.source_confidence if partido else ""
    source_method = zone.source_method if zone else locality.source_method if locality else partido.source_method if partido else ""
    needs_review = (
        not partido
        or not locality
        or not zone
        or bool(zone and zone.needs_manual_review)
        or _territory_conflict(source_zone, normalized_zone)
        or _locality_conflict(source_locality, normalized_locality)
    )
    return TerritoryInferenceResult(
        partido=partido.name if partido else "",
        locality=normalized_locality,
        zone=normalized_zone,
        confidence=confidence,
        source_method=source_method,
        needs_review=needs_review,
        evidence=evidence,
    )


def apply_territory_inference(property_obj, result):
    property_obj.inferred_partido = result.partido
    property_obj.inferred_locality = result.locality
    property_obj.inferred_zone = result.zone
    property_obj.territory_confidence = result.confidence
    property_obj.territory_source_method = result.source_method
    property_obj.territory_needs_review = result.needs_review
    property_obj.territory_evidence = result.evidence
    property_obj.territory_inferred_at = timezone.now()
    if result.zone:
        property_obj.inferred_neighborhood = result.zone
    property_obj.zone_needs_review = result.needs_review
    property_obj.zone_conflict = _territory_conflict(property_source_zone(property_obj), result.zone)
    property_obj.save(update_fields=[
        "inferred_partido",
        "inferred_locality",
        "inferred_zone",
        "territory_confidence",
        "territory_source_method",
        "territory_needs_review",
        "territory_evidence",
        "territory_inferred_at",
        "inferred_neighborhood",
        "zone_needs_review",
        "zone_conflict",
    ])


def territory_values_from_result(result):
    return {
        "partido": result.partido,
        "locality": result.locality,
        "zone": result.zone,
        "confidence": result.confidence,
        "source_method": result.source_method,
        "needs_review": result.needs_review,
        "evidence": result.evidence,
    }


def property_source_zone(property_obj):
    for value in (
        getattr(property_obj, "neighborhood", ""),
        getattr(property_obj, "detected_neighborhood", ""),
        getattr(property_obj, "inferred_neighborhood", ""),
    ):
        normalized = normalize_neighborhood_name(value)
        if normalized:
            return normalized
    return ""


def property_source_locality(property_obj):
    for value in (
        getattr(property_obj, "detected_locality", ""),
        getattr(property_obj, "locality", ""),
    ):
        normalized = normalize_locality(value)
        if normalized:
            return normalized
    return ""


def _read_geojson(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"type": "FeatureCollection", "features": []}
    if payload.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": []}
    payload["features"] = payload.get("features") or []
    return payload


def _polygons_from_payload(payload, fallback_level):
    output = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        name = _label(props)
        if not name:
            continue
        for rings in _geometry_polygons(feature.get("geometry") or {}):
            output.append(
                TerritoryPolygon(
                    level=props.get("level_name") or fallback_level,
                    name=canonicalize_unified_zone_name(name) if fallback_level == "zona" else name,
                    rings=rings,
                    feature_id=props.get("feature_id") or feature.get("id") or props.get("gap_id") or "",
                    parent_locality=props.get("parent_locality") or props.get("locality") or "",
                    source_confidence=props.get("source_confidence") or "",
                    source_method=props.get("source_method") or "",
                    relation_id=str(props.get("relation_id") or props.get("osm_relation_id") or ""),
                    needs_manual_review=bool(props.get("needs_manual_review")),
                )
            )
    return output


def _label(props):
    return (
        props.get("canonical_name")
        or props.get("zone_name")
        or props.get("locality_name")
        or props.get("partido_name")
        or props.get("name")
        or props.get("label")
        or ""
    )


def _geometry_polygons(geometry):
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        return [_clean_polygon(coordinates)]
    if geometry_type == "MultiPolygon":
        return [_clean_polygon(polygon) for polygon in coordinates]
    return []


def _clean_polygon(rings):
    return [_clean_ring(ring) for ring in rings if len(ring) >= 4]


def _clean_ring(coordinates):
    ring = []
    for point in coordinates:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            ring.append((float(point[0]), float(point[1])))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _first_covering(polygons, latitude, longitude):
    for polygon in polygons:
        if polygon.covers(latitude, longitude):
            return polygon
    return None


def _nearest(polygons, latitude, longitude):
    nearest = None
    for polygon in polygons:
        distance = polygon.distance_m(latitude, longitude)
        if distance is not None and (nearest is None or distance < nearest[0]):
            nearest = (distance, polygon)
    return nearest


def _coordinates_from_property(property_obj):
    location = getattr(property_obj, "location", None)
    if location:
        return (
            location.latitude,
            location.longitude,
            "manual_location" if location.manually_corrected else "location",
            {
                "provider": location.provider,
                "precision": location.precision,
                "manual": location.manually_corrected,
            },
        )
    if property_obj.detected_latitude is not None and property_obj.detected_longitude is not None:
        return (
            property_obj.detected_latitude,
            property_obj.detected_longitude,
            "detected_coordinates",
            {},
        )
    return None, None, "", {}


def _territory_conflict(source_zone, inferred_zone):
    source = normalize_neighborhood_name(source_zone)
    inferred = normalize_neighborhood_name(inferred_zone)
    return bool(source and inferred and source != inferred)


def _locality_conflict(source_locality, inferred_locality):
    source = normalize_locality(source_locality)
    inferred = normalize_locality(inferred_locality)
    return bool(source and inferred and source != inferred)
