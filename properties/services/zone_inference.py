import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from properties.models import GeocodeCache

from .geocoding import Geocoder
from .normalization import normalize_neighborhood_name, normalize_whitespace
from .zone_names import canonicalize_unified_zone_name
from .spatial import point_in_polygon, point_to_polygon_distance_m


@dataclass
class ZonePolygon:
    name: str
    raw_name: str
    rings: list
    source: str
    relation_id: str = ""

    def covers(self, latitude, longitude):
        if not self.rings:
            return False
        if not point_in_polygon(latitude, longitude, self.rings[0]):
            return False
        return not any(point_in_polygon(latitude, longitude, hole) for hole in self.rings[1:])

    def distance_m(self, latitude, longitude):
        if not self.rings:
            return None
        return point_to_polygon_distance_m(latitude, longitude, self.rings[0])


@dataclass
class ZoneIndex:
    polygons: list = field(default_factory=list)
    skipped_relations: dict = field(default_factory=dict)


@dataclass
class ZoneInferenceResult:
    inferred_neighborhood: str = ""
    method: str = ""
    distance_m: float = None
    zone_conflict: bool = False
    needs_review: bool = False
    evidence: dict = field(default_factory=dict)
    geocoding_status: str = "not_needed"


def default_geojson_path():
    return Path(settings.ZONE_GEOJSON_PATH)


def property_source_zone(property_obj):
    for value in (property_obj.neighborhood, property_obj.detected_neighborhood):
        normalized = normalize_neighborhood_name(value)
        if normalized:
            return normalized
    return ""


def zones_conflict(source_zone, inferred_zone):
    source = normalize_neighborhood_name(source_zone)
    inferred = normalize_neighborhood_name(inferred_zone)
    return bool(source and inferred and source != inferred)


def load_zone_index(path=None):
    geojson_path = Path(path or default_geojson_path())
    stat = geojson_path.stat()
    return _load_zone_index_cached(str(geojson_path), stat.st_mtime_ns)


@lru_cache(maxsize=4)
def _load_zone_index_cached(path, _mtime):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    features = data.get("features") or []
    polygons = []
    direct_relation_ids = set()
    relation_lines = {}
    relation_names = {}

    for feature in features:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        geometry_type = geometry.get("type")
        if geometry_type in {"Polygon", "MultiPolygon"}:
            raw_name = properties.get("name") or ""
            name = canonicalize_unified_zone_name(normalize_whitespace(raw_name))
            if not name:
                continue
            relation_id = _relation_number(properties.get("@id"))
            if relation_id:
                direct_relation_ids.add(relation_id)
            for rings in _geometry_polygons(geometry):
                polygons.append(
                    ZonePolygon(
                        name=name,
                        raw_name=raw_name,
                        rings=rings,
                        source="polygon",
                        relation_id=relation_id,
                    )
                )
            continue

        if geometry_type != "LineString":
            continue
        coordinates = _clean_coordinates(geometry.get("coordinates") or [], close=False)
        if len(coordinates) < 2:
            continue
        for relation in properties.get("@relations") or []:
            relation_id = str(relation.get("rel") or "")
            reltags = relation.get("reltags") or {}
            raw_name = reltags.get("name") or ""
            if not relation_id or not raw_name:
                continue
            relation_lines.setdefault(relation_id, []).append(coordinates)
            relation_names[relation_id] = raw_name

    skipped = {}
    for relation_id, lines in relation_lines.items():
        if relation_id in direct_relation_ids:
            continue
        raw_name = relation_names.get(relation_id) or ""
        name = canonicalize_unified_zone_name(normalize_whitespace(raw_name))
        if not name:
            skipped[relation_id] = "missing_name"
            continue
        if not _endpoints_can_close(lines):
            skipped[relation_id] = "open_boundary"
            continue
        rings = _assemble_rings(lines)
        if not rings:
            skipped[relation_id] = "cannot_assemble"
            continue
        for ring in rings:
            polygons.append(
                ZonePolygon(
                    name=name,
                    raw_name=raw_name,
                    rings=[ring],
                    source="relation",
                    relation_id=relation_id,
                )
            )

    return ZoneIndex(polygons=polygons, skipped_relations=skipped)


def infer_property_zone(
    property_obj,
    geojson_path=None,
    max_distance_m=None,
    geocode_missing=False,
    geocoder=None,
):
    max_distance = (
        settings.ZONE_INFERENCE_MAX_DISTANCE_M if max_distance_m is None else max_distance_m
    )
    latitude, longitude, coordinate_source, evidence = _coordinates_from_property(property_obj)
    geocoding_status = "not_needed" if latitude is not None and longitude is not None else "no_query"

    if latitude is None or longitude is None:
        geocoder = geocoder or Geocoder()
        query = geocoder.build_query(property_obj)
        evidence["geocode_query"] = query
        if query:
            cache_exists = GeocodeCache.objects.filter(query=query).exists()
            location = geocoder.geocode_property_from_cache(property_obj)
            if location:
                latitude = location.latitude
                longitude = location.longitude
                coordinate_source = "geocode_cache"
                evidence.update(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "provider": location.provider,
                        "precision": location.precision,
                    }
                )
                geocoding_status = "cache_hit" if cache_exists else "cache_applied"
            elif geocode_missing:
                location = geocoder.geocode_property(property_obj)
                if location:
                    latitude = location.latitude
                    longitude = location.longitude
                    coordinate_source = "geocode_external"
                    evidence.update(
                        {
                            "latitude": latitude,
                            "longitude": longitude,
                            "provider": location.provider,
                            "precision": location.precision,
                        }
                    )
                    geocoding_status = "external_hit"
                else:
                    geocoding_status = "external_no_result"
            else:
                geocoding_status = "cache_miss"

    if latitude is None or longitude is None:
        return _result(
            property_obj,
            "",
            "no_coordinates",
            None,
            True,
            {**evidence, "coordinate_source": coordinate_source or ""},
            geocoding_status,
        )

    match = infer_zone_for_point(latitude, longitude, geojson_path, max_distance)
    evidence.update(
        {
            "coordinate_source": coordinate_source,
            "latitude": latitude,
            "longitude": longitude,
            "geojson_path": str(Path(geojson_path or default_geojson_path())),
        }
    )
    if not match["zone"]:
        return _result(
            property_obj,
            "",
            f"{coordinate_source}_no_match",
            None,
            True,
            {**evidence, **match["evidence"]},
            geocoding_status,
        )

    method = f"{coordinate_source}_{match['method']}"
    return _result(
        property_obj,
        match["zone"],
        method,
        match["distance_m"],
        False,
        {**evidence, **match["evidence"]},
        geocoding_status,
    )


def infer_zone_for_point(latitude, longitude, geojson_path=None, max_distance_m=None):
    max_distance = (
        settings.ZONE_INFERENCE_MAX_DISTANCE_M if max_distance_m is None else max_distance_m
    )
    index = load_zone_index(geojson_path)
    nearest = None
    for polygon in index.polygons:
        if polygon.covers(latitude, longitude):
            return {
                "zone": polygon.name,
                "method": "polygon",
                "distance_m": 0,
                "evidence": _polygon_evidence(polygon, index),
            }
        distance = polygon.distance_m(latitude, longitude)
        if distance is not None and (nearest is None or distance < nearest[0]):
            nearest = (distance, polygon)

    if nearest and nearest[0] <= max_distance:
        distance, polygon = nearest
        return {
            "zone": polygon.name,
            "method": "nearest",
            "distance_m": round(distance, 2),
            "evidence": _polygon_evidence(polygon, index),
        }

    evidence = {"skipped_relations": index.skipped_relations}
    if nearest:
        evidence.update(
            {
                "nearest_zone": nearest[1].name,
                "nearest_distance_m": round(nearest[0], 2),
            }
        )
    return {"zone": "", "method": "no_match", "distance_m": None, "evidence": evidence}


def apply_zone_inference(property_obj, result):
    property_obj.inferred_neighborhood = result.inferred_neighborhood
    property_obj.inferred_neighborhood_method = result.method
    property_obj.inferred_neighborhood_distance_m = result.distance_m
    property_obj.zone_conflict = result.zone_conflict
    property_obj.zone_needs_review = result.needs_review
    property_obj.zone_inference_evidence = result.evidence
    property_obj.zone_inferred_at = timezone.now()
    property_obj.save(
        update_fields=[
            "inferred_neighborhood",
            "inferred_neighborhood_method",
            "inferred_neighborhood_distance_m",
            "zone_conflict",
            "zone_needs_review",
            "zone_inference_evidence",
            "zone_inferred_at",
        ]
    )


def _result(
    property_obj,
    inferred_neighborhood,
    method,
    distance_m,
    needs_review,
    evidence,
    geocoding_status,
):
    inferred = canonicalize_unified_zone_name(normalize_whitespace(inferred_neighborhood))
    source = property_source_zone(property_obj)
    return ZoneInferenceResult(
        inferred_neighborhood=inferred,
        method=method,
        distance_m=distance_m,
        zone_conflict=zones_conflict(source, inferred),
        needs_review=needs_review,
        evidence={**evidence, "source_zone": source, "inferred_zone": inferred},
        geocoding_status=geocoding_status,
    )


def _coordinates_from_property(property_obj):
    location = getattr(property_obj, "location", None)
    if location:
        return (
            location.latitude,
            location.longitude,
            "manual_location" if location.manually_corrected else "location",
            {
                "latitude": location.latitude,
                "longitude": location.longitude,
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
            {
                "latitude": property_obj.detected_latitude,
                "longitude": property_obj.detected_longitude,
            },
        )
    return None, None, "", {}


def _polygon_evidence(polygon, index):
    return {
        "zone_raw_name": polygon.raw_name,
        "zone_source": polygon.source,
        "zone_relation_id": polygon.relation_id,
        "skipped_relations": index.skipped_relations,
    }


def _relation_number(value):
    if not value or not str(value).startswith("relation/"):
        return ""
    return str(value).split("/", 1)[1]


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
    return _clean_coordinates(coordinates, close=True)


def _clean_coordinates(coordinates, close):
    ring = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        ring.append((float(point[0]), float(point[1])))
    if close and ring and _point_key(ring[0]) != _point_key(ring[-1]):
        ring.append(ring[0])
    return ring


def _point_key(point):
    return (round(point[0], 7), round(point[1], 7))


def _endpoints_can_close(lines):
    endpoints = {}
    for line in lines:
        endpoints[_point_key(line[0])] = endpoints.get(_point_key(line[0]), 0) + 1
        endpoints[_point_key(line[-1])] = endpoints.get(_point_key(line[-1]), 0) + 1
    return all(count % 2 == 0 for count in endpoints.values())


def _assemble_rings(lines):
    remaining = [list(line) for line in lines if len(line) >= 2]
    rings = []
    while remaining:
        ring = remaining.pop(0)
        while _point_key(ring[0]) != _point_key(ring[-1]):
            current = _point_key(ring[-1])
            match_index = None
            append_points = None
            for index, line in enumerate(remaining):
                if _point_key(line[0]) == current:
                    match_index = index
                    append_points = line[1:]
                    break
                if _point_key(line[-1]) == current:
                    match_index = index
                    append_points = list(reversed(line[:-1]))
                    break
            if match_index is None:
                return []
            remaining.pop(match_index)
            ring.extend(append_points)
        if len(ring) >= 4:
            rings.append(_clean_ring(ring))
    return rings
