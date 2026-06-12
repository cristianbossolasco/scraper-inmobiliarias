#!/usr/bin/env python3
"""
Generate security GeoJSON artifacts for Hurlingham.

Outputs:
  data/geo/Zonas_Hurlingham_polygons.geojson
  data/geo/security_points_hurlingham.geojson
  data/geo/security_zones_hurlingham.geojson
  data/seguridad_hurlingham.geojson
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import polygonize, transform, unary_union
from shapely.validation import make_valid


WGS84 = "EPSG:4326"
METRIC_CRS = "EPSG:32721"
MUNICIPAL_SECURITY_URL = "https://www.hurlingham.gob.ar/seguridad/"
MUNICIPAL_MAP_URL = "https://www.hurlingham.gob.ar/mapa-de-seguridad/"
WPGMZA_MARKERS_URL = "https://www.hurlingham.gob.ar/wp-json/wpgmza/v1/markers?map_id=6"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

CAMERA_CATEGORY_IDS = {"1", "2", "3"}
EXCLUDED_CATEGORY_IDS = {"4", "5", "6", "7"}
VALID_SECURITY_TYPES = {
    "camera",
    "safe_stop",
    "plate_reader",
    "neighborhood_alarm",
    "police_station",
    "women_police_station",
}

RELATION_IDS = [
    19399338,
    19399339,
    19399340,
    19399341,
    19399342,
    19399343,
    19399344,
    19399345,
    19399346,
    19399347,
    19399348,
    19399349,
    19399350,
    19399351,
    19399352,
    19399353,
    19399354,
    19399482,
    19399483,
    19399484,
    19399485,
    19399486,
    19399628,
    19399629,
    19399630,
    19399631,
    19399632,
    19399633,
    19399634,
    19399635,
    19399746,
    19399747,
    19399748,
    19399749,
    19399750,
    19399751,
    19399752,
    19399753,
    19399754,
    19399755,
    19399756,
    19399757,
    19399758,
    19400449,  # Federal, present in the user's existing zone labels.
]

CATEGORY_NAMES = {
    "1": "Hurlingham Centro",
    "2": "Hurlingham Sur",
    "3": "Hurlingham Norte",
    "4": "Victimas de Terrorismo de Estado",
    "5": "Centros Culturales",
    "6": "Talleres culturales municipales descentralizados",
    "7": "Escuelas y Ballets",
}

POLICE_SEED = [
    {
        "id": "hurlingham_comisaria_1",
        "security_type": "police_station",
        "name": "Comisaria Hurlingham 1 (Centro)",
        "locality": "Hurlingham",
        "address": "Victoria 1321a, Hurlingham, Buenos Aires, Argentina",
        "phone": "011-4662-3333 / 011-4452-8370",
        "coordinates": [-58.64174, -34.58362],
        "source_url": MUNICIPAL_SECURITY_URL,
        "coordinate_source_url": "https://mapcarta.com/es/N573408102",
    },
    {
        "id": "hurlingham_comisaria_2",
        "security_type": "police_station",
        "name": "Comisaria Hurlingham 2",
        "locality": "Villa Tesei",
        "address": "Av. Gobernador Valentin Vergara 2350, Villa Tesei, Hurlingham, Buenos Aires, Argentina",
        "phone": "011-4459-1910 / 011-4459-6142",
        "coordinates": [-58.63454, -34.61476],
        "source_url": MUNICIPAL_SECURITY_URL,
        "coordinate_source_url": "https://mapcarta.com/es/N4340444590",
    },
    {
        "id": "hurlingham_comisaria_3",
        "security_type": "police_station",
        "name": "Comisaria Hurlingham 3",
        "locality": "William C. Morris",
        "address": "Potosi 3490, William C. Morris, Hurlingham, Buenos Aires, Argentina",
        "phone": "011-4665-8402",
        "coordinates": [-58.65863, -34.5786],
        "source_url": MUNICIPAL_SECURITY_URL,
        "coordinate_source_url": "https://mapcarta.com/es/N1443877883",
    },
    {
        "id": "hurlingham_comisaria_4",
        "security_type": "police_station",
        "name": "Comisaria Hurlingham 4",
        "locality": "Villa Tesei",
        "address": "Juan de Langara 750, Villa Tesei, Hurlingham, Buenos Aires, Argentina",
        "phone": "011-4459-8286 / 011-4459-8226",
        "coordinates": [-58.65248, -34.6181],
        "source_url": MUNICIPAL_SECURITY_URL,
        "coordinate_source_url": "https://mapcarta.com/es/N1443877888",
    },
    {
        "id": "hurlingham_comisaria_5",
        "security_type": "police_station",
        "name": "Comisaria Hurlingham 5",
        "locality": "Hurlingham",
        "address": "Thevenin 2285, Hurlingham, Buenos Aires, Argentina",
        "phone": "011-4452-7025",
        "coordinates": [-58.63625, -34.57255],
        "source_url": MUNICIPAL_SECURITY_URL,
        "coordinate_source_url": "https://mapcarta.com/es/W821713445",
    },
    {
        "id": "hurlingham_comisaria_mujer_familia",
        "security_type": "women_police_station",
        "name": "Comisaria de la Mujer y la Familia Hurlingham",
        "locality": "Hurlingham",
        "address": "Handel 1625, Hurlingham, Buenos Aires, Argentina",
        "phone": "011-4662-4200",
        "coordinates": [-58.6285614, -34.5992191],
        "source_url": MUNICIPAL_SECURITY_URL,
        "coordinate_source_url": "https://catalogo.datos.gba.gob.ar/dataset/comisarias-de-la-mujer",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Hurlingham security GeoJSON files.")
    parser.add_argument("--out-dir", default="data/geo", help="Directory for canonical GeoJSON outputs.")
    parser.add_argument(
        "--dashboard-out",
        default="data/seguridad_hurlingham.geojson",
        help="Dashboard-compatible security layer path.",
    )
    parser.add_argument("--nearest-police-cap-m", type=float, default=2500.0)
    parser.add_argument("--camera-buffer-150", type=float, default=150.0)
    parser.add_argument("--camera-buffer-250", type=float, default=250.0)
    parser.add_argument("--min-zones", type=int, default=35)
    return parser.parse_args()


def fetch_json(url: str) -> Any:
    response = requests.get(url, timeout=60, headers={"User-Agent": "hurlingham-security-geojson/1.0"})
    response.raise_for_status()
    return response.json()


def fetch_overpass_relation_data() -> dict[str, Any]:
    relation_query = "\n  ".join(f"rel({relation_id});" for relation_id in RELATION_IDS)
    query = f"[out:json][timeout:180];\n(\n  {relation_query}\n);\nout geom;"
    query = f"[out:json][timeout:180];\n(\n  {relation_query}\n);\nout body geom;\n>;\nout body geom;"
    last_error: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(3):
            try:
                response = requests.post(
                    endpoint,
                    data=query.encode("utf-8"),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "User-Agent": "hurlingham-security-geojson/1.0",
                    },
                    timeout=180,
                )
                response.raise_for_status()
                payload = response.json()
                payload["_overpass_endpoint"] = endpoint
                return payload
            except Exception as exc:  # pragma: no cover - network resilience path
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Overpass request failed: {last_error}")


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def strip_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def classify_marker(title: str) -> str:
    normalized = strip_accents(title).lower()
    if re.search(r"\blpr\b", normalized):
        return "plate_reader"
    if "totem" in normalized or re.search(r"\bp\.?\s*s\.?\b", normalized):
        return "safe_stop"
    return "camera"


def clean_coordinates(lon: float, lat: float) -> list[float]:
    return [round(float(lon), 7), round(float(lat), 7)]


def distance_m(coord_a: tuple[float, float], coord_b: tuple[float, float]) -> float:
    lon_a, lat_a = coord_a
    lon_b, lat_b = coord_b
    radius_m = 6_371_008.8
    d_lat = math.radians(lat_b - lat_a)
    d_lon = math.radians(lon_b - lon_a)
    rad_lat_a = math.radians(lat_a)
    rad_lat_b = math.radians(lat_b)
    value = (
        math.sin(d_lat / 2) ** 2
        + math.cos(rad_lat_a) * math.cos(rad_lat_b) * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius_m * math.asin(min(1.0, math.sqrt(value)))


def round_nested(value: Any, precision: int = 7) -> Any:
    if isinstance(value, float):
        if math.isfinite(value):
            return round(value, precision)
        return None
    if isinstance(value, list):
        return [round_nested(item, precision) for item in value]
    if isinstance(value, tuple):
        return [round_nested(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: round_nested(item, precision) for key, item in value.items()}
    return value


def geometry_mapping(geom: Any) -> dict[str, Any]:
    return round_nested(mapping(geom), 7)


def extract_polygons(geom: Any) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        polygons: list[Polygon] = []
        for part in geom.geoms:
            polygons.extend(extract_polygons(part))
        return polygons
    return []


def way_member_coordinates(member: dict[str, Any], ways_by_id: dict[int, dict[str, Any]]) -> list[tuple[float, float]]:
    geometry = member.get("geometry")
    if geometry is None and member.get("ref") in ways_by_id:
        geometry = ways_by_id[int(member["ref"])].get("geometry")
    return [
        (float(node["lon"]), float(node["lat"]))
        for node in geometry or []
        if "lon" in node and "lat" in node
    ]


def relation_lines(relation: dict[str, Any], ways_by_id: dict[int, dict[str, Any]]) -> list[LineString]:
    lines: list[LineString] = []
    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        if member.get("role") not in ("", "outer", None):
            continue
        coords = way_member_coordinates(member, ways_by_id)
        if len(coords) >= 2:
            lines.append(LineString(coords))
    return lines


def add_small_gap_closure(lines: list[LineString], max_gap_m: float = 75.0) -> list[LineString] | None:
    endpoint_counts: Counter[tuple[float, float]] = Counter()
    endpoint_values: dict[tuple[float, float], tuple[float, float]] = {}
    for line in lines:
        coords = list(line.coords)
        if not coords:
            continue
        for coord in (coords[0], coords[-1]):
            key = (round(float(coord[0]), 7), round(float(coord[1]), 7))
            endpoint_counts[key] += 1
            endpoint_values[key] = (float(coord[0]), float(coord[1]))
    odd_endpoints = [key for key, count in endpoint_counts.items() if count % 2 == 1]
    if len(odd_endpoints) != 2:
        return None
    start = endpoint_values[odd_endpoints[0]]
    end = endpoint_values[odd_endpoints[1]]
    if distance_m(start, end) > max_gap_m:
        return None
    return [*lines, LineString([start, end])]


def polygonize_lines(lines: list[LineString]) -> tuple[Polygon | MultiPolygon | None, str | None]:
    if not lines:
        return None, None

    polygon_candidates = list(polygonize(unary_union(lines)))
    method = "relation_members_polygonized"
    if not polygon_candidates:
        closed_lines = add_small_gap_closure(lines)
        if closed_lines:
            polygon_candidates = list(polygonize(unary_union(closed_lines)))
            method = "relation_members_polygonized_with_small_gap_closure"

    if not polygon_candidates:
        for line in lines:
            coords = list(line.coords)
            if len(coords) >= 4 and coords[0] == coords[-1]:
                polygon_candidates.append(Polygon(coords))
                method = "closed_member_way_polygonized"

    if not polygon_candidates:
        return None, None

    geom = make_valid(unary_union(polygon_candidates))
    polygons = [poly for poly in extract_polygons(geom) if poly.area > 0]
    if not polygons:
        return None, None
    if len(polygons) == 1:
        return polygons[0], method
    return MultiPolygon(polygons), method


def relation_to_geometry(
    relation: dict[str, Any],
    ways_by_id: dict[int, dict[str, Any]],
) -> tuple[Polygon | MultiPolygon | None, str | None]:
    return polygonize_lines(relation_lines(relation, ways_by_id))


def global_polygon_candidates(ways_by_id: dict[int, dict[str, Any]]) -> list[Polygon]:
    lines: list[LineString] = []
    for way in ways_by_id.values():
        coords = [
            (float(node["lon"]), float(node["lat"]))
            for node in way.get("geometry", [])
            if "lon" in node and "lat" in node
        ]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return []
    return [poly for poly in polygonize(unary_union(lines)) if poly.area > 0]


def label_point_for_relation(
    relation: dict[str, Any],
    nodes_by_id: dict[int, dict[str, Any]],
) -> Point | None:
    for member in relation.get("members", []):
        if member.get("type") == "node" and member.get("role") == "label":
            node = nodes_by_id.get(int(member["ref"]))
            if node and "lon" in node and "lat" in node:
                return Point(float(node["lon"]), float(node["lat"]))
    return None


def fallback_geometry_from_label(
    relation: dict[str, Any],
    nodes_by_id: dict[int, dict[str, Any]],
    global_polygons: list[Polygon],
) -> Polygon | None:
    point = label_point_for_relation(relation, nodes_by_id)
    if point is None:
        return None
    candidates = [poly for poly in global_polygons if poly.buffer(1e-12).covers(point)]
    if not candidates:
        return None
    return min(candidates, key=lambda geom: geom.area)


def build_zone_geojson(overpass_payload: dict[str, Any], generated_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    returned_relation_ids: set[int] = set()
    features: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    elements = overpass_payload.get("elements", [])
    ways_by_id = {int(element["id"]): element for element in elements if element.get("type") == "way"}
    nodes_by_id = {int(element["id"]): element for element in elements if element.get("type") == "node"}
    fallback_polygons = global_polygon_candidates(ways_by_id)

    for element in elements:
        if element.get("type") != "relation":
            continue
        relation_id = int(element["id"])
        returned_relation_ids.add(relation_id)
        tags = element.get("tags") or {}
        geom, extraction_method = relation_to_geometry(element, ways_by_id)
        if geom is None:
            geom = fallback_geometry_from_label(element, nodes_by_id, fallback_polygons)
            extraction_method = "global_boundary_polygonized_from_label"
        if geom is None:
            failures.append({"relation_id": relation_id, "name": tags.get("name"), "reason": "polygonize_failed"})
            continue
        props = {
            "id": f"osm_relation_{relation_id}",
            "zone_name": tags.get("name") or f"OSM relation {relation_id}",
            "name": tags.get("name") or f"OSM relation {relation_id}",
            "locality": tags.get("is_in") or tags.get("addr:city"),
            "osm_relation_id": relation_id,
            "source_url": f"https://www.openstreetmap.org/relation/{relation_id}",
            "source_name": "OpenStreetMap Overpass API",
            "zone_extraction_method": extraction_method,
            "generated_at": generated_at,
        }
        features.append({"type": "Feature", "properties": props, "geometry": geometry_mapping(geom)})

    features.sort(key=lambda feature: str(feature["properties"]["zone_name"]))
    missing = sorted(set(RELATION_IDS) - returned_relation_ids)
    metadata = {
        "generated_at": generated_at,
        "crs": WGS84,
        "source_name": "OpenStreetMap Overpass API",
        "overpass_endpoint": overpass_payload.get("_overpass_endpoint"),
        "requested_relation_ids": RELATION_IDS,
        "returned_relation_ids": sorted(returned_relation_ids),
        "missing_relation_ids": missing,
        "polygonize_failures": failures,
    }
    return {"type": "FeatureCollection", "metadata": metadata, "features": features}, metadata


def marker_to_feature(marker: dict[str, Any], generated_at: str) -> dict[str, Any] | None:
    if str(marker.get("map_id")) != "6":
        return None
    category_ids = {str(category_id) for category_id in marker.get("categories") or []}
    if not category_ids.intersection(CAMERA_CATEGORY_IDS):
        return None
    title = str(marker.get("title") or "").strip()
    security_type = classify_marker(title)
    lat = float(marker["lat"])
    lon = float(marker["lng"])
    description_text = strip_html(str(marker.get("description") or ""))
    municipal_id_match = re.search(r"\bID:\s*([A-Za-z0-9_-]+)", description_text)
    zone_match = re.search(r"\bZona:\s*([^|]+?)(?:\s+ID:|$)", description_text)
    map_sector = next((CATEGORY_NAMES[cat] for cat in sorted(category_ids) if cat in CAMERA_CATEGORY_IDS), None)
    marker_id = str(marker.get("id") or f"{security_type}_{lat}_{lon}")
    props = {
        "id": f"hurlingham_{security_type}_{marker_id}",
        "security_type": security_type,
        "name": title,
        "address": marker.get("address") or None,
        "locality": "Hurlingham",
        "map_sector": map_sector,
        "municipal_zone": zone_match.group(1).strip() if zone_match else None,
        "municipal_marker_id": municipal_id_match.group(1) if municipal_id_match else None,
        "source_url": WPGMZA_MARKERS_URL,
        "source_name": "Hurlingham Municipio - WP Google Maps markers map_id=6",
        "extraction_method": "public_rest_endpoint_title_classification",
        "data_confidence": "high",
        "generated_at": generated_at,
        "marker_id": marker_id,
        "marker_category_ids": sorted(category_ids),
        "marker_category_names": [CATEGORY_NAMES.get(cat, cat) for cat in sorted(category_ids)],
        "raw_description_text": description_text or None,
    }
    return {
        "type": "Feature",
        "id": props["id"],
        "properties": props,
        "geometry": {"type": "Point", "coordinates": clean_coordinates(lon, lat)},
    }


def build_police_features(generated_at: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for row in POLICE_SEED:
        props = {
            "id": row["id"],
            "security_type": row["security_type"],
            "name": row["name"],
            "address": row["address"],
            "locality": row["locality"],
            "phone": row["phone"],
            "source_url": row["source_url"],
            "source_name": "Hurlingham Municipio / public police station seed",
            "coordinate_source_url": row["coordinate_source_url"],
            "extraction_method": "seed_from_public_addresses_and_coordinates",
            "data_confidence": "medium" if row["security_type"] == "police_station" else "high",
            "generated_at": generated_at,
        }
        features.append(
            {
                "type": "Feature",
                "id": row["id"],
                "properties": props,
                "geometry": {"type": "Point", "coordinates": clean_coordinates(*row["coordinates"])},
            }
        )
    return features


def dedupe_point_features(features: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, float, float], dict[str, Any]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        coords = feature.get("geometry", {}).get("coordinates") or []
        key = (str(props.get("security_type")), round(float(coords[1]), 7), round(float(coords[0]), 7))
        if key not in deduped:
            deduped[key] = feature
            continue
        current_props = deduped[key].get("properties") or {}
        if len([v for v in props.values() if v not in (None, "", [], {})]) > len(
            [v for v in current_props.values() if v not in (None, "", [], {})]
        ):
            deduped[key] = feature
    return list(deduped.values())


def build_security_points(markers: list[dict[str, Any]], generated_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    marker_features = [feature for marker in markers if (feature := marker_to_feature(marker, generated_at))]
    raw_marker_counts = Counter(feature["properties"]["security_type"] for feature in marker_features)
    all_features = dedupe_point_features([*marker_features, *build_police_features(generated_at)])
    counts = Counter(feature["properties"]["security_type"] for feature in all_features)
    excluded_counts = Counter()
    for marker in markers:
        category_ids = {str(category_id) for category_id in marker.get("categories") or []}
        if str(marker.get("map_id")) != "6" or not category_ids.intersection(CAMERA_CATEGORY_IDS):
            for category_id in category_ids.intersection(EXCLUDED_CATEGORY_IDS):
                excluded_counts[CATEGORY_NAMES.get(category_id, category_id)] += 1

    metadata = {
        "generated_at": generated_at,
        "crs": WGS84,
        "scope": "Partido de Hurlingham, Buenos Aires, Argentina",
        "sources": [
            MUNICIPAL_SECURITY_URL,
            MUNICIPAL_MAP_URL,
            WPGMZA_MARKERS_URL,
            "https://www.openstreetmap.org/",
            "https://catalogo.datos.gba.gob.ar/dataset/comisarias-de-la-mujer",
        ],
        "classification_rules": {
            "plate_reader": "marker title contains LPR",
            "safe_stop": "marker title contains Totem or P.S.",
            "camera": "security marker in categories 1, 2, 3 not matched by the rules above",
        },
        "raw_marker_type_counts": dict(sorted(raw_marker_counts.items())),
        "deduped_security_type_counts": dict(sorted(counts.items())),
        "excluded_non_security_marker_counts": dict(sorted(excluded_counts.items())),
        "neighborhood_alarm_status": "no_public_coordinates_found",
        "neighborhood_alarm_notes": "The municipal security page publishes a count but no public coordinate endpoint was found.",
    }
    return {"type": "FeatureCollection", "metadata": metadata, "features": all_features}, metadata


def feature_to_shape(feature: dict[str, Any]) -> Any:
    return shape(feature["geometry"])


def point_feature_to_point(feature: dict[str, Any]) -> Point:
    coords = feature["geometry"]["coordinates"]
    return Point(float(coords[0]), float(coords[1]))


def transform_geometry(geom: Any, transformer: Transformer) -> Any:
    return transform(transformer.transform, geom)


def norm_positive(values: list[float | None]) -> list[float | None]:
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not valid:
        return [None for _ in values]
    min_value = min(valid)
    max_value = max(valid)
    if max_value == min_value:
        fill_value = 100.0 if max_value > 0 else 0.0
        return [fill_value if value is not None else None for value in values]
    output: list[float | None] = []
    for value in values:
        if value is None:
            output.append(None)
        else:
            output.append(max(0.0, min(100.0, (float(value) - min_value) / (max_value - min_value) * 100.0)))
    return output


def norm_inverse_distance(values: list[float | None], cap_m: float) -> list[float | None]:
    output: list[float | None] = []
    for value in values:
        if value is None:
            output.append(None)
        else:
            clipped = max(0.0, min(cap_m, float(value)))
            output.append(max(0.0, min(100.0, 100.0 * (1.0 - clipped / cap_m))))
    return output


def classify_level(score: float | None) -> str | None:
    if score is None:
        return None
    if score < 20:
        return "muy_baja"
    if score < 40:
        return "baja"
    if score < 60:
        return "media"
    if score < 80:
        return "media_alta"
    return "alta"


def coverage_pct(zone_geoms_m: list[Any], points_m: list[Point], buffer_m: float) -> list[float | None]:
    if not points_m:
        return [None for _ in zone_geoms_m]
    buffered = unary_union([point.buffer(buffer_m, quad_segs=8) for point in points_m])
    output: list[float | None] = []
    for geom in zone_geoms_m:
        area = float(geom.area)
        if area <= 0:
            output.append(None)
        else:
            output.append(round(float(geom.intersection(buffered).area / area * 100.0), 4))
    return output


def nearest_distance_m(zone_geoms_m: list[Any], points_m: list[Point]) -> list[float | None]:
    if not points_m:
        return [None for _ in zone_geoms_m]
    return [round(min(float(zone.distance(point)) for point in points_m), 2) for zone in zone_geoms_m]


def assign_points_to_zones(zone_geoms_m: list[Any], points_m: list[Point], tolerance_m: float = 0.5) -> list[int | None]:
    zone_areas = [float(zone.area) for zone in zone_geoms_m]
    assignments: list[int | None] = []
    for point in points_m:
        covering = [index for index, zone in enumerate(zone_geoms_m) if zone.covers(point)]
        if covering:
            assignments.append(min(covering, key=lambda index: zone_areas[index]))
            continue
        nearby = [
            (float(zone.distance(point)), index)
            for index, zone in enumerate(zone_geoms_m)
            if float(zone.distance(point)) <= tolerance_m
        ]
        assignments.append(min(nearby)[1] if nearby else None)
    return assignments


def build_score(zone_props: list[dict[str, Any]], nearest_cap_m: float) -> list[float | None]:
    component_specs: list[tuple[str, float, list[float | None], str]] = []
    component_specs.append(
        ("cameras_per_km2", 0.20, norm_positive([props["cameras_per_km2"] for props in zone_props]), "normalized")
    )
    component_specs.append(
        (
            "camera_coverage_250m_pct",
            0.25,
            [props["camera_coverage_250m_pct"] for props in zone_props],
            "direct_pct",
        )
    )
    component_specs.append(
        (
            "nearest_police_m",
            0.20,
            norm_inverse_distance([props["nearest_police_m"] for props in zone_props], nearest_cap_m),
            "inverse_distance",
        )
    )
    component_specs.append(
        (
            "police_station_count_per_km2",
            0.10,
            norm_positive([props["police_station_count_per_km2"] for props in zone_props]),
            "normalized",
        )
    )
    component_specs.append(
        (
            "women_police_station_count_per_km2",
            0.05,
            norm_positive([props["women_police_station_count_per_km2"] for props in zone_props]),
            "normalized",
        )
    )
    component_specs.append(
        (
            "safe_stop_count_per_km2",
            0.10,
            norm_positive([props["safe_stop_count_per_km2"] for props in zone_props]),
            "normalized",
        )
    )
    component_specs.append(
        (
            "plate_reader_count_per_km2",
            0.10,
            norm_positive([props["plate_reader_count_per_km2"] for props in zone_props]),
            "normalized",
        )
    )

    scores: list[float | None] = []
    for index, _props in enumerate(zone_props):
        weighted_sum = 0.0
        weight_sum = 0.0
        for _name, weight, values, _method in component_specs:
            value = values[index]
            if value is None:
                continue
            weighted_sum += float(value) * weight
            weight_sum += weight
        scores.append(round(weighted_sum / weight_sum, 2) if weight_sum else None)
    return scores


def build_security_zones(
    zones_geojson: dict[str, Any],
    points_geojson: dict[str, Any],
    generated_at: str,
    nearest_cap_m: float,
    camera_buffer_150: float,
    camera_buffer_250: float,
) -> dict[str, Any]:
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    zone_features = zones_geojson["features"]
    point_features = points_geojson["features"]

    zone_geoms = [feature_to_shape(feature) for feature in zone_features]
    zone_geoms_m = [transform_geometry(geom, to_metric) for geom in zone_geoms]
    points_by_type: dict[str, list[Point]] = {security_type: [] for security_type in VALID_SECURITY_TYPES}
    points_by_type_m: dict[str, list[Point]] = {security_type: [] for security_type in VALID_SECURITY_TYPES}
    point_geoms_m: list[Point] = []
    for feature in point_features:
        security_type = feature["properties"]["security_type"]
        point = point_feature_to_point(feature)
        point_m = transform_geometry(point, to_metric)
        points_by_type[security_type].append(point)
        points_by_type_m[security_type].append(point_m)
        point_geoms_m.append(point_m)

    point_zone_indexes = assign_points_to_zones(zone_geoms_m, point_geoms_m)
    for feature, zone_index in zip(point_features, point_zone_indexes):
        feature["properties"]["within_hurlingham_zones"] = zone_index is not None
        feature["properties"]["assigned_zone_name"] = (
            zone_features[zone_index]["properties"]["zone_name"] if zone_index is not None else None
        )

    zone_props: list[dict[str, Any]] = []
    for zone_index, (feature, geom_m) in enumerate(zip(zone_features, zone_geoms_m)):
        source_props = feature["properties"]
        area_km2 = round(float(geom_m.area / 1_000_000.0), 4)
        props: dict[str, Any] = {
            "zone_name": source_props["zone_name"],
            "locality": source_props.get("locality"),
            "osm_relation_id": source_props.get("osm_relation_id"),
            "area_km2": area_km2,
        }
        for security_type in VALID_SECURITY_TYPES:
            props[f"{security_type}_count"] = 0
        for point_feature, assigned_zone_index in zip(point_features, point_zone_indexes):
            if assigned_zone_index != zone_index:
                continue
            security_type = point_feature["properties"]["security_type"]
            props[f"{security_type}_count"] += 1

        props["camera_count"] = props.pop("camera_count")
        props["safe_stop_count"] = props.pop("safe_stop_count")
        props["plate_reader_count"] = props.pop("plate_reader_count")
        props["neighborhood_alarm_count"] = props.pop("neighborhood_alarm_count")
        props["police_station_count"] = props.pop("police_station_count")
        props["women_police_station_count"] = props.pop("women_police_station_count")
        for security_type, density_name in {
            "camera": "cameras_per_km2",
            "safe_stop": "safe_stop_count_per_km2",
            "plate_reader": "plate_reader_count_per_km2",
            "neighborhood_alarm": "neighborhood_alarm_count_per_km2",
            "police_station": "police_station_count_per_km2",
            "women_police_station": "women_police_station_count_per_km2",
        }.items():
            props[density_name] = (
                round(float(props[f"{security_type}_count"]) / area_km2, 4) if area_km2 > 0 else None
            )
        zone_props.append(props)

    camera_points_m = points_by_type_m["camera"]
    police_points_m = [*points_by_type_m["police_station"], *points_by_type_m["women_police_station"]]
    camera_coverage_150 = coverage_pct(zone_geoms_m, camera_points_m, camera_buffer_150)
    camera_coverage_250 = coverage_pct(zone_geoms_m, camera_points_m, camera_buffer_250)
    nearest_police = nearest_distance_m(zone_geoms_m, police_points_m)
    nearest_camera = nearest_distance_m(zone_geoms_m, camera_points_m)

    for index, props in enumerate(zone_props):
        props["camera_coverage_150m_pct"] = camera_coverage_150[index]
        props["camera_coverage_250m_pct"] = camera_coverage_250[index]
        props["nearest_police_m"] = nearest_police[index]
        props["nearest_camera_m"] = nearest_camera[index]

    scores = build_score(zone_props, nearest_cap_m)
    has_police = bool(points_by_type["police_station"] or points_by_type["women_police_station"])
    has_camera = bool(points_by_type["camera"])
    has_other = bool(points_by_type["safe_stop"] or points_by_type["plate_reader"] or points_by_type["neighborhood_alarm"])
    if has_police and has_camera and has_other:
        confidence = "high"
    elif has_police and has_camera:
        confidence = "medium"
    elif has_police:
        confidence = "low"
    else:
        confidence = "none"

    output_features: list[dict[str, Any]] = []
    for source_feature, props, score in zip(zone_features, zone_props, scores):
        props["security_infrastructure_score"] = score
        props["security_level"] = classify_level(score)
        props["data_confidence"] = confidence
        props["source_notes"] = (
            "Infrastructure score only; not a crime rate. Cameras, safe stops/totems, plate readers, "
            "and police station points come from public sources. Neighborhood alarm coordinates were not public."
        )
        props["generated_at"] = generated_at
        output_features.append({"type": "Feature", "properties": props, "geometry": source_feature["geometry"]})

    metadata = {
        "generated_at": generated_at,
        "crs": WGS84,
        "metric_crs_used_for_calculation": METRIC_CRS,
        "score_description": "Security infrastructure, coverage, and proximity score. It is not a crime score.",
        "score_weights": {
            "cameras_per_km2": 0.20,
            "camera_coverage_250m_pct": 0.25,
            "nearest_police_m": 0.20,
            "police_station_count_per_km2": 0.10,
            "women_police_station_count_per_km2": 0.05,
            "safe_stop_count_per_km2": 0.10,
            "plate_reader_count_per_km2": 0.10,
        },
        "data_confidence": confidence,
    }
    return {"type": "FeatureCollection", "metadata": metadata, "features": output_features}


def build_dashboard_geojson(security_zones: dict[str, Any], generated_at: str) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    for feature in security_zones["features"]:
        props = dict(feature["properties"])
        props["score"] = props.get("security_infrastructure_score")
        props["source"] = "municipal_wp_google_maps+osm+pba_seed"
        props["label"] = props.get("zone_name")
        features.append({"type": "Feature", "properties": props, "geometry": feature["geometry"]})
    return {
        "type": "FeatureCollection",
        "name": "seguridad_hurlingham",
        "metadata": {
            "generated_at": generated_at,
            "source": "municipal_wp_google_maps+osm+pba_seed",
            "compatible_with": "properties.views._load_security_features",
            "score_field": "score",
            "label_field": "label",
            "notes": "Dashboard-compatible copy of data/geo/security_zones_hurlingham.geojson.",
        },
        "features": features,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_outputs(
    zones_polygons: dict[str, Any],
    security_points: dict[str, Any],
    security_zones: dict[str, Any],
    dashboard_geojson: dict[str, Any],
    min_zones: int,
) -> dict[str, Any]:
    for label, payload in {
        "zones_polygons": zones_polygons,
        "security_points": security_points,
        "security_zones": security_zones,
        "dashboard_geojson": dashboard_geojson,
    }.items():
        if payload.get("type") != "FeatureCollection":
            raise ValueError(f"{label} is not a FeatureCollection")

    zone_geom_types = Counter(feature["geometry"]["type"] for feature in zones_polygons["features"])
    if not set(zone_geom_types).issubset({"Polygon", "MultiPolygon"}):
        raise ValueError(f"Zone geometries must be Polygon/MultiPolygon: {dict(zone_geom_types)}")
    if len(zones_polygons["features"]) < min_zones:
        raise ValueError(f"Expected at least {min_zones} zones, got {len(zones_polygons['features'])}")

    point_counts = Counter()
    outside_points = []
    for feature in security_points["features"]:
        geom = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        if geom.get("type") != "Point":
            raise ValueError("All security point geometries must be Point")
        security_type = props.get("security_type")
        if security_type not in VALID_SECURITY_TYPES:
            raise ValueError(f"Invalid security_type: {security_type}")
        lon, lat = geom["coordinates"]
        if not (-58.72 <= lon <= -58.58 and -34.66 <= lat <= -34.54):
            outside_points.append(props.get("id"))
        categories = {str(category_id) for category_id in props.get("marker_category_ids") or []}
        if categories and not categories.intersection(CAMERA_CATEGORY_IDS):
            raise ValueError(f"Non-security marker included: {props.get('id')} categories={categories}")
        point_counts[security_type] += 1
    if outside_points:
        raise ValueError(f"Points outside broad Hurlingham bounds: {outside_points[:5]}")

    zone_metric_geom_types = Counter(feature["geometry"]["type"] for feature in security_zones["features"])
    if not set(zone_metric_geom_types).issubset({"Polygon", "MultiPolygon"}):
        raise ValueError(f"Security zone geometries must be Polygon/MultiPolygon: {dict(zone_metric_geom_types)}")

    for feature in dashboard_geojson["features"]:
        props = feature.get("properties") or {}
        score = props.get("score")
        if score is None or not (0 <= float(score) <= 100):
            raise ValueError(f"Dashboard feature has invalid score: {props.get('label')} -> {score}")

    return {
        "zone_count": len(zones_polygons["features"]),
        "zone_geometry_types": dict(sorted(zone_geom_types.items())),
        "security_point_count": len(security_points["features"]),
        "security_type_counts": dict(sorted(point_counts.items())),
        "security_zone_count": len(security_zones["features"]),
        "dashboard_feature_count": len(dashboard_geojson["features"]),
    }


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir = Path(args.out_dir)

    overpass_payload = fetch_overpass_relation_data()
    zones_polygons, zones_metadata = build_zone_geojson(overpass_payload, generated_at)
    markers = fetch_json(WPGMZA_MARKERS_URL)
    security_points, points_metadata = build_security_points(markers, generated_at)
    security_zones = build_security_zones(
        zones_polygons,
        security_points,
        generated_at,
        args.nearest_police_cap_m,
        args.camera_buffer_150,
        args.camera_buffer_250,
    )
    dashboard_geojson = build_dashboard_geojson(security_zones, generated_at)

    validation = validate_outputs(
        zones_polygons,
        security_points,
        security_zones,
        dashboard_geojson,
        args.min_zones,
    )

    zones_path = out_dir / "Zonas_Hurlingham_polygons.geojson"
    points_path = out_dir / "security_points_hurlingham.geojson"
    security_zones_path = out_dir / "security_zones_hurlingham.geojson"
    dashboard_path = Path(args.dashboard_out)

    write_json(zones_path, zones_polygons)
    write_json(points_path, security_points)
    write_json(security_zones_path, security_zones)
    write_json(dashboard_path, dashboard_geojson)

    summary = {
        "generated_at": generated_at,
        "outputs": {
            "zones": str(zones_path),
            "points": str(points_path),
            "security_zones": str(security_zones_path),
            "dashboard": str(dashboard_path),
        },
        "validation": validation,
        "zones_metadata": {
            "missing_relation_ids": zones_metadata["missing_relation_ids"],
            "polygonize_failures": zones_metadata["polygonize_failures"],
            "overpass_endpoint": zones_metadata["overpass_endpoint"],
        },
        "points_metadata": {
            "raw_marker_type_counts": points_metadata["raw_marker_type_counts"],
            "deduped_security_type_counts": points_metadata["deduped_security_type_counts"],
            "excluded_non_security_marker_counts": points_metadata["excluded_non_security_marker_counts"],
            "neighborhood_alarm_status": points_metadata["neighborhood_alarm_status"],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
