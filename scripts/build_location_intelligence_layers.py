#!/usr/bin/env python3
"""Build reproducible Hurlingham location-intelligence GeoJSON layers.

This script is intentionally independent from the Django app and scrapers.
It converts local ARBA shapefiles, normalizes existing generated layers,
downloads a small OSM/Overpass extract, and builds zone-level metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import statistics
import sys
import tarfile
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import shapefile
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid


WGS84 = "EPSG:4326"
ARBA_CRS = "EPSG:5347"
METRIC_CRS = "EPSG:32721"
USER_AGENT = "radar-inmobiliario-location-intelligence/1.0"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

COMMERCIAL_AMENITIES = {
    "atm",
    "bank",
    "cafe",
    "fast_food",
    "fuel",
    "pharmacy",
    "restaurant",
}
COMMERCIAL_SHOPS = {"bakery", "convenience", "greengrocer", "supermarket"}
SCHOOL_AMENITIES = {"college", "kindergarten", "school", "university"}
HEALTH_AMENITIES = {"clinic", "dentist", "doctors", "hospital", "pharmacy"}
GREEN_TAGS = {
    ("leisure", "garden"),
    ("leisure", "park"),
    ("leisure", "pitch"),
    ("leisure", "recreation_ground"),
    ("leisure", "sports_centre"),
    ("landuse", "grass"),
    ("landuse", "recreation_ground"),
}
MAJOR_HIGHWAYS = {"motorway", "trunk", "primary", "secondary", "tertiary"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any], *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def round_nested(value: Any, precision: int = 7) -> Any:
    if isinstance(value, float):
        return round(value, precision) if math.isfinite(value) else None
    if isinstance(value, list):
        return [round_nested(item, precision) for item in value]
    if isinstance(value, tuple):
        return [round_nested(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: round_nested(item, precision) for key, item in value.items()}
    return value


def geometry_mapping(geom: Any) -> dict[str, Any]:
    return round_nested(mapping(geom), 7)


def norm_text(value: Any) -> str:
    text = "" if value is None else str(value).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text)


def zone_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm_text(value)).strip("_")


def clean_property(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    return value


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


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[int(pos)]
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def safe_round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def norm_positive(values: list[float | None]) -> list[float | None]:
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not valid:
        return [None for _ in values]
    min_value = min(valid)
    max_value = max(valid)
    if max_value == min_value:
        fill = 100.0 if max_value > 0 else 0.0
        return [fill if value is not None else None for value in values]
    return [
        None
        if value is None
        else max(0.0, min(100.0, (float(value) - min_value) / (max_value - min_value) * 100.0))
        for value in values
    ]


def norm_inverse_distance(values: list[float | None], cap_m: float) -> list[float | None]:
    output: list[float | None] = []
    for value in values:
        if value is None:
            output.append(None)
        else:
            clipped = max(0.0, min(cap_m, float(value)))
            output.append(max(0.0, min(100.0, 100.0 * (1.0 - clipped / cap_m))))
    return output


def weighted_score(components: Iterable[tuple[float | None, float]]) -> float | None:
    weighted = 0.0
    weights = 0.0
    for value, weight in components:
        if value is None:
            continue
        weighted += float(value) * weight
        weights += weight
    return round(weighted / weights, 2) if weights else None


def make_feature_collection(
    features: list[dict[str, Any]],
    *,
    name: str,
    generated_at: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_metadata = {
        "generated_at": generated_at,
        "crs": WGS84,
        "scope": "Partido de Hurlingham, Buenos Aires, Argentina",
    }
    if metadata:
        payload_metadata.update(metadata)
    return {
        "type": "FeatureCollection",
        "name": name,
        "metadata": payload_metadata,
        "features": features,
    }


def load_zone_context(zones_path: Path) -> dict[str, Any]:
    zones = read_json(zones_path)
    if zones.get("type") != "FeatureCollection":
        raise ValueError(f"Zones file is not a FeatureCollection: {zones_path}")
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    to_arba = Transformer.from_crs(WGS84, ARBA_CRS, always_xy=True)
    features = zones.get("features") or []
    zone_geoms_wgs = [shape(feature["geometry"]) for feature in features]
    zone_geoms_metric = [transform(to_metric.transform, geom) for geom in zone_geoms_wgs]
    zone_geoms_arba = [transform(to_arba.transform, geom) for geom in zone_geoms_wgs]
    zone_union_wgs = unary_union(zone_geoms_wgs)
    zone_union_metric = unary_union(zone_geoms_metric)
    zone_union_arba = unary_union(zone_geoms_arba)
    zone_names = [str((feature.get("properties") or {}).get("zone_name") or "") for feature in features]
    return {
        "payload": zones,
        "features": features,
        "names": zone_names,
        "geoms_wgs": zone_geoms_wgs,
        "geoms_metric": zone_geoms_metric,
        "geoms_arba": zone_geoms_arba,
        "union_wgs": zone_union_wgs,
        "union_metric": zone_union_metric,
        "union_arba": zone_union_arba,
    }


def assign_zone_index(geom: Any, zone_geoms: list[Any]) -> int | None:
    if geom.is_empty:
        return None
    point = geom if isinstance(geom, Point) else geom.representative_point()
    covering = [idx for idx, zone in enumerate(zone_geoms) if zone.covers(point)]
    if covering:
        return min(covering, key=lambda idx: zone_geoms[idx].area)
    intersecting = [idx for idx, zone in enumerate(zone_geoms) if zone.intersects(geom)]
    if intersecting:
        return max(intersecting, key=lambda idx: zone_geoms[idx].intersection(geom).area)
    return None


def copy_existing_layers(config: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]]) -> dict[str, Path]:
    outputs: dict[str, Path] = {}
    for layer in config.get("existing_layers", []):
        src = Path(layer["source"])
        dst = Path(layer["output"])
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".geojson":
            payload = read_json(src)
            metadata = dict(payload.get("metadata") or {})
            metadata.setdefault("generated_at", generated_at)
            metadata["normalized_copy_of"] = str(src)
            metadata.setdefault("crs", WGS84)
            payload["metadata"] = metadata
            write_json(dst, payload)
        else:
            shutil.copy2(src, dst)
        feature_count = None
        if dst.suffix.lower() == ".geojson":
            feature_count = len(read_json(dst).get("features") or [])
        elif dst.suffix.lower() == ".csv":
            feature_count = count_csv_rows(dst)
        outputs[layer["key"]] = dst
        catalog.append(
            catalog_row(
                layer_name=layer["key"],
                path=dst,
                source_name=layer.get("source_name"),
                source_url=None,
                downloaded_at=generated_at,
                license_text="See original source metadata",
                geometry_type=None,
                feature_count=feature_count,
                crs=WGS84 if dst.suffix.lower() == ".geojson" else None,
                spatial_precision=layer.get("spatial_precision", "varies"),
                temporal_coverage=layer.get("temporal_coverage"),
                confidence=layer.get("confidence", "medium"),
                notes=f"Normalized copy of {src}",
            )
        )
    return outputs


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return max(0, sum(1 for _line in handle) - 1)


def catalog_row(
    *,
    layer_name: str,
    path: Path,
    source_name: str | None,
    source_url: str | None,
    downloaded_at: str | None,
    license_text: str | None,
    geometry_type: str | None,
    feature_count: int | None,
    crs: str | None,
    spatial_precision: str | None,
    temporal_coverage: str | None,
    confidence: str | None,
    notes: str | None,
) -> dict[str, Any]:
    return {
        "layer_name": layer_name,
        "file_path": str(path),
        "source_name": source_name or "",
        "source_url": source_url or "",
        "downloaded_at": downloaded_at or "",
        "license": license_text or "",
        "geometry_type": geometry_type or "",
        "feature_count": "" if feature_count is None else feature_count,
        "crs": crs or "",
        "spatial_precision": spatial_precision or "",
        "temporal_coverage": temporal_coverage or "",
        "data_confidence": confidence or "",
        "notes": notes or "",
    }


def write_catalog(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "layer_name",
        "file_path",
        "source_name",
        "source_url",
        "downloaded_at",
        "license",
        "geometry_type",
        "feature_count",
        "crs",
        "spatial_precision",
        "temporal_coverage",
        "data_confidence",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def copy_arba_raw_files(arba_dir: Path, raw_arba_dir: Path, layers: list[dict[str, Any]]) -> dict[str, Path]:
    raw_arba_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, Path] = {}
    for layer in layers:
        src = arba_dir / layer["archive"]
        if not src.exists():
            raise FileNotFoundError(f"Missing ARBA archive: {src}")
        dst = raw_arba_dir / layer["archive"]
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        copied[layer["key"]] = dst
    return copied


def convert_shapefile_archive(
    *,
    layer: dict[str, Any],
    archive_path: Path,
    zone_context: dict[str, Any],
    generated_at: str,
    source_config: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    to_wgs = Transformer.from_crs(source_config.get("input_crs", ARBA_CRS), WGS84, always_xy=True)
    features: list[dict[str, Any]] = []
    geom_counts: Counter[str] = Counter()
    zone_counts: Counter[str] = Counter()
    invalid_fixed = 0
    total_records = 0
    outside_count = 0
    area_by_zone: defaultdict[int, list[float]] = defaultdict(list)
    count_by_zone: Counter[int] = Counter()

    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(tmpdir)
        shp_path = next(Path(tmpdir).glob("*.shp"))
        reader = shapefile.Reader(str(shp_path), encoding="utf-8")
        try:
            field_names = [field[0].lower() for field in reader.fields[1:]]
            for shape_record in reader.iterShapeRecords():
                total_records += 1
                geom_native = shape(shape_record.shape.__geo_interface__)
                if geom_native.is_empty:
                    continue
                if not geom_native.is_valid:
                    geom_native = make_valid(geom_native)
                    invalid_fixed += 1
                if not geom_native.intersects(zone_context["union_arba"]):
                    outside_count += 1
                    continue
                zone_idx = assign_zone_index(geom_native, zone_context["geoms_arba"])
                props = {
                    field: clean_property(value)
                    for field, value in zip(field_names, list(shape_record.record))
                }
                props.update(
                    {
                        "source_name": source_config["source_name"],
                        "source_url": source_config["source_url"],
                        "source_layer": layer["title"],
                        "source_archive": archive_path.name,
                        "wfs_type": layer.get("wfs_type"),
                        "input_crs": source_config.get("input_crs", ARBA_CRS),
                        "generated_at": generated_at,
                        "assigned_zone_name": zone_context["names"][zone_idx] if zone_idx is not None else None,
                        "data_confidence": layer.get("confidence", "medium"),
                    }
                )
                geom_wgs = transform(to_wgs.transform, geom_native)
                if not geom_wgs.is_valid:
                    geom_wgs = make_valid(geom_wgs)
                    invalid_fixed += 1
                geom_counts[geom_wgs.geom_type] += 1
                if zone_idx is not None:
                    zone_counts[zone_context["names"][zone_idx]] += 1
                    count_by_zone[zone_idx] += 1
                    if layer["key"] in {"parcela", "manzana"} and geom_native.area > 0:
                        area_by_zone[zone_idx].append(float(geom_native.area))
                features.append({"type": "Feature", "properties": props, "geometry": geometry_mapping(geom_wgs)})
        finally:
            reader.close()

    metadata = {
        "source_name": source_config["source_name"],
        "source_url": source_config["source_url"],
        "wfs_url": source_config["wfs_url"],
        "wfs_type": layer.get("wfs_type"),
        "source_archive": archive_path.name,
        "input_crs": source_config.get("input_crs", ARBA_CRS),
        "metric_crs_used_for_source_area": source_config.get("input_crs", ARBA_CRS),
        "license": source_config.get("license"),
        "data_confidence": layer.get("confidence", "medium"),
        "notes": layer.get("notes"),
        "total_source_records": total_records,
        "output_feature_count": len(features),
        "outside_hurlingham_zone_count": outside_count,
        "invalid_geometries_repaired": invalid_fixed,
        "geometry_type_counts": dict(sorted(geom_counts.items())),
        "assigned_zone_counts": dict(sorted(zone_counts.items())),
    }
    return (
        make_feature_collection(features, name=f"arba_{layer['key']}_hurlingham", generated_at=generated_at, metadata=metadata),
        features,
        {
            "count_by_zone": dict(count_by_zone),
            "area_by_zone": {idx: values for idx, values in area_by_zone.items()},
            "metadata": metadata,
        },
    )


def build_parcel_zone_metrics(
    zone_context: dict[str, Any],
    parcel_stats: dict[str, Any] | None,
    block_stats: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    parcel_stats = parcel_stats or {"count_by_zone": {}, "area_by_zone": {}}
    block_stats = block_stats or {"count_by_zone": {}, "area_by_zone": {}}
    for idx, (zone_name, geom_metric) in enumerate(zip(zone_context["names"], zone_context["geoms_metric"])):
        area_km2 = float(geom_metric.area / 1_000_000)
        parcel_areas = [float(value) for value in parcel_stats.get("area_by_zone", {}).get(idx, []) if value > 0]
        block_areas = [float(value) for value in block_stats.get("area_by_zone", {}).get(idx, []) if value > 0]
        parcel_count = int(parcel_stats.get("count_by_zone", {}).get(idx, 0))
        block_count = int(block_stats.get("count_by_zone", {}).get(idx, 0))
        median_area = percentile(parcel_areas, 0.5)
        p25 = percentile(parcel_areas, 0.25)
        p75 = percentile(parcel_areas, 0.75)
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": round(area_km2, 4),
                "parcel_count": parcel_count,
                "parcels_per_km2": safe_round(parcel_count / area_km2, 4) if area_km2 else None,
                "parcel_area_m2_mean": safe_round(statistics.mean(parcel_areas), 2) if parcel_areas else None,
                "parcel_area_m2_median": safe_round(median_area, 2),
                "parcel_area_m2_p25": safe_round(p25, 2),
                "parcel_area_m2_p75": safe_round(p75, 2),
                "block_count": block_count,
                "block_area_m2_median": safe_round(percentile(block_areas, 0.5), 2),
                "cadastral_data_confidence": "high" if parcel_count else "low",
            }
        )
    return rows


def build_arba_layers(
    *,
    config: dict[str, Any],
    arba_dir: Path,
    raw_dir: Path,
    zone_context: dict[str, Any],
    generated_at: str,
    catalog: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    arba_config = config["arba"]
    copied = copy_arba_raw_files(arba_dir, raw_dir / "arba", arba_config["layers"])
    layer_stats: dict[str, dict[str, Any]] = {}
    for layer in arba_config["layers"]:
        print(f"Converting ARBA {layer['key']} from {copied[layer['key']]}")
        payload, _features, stats = convert_shapefile_archive(
            layer=layer,
            archive_path=copied[layer["key"]],
            zone_context=zone_context,
            generated_at=generated_at,
            source_config=arba_config,
        )
        output_path = Path(layer["output"])
        write_json(output_path, payload, compact=layer["key"] in {"parcela", "medida_lado", "manzana"})
        layer_stats[layer["key"]] = stats
        geom_types = ",".join(sorted(payload["metadata"].get("geometry_type_counts", {}).keys()))
        catalog.append(
            catalog_row(
                layer_name=f"arba_{layer['key']}",
                path=output_path,
                source_name=arba_config["source_name"],
                source_url=arba_config["source_url"],
                downloaded_at=generated_at,
                license_text=arba_config.get("license"),
                geometry_type=geom_types,
                feature_count=len(payload.get("features") or []),
                crs=WGS84,
                spatial_precision="catastro_partido",
                temporal_coverage=None,
                confidence=layer.get("confidence", "medium"),
                notes=layer.get("notes"),
            )
        )
    parcel_zone_rows = build_parcel_zone_metrics(
        zone_context,
        layer_stats.get("parcela"),
        layer_stats.get("manzana"),
    )
    write_parcel_zone_geojson(zone_context, parcel_zone_rows, generated_at, catalog)
    return layer_stats, parcel_zone_rows


def write_parcel_zone_geojson(
    zone_context: dict[str, Any],
    rows: list[dict[str, Any]],
    generated_at: str,
    catalog: list[dict[str, Any]],
) -> None:
    features = []
    for source_feature, row in zip(zone_context["features"], rows):
        props = dict(row)
        props["source_name"] = "ARBA GeoARBA"
        props["source_url"] = "https://www.arba.gov.ar/geoarba/inicio.asp"
        props["generated_at"] = generated_at
        features.append({"type": "Feature", "properties": props, "geometry": source_feature["geometry"]})
    payload = make_feature_collection(
        features,
        name="cadastral_zones_hurlingham",
        generated_at=generated_at,
        metadata={
            "source_name": "ARBA GeoARBA",
            "metric_crs_used_for_calculation": METRIC_CRS,
            "data_confidence": "high",
        },
    )
    out = Path("data/geo/parcels/cadastral_zones_hurlingham.geojson")
    write_json(out, payload)
    catalog.append(
        catalog_row(
            layer_name="cadastral_zones",
            path=out,
            source_name="ARBA GeoARBA",
            source_url="https://www.arba.gov.ar/geoarba/inicio.asp",
            downloaded_at=generated_at,
            license_text="Public official geospatial data; cite ARBA/GeoARBA.",
            geometry_type="Polygon",
            feature_count=len(features),
            crs=WGS84,
            spatial_precision="zone_aggregation_from_cadastre",
            temporal_coverage=None,
            confidence="high",
            notes="Parcel and block metrics aggregated to OSM neighborhood zones.",
        )
    )


def overpass_query(bbox: list[float]) -> str:
    south, west, north, east = bbox
    tag_filter = f"({south},{west},{north},{east})"
    return f"""
[out:json][timeout:120];
(
  node["amenity"~"^(atm|bank|cafe|clinic|college|dentist|doctors|fast_food|fuel|hospital|kindergarten|pharmacy|restaurant|school|university)$"]{tag_filter};
  way["amenity"~"^(atm|bank|cafe|clinic|college|dentist|doctors|fast_food|fuel|hospital|kindergarten|pharmacy|restaurant|school|university)$"]{tag_filter};
  relation["amenity"~"^(clinic|college|hospital|kindergarten|school|university)$"]{tag_filter};
  node["shop"~"^(bakery|convenience|greengrocer|supermarket)$"]{tag_filter};
  way["shop"~"^(bakery|convenience|greengrocer|supermarket)$"]{tag_filter};
  node["highway"="bus_stop"]{tag_filter};
  node["public_transport"~"^(platform|stop_position)$"]{tag_filter};
  node["railway"~"^(station|halt|tram_stop)$"]{tag_filter};
  way["railway"~"^(rail|light_rail|subway|tram)$"]{tag_filter};
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"]{tag_filter};
  way["waterway"]{tag_filter};
  way["leisure"~"^(garden|park|pitch|recreation_ground|sports_centre)$"]{tag_filter};
  relation["leisure"~"^(garden|park|pitch|recreation_ground|sports_centre)$"]{tag_filter};
  way["landuse"~"^(grass|industrial|recreation_ground)$"]{tag_filter};
  relation["landuse"~"^(industrial|recreation_ground)$"]{tag_filter};
  node["amenity"~"^(recycling|waste_transfer_station)$"]{tag_filter};
  way["amenity"~"^(recycling|waste_transfer_station)$"]{tag_filter};
);
out body geom center;
"""


def fetch_overpass(osm_config: dict[str, Any], generated_at: str, *, skip: bool = False) -> dict[str, Any] | None:
    raw_path = Path(osm_config["raw_output"])
    if raw_path.exists():
        payload = read_json(raw_path)
        payload.setdefault("_metadata", {})
        payload["_metadata"].setdefault("loaded_from_cache", True)
        return payload
    if skip:
        return None
    query = overpass_query(osm_config["bbox"])
    last_error: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(2):
            try:
                response = requests.post(
                    endpoint,
                    data=query.encode("utf-8"),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=160,
                )
                response.raise_for_status()
                payload = response.json()
                payload["_metadata"] = {
                    "downloaded_at": generated_at,
                    "source_url": endpoint,
                    "query_scope": "Hurlingham bbox clipped to local zones",
                }
                write_json(raw_path, payload)
                return payload
            except Exception as exc:  # noqa: BLE001 - try fallback endpoint.
                last_error = exc
                time.sleep(2 + attempt)
    print(f"WARNING Overpass unavailable: {last_error}", file=sys.stderr)
    return None


def osm_element_geometry(element: dict[str, Any]) -> Any | None:
    if element.get("type") == "node":
        return Point(float(element["lon"]), float(element["lat"]))
    coords = [(float(node["lon"]), float(node["lat"])) for node in element.get("geometry") or []]
    if len(coords) >= 4 and coords[0] == coords[-1] and is_area_tags(element.get("tags") or {}):
        return Polygon(coords)
    if len(coords) >= 2:
        return LineString(coords)
    center = element.get("center")
    if center and "lon" in center and "lat" in center:
        return Point(float(center["lon"]), float(center["lat"]))
    return None


def is_area_tags(tags: dict[str, Any]) -> bool:
    if tags.get("area") == "yes":
        return True
    if tags.get("amenity") in SCHOOL_AMENITIES | HEALTH_AMENITIES:
        return True
    if tags.get("shop") in COMMERCIAL_SHOPS:
        return True
    if (("leisure", str(tags.get("leisure"))) in GREEN_TAGS) or (("landuse", str(tags.get("landuse"))) in GREEN_TAGS):
        return True
    if tags.get("landuse") == "industrial":
        return True
    return False


def osm_feature(element: dict[str, Any], geom: Any, generated_at: str, source_url: str) -> dict[str, Any]:
    tags = element.get("tags") or {}
    props = {
        "id": f"osm_{element.get('type')}_{element.get('id')}",
        "osm_type": element.get("type"),
        "osm_id": element.get("id"),
        "name": tags.get("name"),
        "amenity": tags.get("amenity"),
        "shop": tags.get("shop"),
        "leisure": tags.get("leisure"),
        "landuse": tags.get("landuse"),
        "highway": tags.get("highway"),
        "railway": tags.get("railway"),
        "waterway": tags.get("waterway"),
        "public_transport": tags.get("public_transport"),
        "source_name": "OpenStreetMap Overpass API",
        "source_url": source_url,
        "generated_at": generated_at,
        "data_confidence": "medium",
    }
    return {"type": "Feature", "properties": props, "geometry": geometry_mapping(geom)}


def build_osm_layers(
    *,
    config: dict[str, Any],
    zone_context: dict[str, Any],
    generated_at: str,
    catalog: list[dict[str, Any]],
    skip_overpass: bool,
) -> dict[str, Any]:
    osm_config = config["osm"]
    payload = fetch_overpass(osm_config, generated_at, skip=skip_overpass)
    source_url = (payload or {}).get("_metadata", {}).get("source_url") or osm_config["source_url"]
    buckets = {
        "amenities": [],
        "green_spaces": [],
        "transport_stops": [],
        "transport_routes": [],
        "education_points": [],
        "health_points": [],
        "waterways": [],
        "externalities": [],
    }
    if payload:
        seen: set[str] = set()
        for element in payload.get("elements") or []:
            tags = element.get("tags") or {}
            geom = osm_element_geometry(element)
            if geom is None or geom.is_empty:
                continue
            if not zone_context["union_wgs"].intersects(geom):
                continue
            key = f"{element.get('type')}:{element.get('id')}"
            if key in seen:
                continue
            seen.add(key)
            feature = osm_feature(element, geom, generated_at, source_url)
            feature["properties"]["assigned_zone_name"] = zone_context["names"][assign_zone_index(geom, zone_context["geoms_wgs"])] if assign_zone_index(geom, zone_context["geoms_wgs"]) is not None else None
            amenity = tags.get("amenity")
            shop = tags.get("shop")
            if amenity in COMMERCIAL_AMENITIES or shop in COMMERCIAL_SHOPS or amenity in SCHOOL_AMENITIES | HEALTH_AMENITIES:
                buckets["amenities"].append(pointified_feature(feature, geom))
            if amenity in SCHOOL_AMENITIES:
                buckets["education_points"].append(pointified_feature(feature, geom))
            if amenity in HEALTH_AMENITIES:
                buckets["health_points"].append(pointified_feature(feature, geom))
            if (("leisure", str(tags.get("leisure"))) in GREEN_TAGS) or (("landuse", str(tags.get("landuse"))) in GREEN_TAGS):
                buckets["green_spaces"].append(feature)
            if tags.get("highway") == "bus_stop" or tags.get("public_transport") in {"platform", "stop_position"} or tags.get("railway") in {"station", "halt", "tram_stop"}:
                buckets["transport_stops"].append(pointified_feature(feature, geom))
            if tags.get("railway") in {"rail", "light_rail", "subway", "tram"}:
                buckets["transport_routes"].append(feature)
            if tags.get("waterway"):
                buckets["waterways"].append(feature)
            if (
                tags.get("highway") in MAJOR_HIGHWAYS
                or tags.get("railway") in {"rail", "light_rail", "subway", "tram"}
                or tags.get("landuse") == "industrial"
                or tags.get("amenity") in {"fuel", "recycling", "waste_transfer_station"}
                or tags.get("waterway")
            ):
                buckets["externalities"].append(feature)

    metadata = {
        "source_name": "OpenStreetMap Overpass API",
        "source_url": source_url,
        "license": osm_config.get("license"),
        "raw_output": osm_config.get("raw_output"),
        "query_bbox": osm_config.get("bbox"),
        "data_confidence": "medium" if payload else "none",
        "notes": "Features are extracted by bbox then clipped/intersected against local Hurlingham zones.",
    }
    outputs = {
        "amenities": ("data/geo/amenities/amenities_osm_hurlingham.geojson", "amenities_osm_hurlingham"),
        "green_spaces": ("data/geo/amenities/green_spaces_hurlingham.geojson", "green_spaces_hurlingham"),
        "transport_stops": ("data/geo/transport/transport_stops_hurlingham.geojson", "transport_stops_hurlingham"),
        "transport_routes": ("data/geo/transport/transport_routes_hurlingham.geojson", "transport_routes_hurlingham"),
        "education_points": ("data/geo/education/education_points_hurlingham.geojson", "education_points_hurlingham"),
        "health_points": ("data/geo/health/health_points_hurlingham.geojson", "health_points_hurlingham"),
        "waterways": ("data/geo/flood/waterways_hurlingham.geojson", "waterways_hurlingham"),
        "externalities": ("data/geo/externalities/externalities_hurlingham.geojson", "externalities_hurlingham"),
    }
    for key, (path_str, name) in outputs.items():
        fc = make_feature_collection(buckets[key], name=name, generated_at=generated_at, metadata=metadata)
        path = Path(path_str)
        write_json(path, fc)
        catalog.append(
            catalog_row(
                layer_name=key,
                path=path,
                source_name="OpenStreetMap Overpass API",
                source_url=source_url,
                downloaded_at=generated_at if payload else None,
                license_text=osm_config.get("license"),
                geometry_type=",".join(sorted({(feature.get("geometry") or {}).get("type", "") for feature in buckets[key]})),
                feature_count=len(buckets[key]),
                crs=WGS84,
                spatial_precision="osm_feature_level",
                temporal_coverage=None,
                confidence="medium" if payload else "none",
                notes="Generated from OSM tags for a base location-intelligence layer.",
            )
        )

    zone_metrics = build_osm_zone_metrics(zone_context, buckets, generated_at, catalog)
    return {"buckets": buckets, "zone_metrics": zone_metrics, "payload_available": bool(payload)}


def pointified_feature(feature: dict[str, Any], geom: Any) -> dict[str, Any]:
    point = geom if isinstance(geom, Point) else geom.representative_point()
    output = {"type": "Feature", "properties": dict(feature["properties"]), "geometry": geometry_mapping(point)}
    output["properties"]["source_geometry_type"] = geom.geom_type
    return output


def shapely_features(features: list[dict[str, Any]], to_metric: Transformer | None = None) -> list[Any]:
    geoms = []
    for feature in features:
        geom = shape(feature["geometry"])
        if to_metric:
            geom = transform(to_metric.transform, geom)
        geoms.append(geom)
    return geoms


def nearest_distance(zone_geom: Any, target_geoms: list[Any]) -> float | None:
    if not target_geoms:
        return None
    return min(float(zone_geom.distance(geom)) for geom in target_geoms)


def count_assigned(features: list[dict[str, Any]], zone_name: str, predicate=None) -> int:
    total = 0
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("assigned_zone_name") != zone_name:
            continue
        if predicate and not predicate(props):
            continue
        total += 1
    return total


def build_osm_zone_metrics(
    zone_context: dict[str, Any],
    buckets: dict[str, list[dict[str, Any]]],
    generated_at: str,
    catalog: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    amenity_geoms_m = shapely_features(buckets["amenities"], to_metric)
    education_geoms_m = shapely_features(buckets["education_points"], to_metric)
    health_geoms_m = shapely_features(buckets["health_points"], to_metric)
    stop_geoms_m = shapely_features(buckets["transport_stops"], to_metric)
    green_geoms_m = shapely_features(buckets["green_spaces"], to_metric)
    route_geoms_m = shapely_features(buckets["transport_routes"], to_metric)
    waterway_geoms_m = shapely_features(buckets["waterways"], to_metric)
    externality_geoms_m = shapely_features(buckets["externalities"], to_metric)

    rows: list[dict[str, Any]] = []
    for zone_name, geom_m in zip(zone_context["names"], zone_context["geoms_metric"]):
        area_km2 = float(geom_m.area / 1_000_000)
        commercial_count = count_assigned(
            buckets["amenities"],
            zone_name,
            lambda props: props.get("amenity") in COMMERCIAL_AMENITIES or props.get("shop") in COMMERCIAL_SHOPS,
        )
        supermarket_count = count_assigned(
            buckets["amenities"],
            zone_name,
            lambda props: props.get("shop") == "supermarket",
        )
        pharmacy_count = count_assigned(
            buckets["amenities"],
            zone_name,
            lambda props: props.get("amenity") == "pharmacy",
        )
        school_count = count_assigned(buckets["education_points"], zone_name)
        health_count = count_assigned(buckets["health_points"], zone_name)
        bus_stop_count = count_assigned(
            buckets["transport_stops"],
            zone_name,
            lambda props: props.get("highway") == "bus_stop" or props.get("public_transport") in {"platform", "stop_position"},
        )
        train_station_count = count_assigned(
            buckets["transport_stops"],
            zone_name,
            lambda props: props.get("railway") in {"station", "halt"},
        )
        green_area_m2 = sum(
            float(geom_m.intersection(green).area)
            for green in green_geoms_m
            if geom_m.intersects(green) and green.geom_type in {"Polygon", "MultiPolygon"}
        )
        industrial_area_m2 = sum(
            float(geom_m.intersection(ext).area)
            for feature, ext in zip(buckets["externalities"], externality_geoms_m)
            if (feature.get("properties") or {}).get("landuse") == "industrial"
            and geom_m.intersects(ext)
            and ext.geom_type in {"Polygon", "MultiPolygon"}
        )
        nearest_bus = nearest_distance(geom_m, [
            geom for feature, geom in zip(buckets["transport_stops"], stop_geoms_m)
            if (feature.get("properties") or {}).get("highway") == "bus_stop"
            or (feature.get("properties") or {}).get("public_transport") in {"platform", "stop_position"}
        ])
        nearest_train = nearest_distance(geom_m, [
            geom for feature, geom in zip(buckets["transport_stops"], stop_geoms_m)
            if (feature.get("properties") or {}).get("railway") in {"station", "halt"}
        ])
        nearest_supermarket = nearest_distance(geom_m, [
            geom for feature, geom in zip(buckets["amenities"], amenity_geoms_m)
            if (feature.get("properties") or {}).get("shop") == "supermarket"
        ])
        nearest_pharmacy = nearest_distance(geom_m, [
            geom for feature, geom in zip(buckets["amenities"], amenity_geoms_m)
            if (feature.get("properties") or {}).get("amenity") == "pharmacy"
        ])
        nearest_park = nearest_distance(geom_m, green_geoms_m)
        nearest_school = nearest_distance(geom_m, education_geoms_m)
        nearest_health = nearest_distance(geom_m, health_geoms_m)
        nearest_waterway = nearest_distance(geom_m, waterway_geoms_m)
        nearest_railway = nearest_distance(geom_m, route_geoms_m)
        nearest_externality = nearest_distance(geom_m, externality_geoms_m)
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": round(area_km2, 4),
                "commercial_pois_count": commercial_count,
                "commercial_pois_per_km2": safe_round(commercial_count / area_km2, 4) if area_km2 else None,
                "supermarkets_count": supermarket_count,
                "pharmacies_count": pharmacy_count,
                "schools_count": school_count,
                "schools_per_km2": safe_round(school_count / area_km2, 4) if area_km2 else None,
                "health_points_count": health_count,
                "health_points_per_km2": safe_round(health_count / area_km2, 4) if area_km2 else None,
                "bus_stop_count": bus_stop_count,
                "bus_stops_per_km2": safe_round(bus_stop_count / area_km2, 4) if area_km2 else None,
                "train_station_count": train_station_count,
                "green_area_m2": safe_round(green_area_m2, 2),
                "green_area_pct": safe_round(green_area_m2 / geom_m.area * 100.0, 4) if geom_m.area else None,
                "industrial_area_m2": safe_round(industrial_area_m2, 2),
                "industrial_area_pct": safe_round(industrial_area_m2 / geom_m.area * 100.0, 4) if geom_m.area else None,
                "nearest_bus_stop_m": safe_round(nearest_bus, 2),
                "nearest_train_station_m": safe_round(nearest_train, 2),
                "nearest_supermarket_m": safe_round(nearest_supermarket, 2),
                "nearest_pharmacy_m": safe_round(nearest_pharmacy, 2),
                "nearest_park_m": safe_round(nearest_park, 2),
                "nearest_school_m": safe_round(nearest_school, 2),
                "nearest_health_center_m": safe_round(nearest_health, 2),
                "distance_to_waterway_m": safe_round(nearest_waterway, 2),
                "distance_to_railway_m": safe_round(nearest_railway, 2),
                "distance_to_externality_m": safe_round(nearest_externality, 2),
            }
        )

    add_osm_scores(rows)
    write_metric_zone_layer(
        zone_context,
        rows,
        generated_at,
        Path("data/geo/amenities/amenities_zones_hurlingham.geojson"),
        "amenities_zones_hurlingham",
        {
            "commercial_pois_count",
            "commercial_pois_per_km2",
            "supermarkets_count",
            "pharmacies_count",
            "nearest_supermarket_m",
            "nearest_pharmacy_m",
            "nearest_park_m",
            "green_area_m2",
            "green_area_pct",
            "amenity_density_score",
            "green_access_score",
            "walkability_proxy_score",
        },
        catalog,
    )
    write_metric_zone_layer(
        zone_context,
        rows,
        generated_at,
        Path("data/geo/transport/transport_zones_hurlingham.geojson"),
        "transport_zones_hurlingham",
        {
            "bus_stop_count",
            "bus_stops_per_km2",
            "train_station_count",
            "nearest_bus_stop_m",
            "nearest_train_station_m",
            "distance_to_railway_m",
            "transport_access_score",
        },
        catalog,
    )
    write_metric_zone_layer(
        zone_context,
        rows,
        generated_at,
        Path("data/geo/education/education_zones_hurlingham.geojson"),
        "education_zones_hurlingham",
        {"schools_count", "schools_per_km2", "nearest_school_m", "education_access_score"},
        catalog,
    )
    write_metric_zone_layer(
        zone_context,
        rows,
        generated_at,
        Path("data/geo/health/health_zones_hurlingham.geojson"),
        "health_zones_hurlingham",
        {"health_points_count", "health_points_per_km2", "nearest_health_center_m", "health_access_score"},
        catalog,
    )
    write_metric_zone_layer(
        zone_context,
        rows,
        generated_at,
        Path("data/geo/flood/flood_zones_hurlingham.geojson"),
        "flood_zones_hurlingham",
        {"distance_to_waterway_m", "flood_penalty_score", "in_flood_risk_zone", "flood_risk_level"},
        catalog,
    )
    write_metric_zone_layer(
        zone_context,
        rows,
        generated_at,
        Path("data/geo/externalities/externalities_zones_hurlingham.geojson"),
        "externalities_zones_hurlingham",
        {"distance_to_railway_m", "distance_to_externality_m", "industrial_area_m2", "industrial_area_pct", "environmental_penalty_score"},
        catalog,
    )
    return {"osm_zone_rows": rows}


def add_osm_scores(rows: list[dict[str, Any]]) -> None:
    commercial_scores = norm_positive([row["commercial_pois_per_km2"] for row in rows])
    supermarket_scores = norm_inverse_distance([row["nearest_supermarket_m"] for row in rows], 2000)
    pharmacy_scores = norm_inverse_distance([row["nearest_pharmacy_m"] for row in rows], 2000)
    green_area_scores = norm_positive([row["green_area_pct"] for row in rows])
    green_distance_scores = norm_inverse_distance([row["nearest_park_m"] for row in rows], 2000)
    bus_density_scores = norm_positive([row["bus_stops_per_km2"] for row in rows])
    bus_distance_scores = norm_inverse_distance([row["nearest_bus_stop_m"] for row in rows], 1500)
    train_distance_scores = norm_inverse_distance([row["nearest_train_station_m"] for row in rows], 3500)
    school_density_scores = norm_positive([row["schools_per_km2"] for row in rows])
    school_distance_scores = norm_inverse_distance([row["nearest_school_m"] for row in rows], 2500)
    health_density_scores = norm_positive([row["health_points_per_km2"] for row in rows])
    health_distance_scores = norm_inverse_distance([row["nearest_health_center_m"] for row in rows], 3000)
    waterway_penalties = [
        None if row["distance_to_waterway_m"] is None else max(0.0, min(100.0, 100.0 * (1.0 - min(row["distance_to_waterway_m"], 1200) / 1200)))
        for row in rows
    ]
    externality_distance_penalties = [
        None if row["distance_to_externality_m"] is None else max(0.0, min(100.0, 100.0 * (1.0 - min(row["distance_to_externality_m"], 1000) / 1000)))
        for row in rows
    ]
    industrial_penalties = norm_positive([row["industrial_area_pct"] for row in rows])

    for idx, row in enumerate(rows):
        row["amenity_density_score"] = weighted_score(
            [
                (commercial_scores[idx], 0.50),
                (supermarket_scores[idx], 0.25),
                (pharmacy_scores[idx], 0.25),
            ]
        )
        row["green_access_score"] = weighted_score(
            [
                (green_area_scores[idx], 0.60),
                (green_distance_scores[idx], 0.40),
            ]
        )
        row["walkability_proxy_score"] = weighted_score(
            [
                (row["amenity_density_score"], 0.50),
                (row["green_access_score"], 0.25),
                (bus_distance_scores[idx], 0.25),
            ]
        )
        row["transport_access_score"] = weighted_score(
            [
                (bus_density_scores[idx], 0.35),
                (bus_distance_scores[idx], 0.35),
                (train_distance_scores[idx], 0.30),
            ]
        )
        row["education_access_score"] = weighted_score(
            [
                (school_density_scores[idx], 0.45),
                (school_distance_scores[idx], 0.55),
            ]
        )
        row["health_access_score"] = weighted_score(
            [
                (health_density_scores[idx], 0.40),
                (health_distance_scores[idx], 0.60),
            ]
        )
        row["flood_penalty_score"] = safe_round(waterway_penalties[idx], 2)
        row["in_flood_risk_zone"] = None
        row["flood_risk_level"] = classify_level(row["flood_penalty_score"])
        row["environmental_penalty_score"] = weighted_score(
            [
                (externality_distance_penalties[idx], 0.70),
                (industrial_penalties[idx], 0.30),
            ]
        )


def write_metric_zone_layer(
    zone_context: dict[str, Any],
    rows: list[dict[str, Any]],
    generated_at: str,
    path: Path,
    name: str,
    keys: set[str],
    catalog: list[dict[str, Any]],
) -> None:
    features = []
    for source_feature, row in zip(zone_context["features"], rows):
        props = {"zone_name": row["zone_name"], "area_km2": row["area_km2"], "generated_at": generated_at}
        props.update({key: row.get(key) for key in sorted(keys)})
        features.append({"type": "Feature", "properties": props, "geometry": source_feature["geometry"]})
    payload = make_feature_collection(
        features,
        name=name,
        generated_at=generated_at,
        metadata={
            "source_name": "OpenStreetMap Overpass API",
            "metric_crs_used_for_calculation": METRIC_CRS,
            "data_confidence": "medium",
        },
    )
    write_json(path, payload)
    catalog.append(
        catalog_row(
            layer_name=name,
            path=path,
            source_name="OpenStreetMap Overpass API",
            source_url="https://overpass-api.de/api/interpreter",
            downloaded_at=generated_at,
            license_text="Open Database License (ODbL)",
            geometry_type="Polygon",
            feature_count=len(features),
            crs=WGS84,
            spatial_precision="zone_aggregation_from_osm",
            temporal_coverage=None,
            confidence="medium",
            notes="Zone-level metrics derived from OSM features.",
        )
    )


def load_features_by_zone(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = read_json(path)
    output = {}
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        key = zone_key(props.get("zone_name") or props.get("label") or props.get("name"))
        if key:
            output[key] = props
    return output


def build_integrated_layer(
    *,
    zone_context: dict[str, Any],
    parcel_zone_rows: list[dict[str, Any]],
    osm_zone_rows: list[dict[str, Any]],
    generated_at: str,
    catalog: list[dict[str, Any]],
) -> None:
    security_by_zone = load_features_by_zone(Path("data/geo/security/security_zones_hurlingham.geojson"))
    crime_by_zone = load_features_by_zone(Path("data/geo/crime/crime_zones_hurlingham.geojson"))
    parcel_by_zone = {zone_key(row["zone_name"]): row for row in parcel_zone_rows}
    osm_by_zone = {zone_key(row["zone_name"]): row for row in osm_zone_rows}
    features = []
    for source_feature, zone_name, geom_metric in zip(zone_context["features"], zone_context["names"], zone_context["geoms_metric"]):
        key = zone_key(zone_name)
        security = security_by_zone.get(key, {})
        crime = crime_by_zone.get(key, {})
        parcel = parcel_by_zone.get(key, {})
        osm = osm_by_zone.get(key, {})
        props = {
            "zone_id": (source_feature.get("properties") or {}).get("id") or key,
            "zone_name": zone_name,
            "locality": (source_feature.get("properties") or {}).get("locality"),
            "area_km2": safe_round(float(geom_metric.area / 1_000_000), 4),
            "security_infrastructure_score": security.get("security_infrastructure_score"),
            "reported_crime_score": None,
            "crime_spatial_precision": crime.get("crime_spatial_precision"),
            "crime_data_scope": crime.get("crime_data_scope"),
            "reported_crimes_total": crime.get("reported_crimes_total"),
            "transport_access_score": osm.get("transport_access_score"),
            "nearest_train_station_m": osm.get("nearest_train_station_m"),
            "nearest_bus_stop_m": osm.get("nearest_bus_stop_m"),
            "bus_stop_count": osm.get("bus_stop_count"),
            "education_access_score": osm.get("education_access_score"),
            "schools_count": osm.get("schools_count"),
            "nearest_school_m": osm.get("nearest_school_m"),
            "health_access_score": osm.get("health_access_score"),
            "nearest_health_center_m": osm.get("nearest_health_center_m"),
            "health_points_count": osm.get("health_points_count"),
            "flood_penalty_score": osm.get("flood_penalty_score"),
            "in_flood_risk_zone": osm.get("in_flood_risk_zone"),
            "distance_to_waterway_m": osm.get("distance_to_waterway_m"),
            "population_density_km2": None,
            "socioeconomic_proxy_score": None,
            "census_service_deficit_proxy": None,
            "urban_informality_score": None,
            "nearest_renabap_m": None,
            "renabap_area_1000m": None,
            "amenity_density_score": osm.get("amenity_density_score"),
            "green_access_score": osm.get("green_access_score"),
            "nearest_park_m": osm.get("nearest_park_m"),
            "green_area_m2": osm.get("green_area_m2"),
            "utility_quality_score": None,
            "development_potential_score": None,
            "zoning_code": None,
            "environmental_penalty_score": osm.get("environmental_penalty_score"),
            "parcel_count": parcel.get("parcel_count"),
            "parcel_area_m2_median": parcel.get("parcel_area_m2_median"),
            "parcels_per_km2": parcel.get("parcels_per_km2"),
            "generated_at": generated_at,
        }
        overall, used_sources = build_location_score(props)
        props["overall_location_value_score"] = overall
        props["location_value_level"] = classify_level(overall)
        props["data_confidence"] = confidence_for(props, used_sources)
        props["sources_count"] = used_sources
        props["score_methodology"] = "Weighted score from available spatially variable components; crime is retained as municipal context and excluded from the score."
        features.append({"type": "Feature", "properties": props, "geometry": source_feature["geometry"]})
    payload = make_feature_collection(
        features,
        name="integrated_location_value_zones_hurlingham",
        generated_at=generated_at,
        metadata={
            "metric_crs_used_for_calculation": METRIC_CRS,
            "score_weights": {
                "security_infrastructure_score": 0.15,
                "transport_access_score": 0.20,
                "education_access_score": 0.15,
                "health_access_score": 0.10,
                "amenity_density_score": 0.15,
                "green_access_score": 0.10,
                "inverse_flood_penalty_score": 0.10,
                "inverse_environmental_penalty_score": 0.05,
            },
            "score_missing_data_policy": "Missing components are omitted and remaining weights are renormalized. If all components are missing, the score is null.",
            "crime_policy": "Municipal crime baseline is not included in the overall score because it does not vary by neighborhood zone.",
        },
    )
    out = Path("data/geo/integrated_location_value_zones_hurlingham.geojson")
    write_json(out, payload)
    catalog.append(
        catalog_row(
            layer_name="integrated_location_value_zones",
            path=out,
            source_name="Local generated integration",
            source_url="",
            downloaded_at=generated_at,
            license_text="Mixed sources; see docs/sources.md",
            geometry_type="Polygon",
            feature_count=len(features),
            crs=WGS84,
            spatial_precision="zone",
            temporal_coverage=None,
            confidence="medium",
            notes="Integrated zone metrics and location-value score.",
        )
    )


def build_location_score(props: dict[str, Any]) -> tuple[float | None, int]:
    components = [
        (props.get("security_infrastructure_score"), 0.15),
        (props.get("transport_access_score"), 0.20),
        (props.get("education_access_score"), 0.15),
        (props.get("health_access_score"), 0.10),
        (props.get("amenity_density_score"), 0.15),
        (props.get("green_access_score"), 0.10),
        (None if props.get("flood_penalty_score") is None else 100.0 - float(props["flood_penalty_score"]), 0.10),
        (
            None
            if props.get("environmental_penalty_score") is None
            else 100.0 - float(props["environmental_penalty_score"]),
            0.05,
        ),
    ]
    used = sum(1 for value, _weight in components if value is not None)
    return weighted_score(components), used


def confidence_for(props: dict[str, Any], used_sources: int) -> str:
    if used_sources >= 6:
        return "medium_high"
    if used_sources >= 4:
        return "medium"
    if used_sources >= 2:
        return "low"
    return "very_low"


def write_unavailable_docs(config: dict[str, Any], generated_at: str) -> None:
    for item in config.get("unavailable_layers", []):
        path = Path(item["folder"]) / item["file"]
        write_text(
            path,
            f"""# {item['title']}

Status: not available in the base robust delivery.

Generated at: {generated_at}

Reason: {item['reason']}

Next step: identify an official downloadable vector source or document a manual digitization workflow before using this layer in valuation metrics.
""",
        )


def write_sources_doc(config: dict[str, Any], generated_at: str) -> None:
    write_text(
        Path("docs/sources.md"),
        f"""# Location Intelligence Sources

Generated at: {generated_at}

## Base Zones

- `data/geo/Zonas_Hurlingham_polygons.geojson`
- Source: OpenStreetMap / Overpass, generated by the existing local security pipeline.
- Use: base zone geometry for all aggregations.

## ARBA GeoARBA

- Source: {config['arba']['source_url']}
- WFS: {config['arba']['wfs_url']}
- Raw files copied to `data/raw/arba/`.
- Input CRS: {config['arba']['input_crs']}
- Output CRS: {WGS84}
- Use: cadastral parcels, blocks, cadastral hierarchy, and side-measure points.

## Existing Local Layers

- Security points/zones are normalized copies of the existing generated artifacts.
- Crime zones/time series are normalized copies of the existing municipal crime pipeline.
- Crime remains municipal scope and low spatial precision; no neighborhood crime distribution is inferred.

## OpenStreetMap

- Source: {config['osm']['source_url']}
- License: {config['osm']['license']}
- Raw extract: `{config['osm']['raw_output']}`
- Use: base transport, amenities, green spaces, waterways, and externality proxy layers.

## Not Available In This Delivery

- Official zoning/FOT/FOS vector layer.
- Official flood-risk polygons.
- Water/sewer network and stable electric outage history.
- RENABAP integration.
""",
    )


def write_recommendations_doc(generated_at: str) -> None:
    write_text(
        Path("docs/location_intelligence_recommendations.md"),
        f"""# Location Intelligence Recommendations

Generated at: {generated_at}

## Useful Maps

- Price per m2 against `overall_location_value_score`.
- Transport access by zone with train/bus proximity.
- Security infrastructure score with municipal crime context shown separately.
- Flood and waterway proximity proxy.
- Amenities, green access, and walkability proxy.
- Externality proximity: rail, major roads, waterways, fuel and industrial areas.
- Parcel-size distribution by zone for development or subdivision context.

## Useful KPIs

- Median USD/m2 by zone compared with Hurlingham median.
- Listing count by zone and publication age.
- `overall_location_value_score` versus median USD/m2.
- `transport_access_score`, `amenity_density_score`, `green_access_score`.
- `security_infrastructure_score` beside `crime_spatial_precision`.
- `parcel_area_m2_median` and `parcels_per_km2`.

## Alerts

- Property priced below zone median with high location-value score.
- Property near waterways or high flood-proxy zone.
- High price but weak transport or amenity access.
- High security infrastructure but municipal crime context remains high.
- Large parcel in high-access zone for possible development review.
- Property near railway, major road, industrial area, fuel station, or waterway.

## Data Notes

- Do not use municipal crime totals as neighborhood crime maps.
- Treat OSM as medium-confidence operational context, not a complete official inventory.
- Keep missing official layers as null rather than inventing zoning, flood, utility, or census attributes.
""",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Hurlingham location-intelligence layers.")
    parser.add_argument("--config", default="config/location_intelligence_sources.json")
    parser.add_argument("--zones", default="data/geo/Zonas_Hurlingham_polygons.geojson")
    parser.add_argument("--arba-dir", default=r"C:\Users\corebi\Downloads\geo")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--scope", default="base", choices=["base"])
    parser.add_argument("--skip-overpass", action="store_true")
    args = parser.parse_args()

    try:
        generated_at = args.generated_at or utc_now()
        config = read_json(Path(args.config))
        zone_context = load_zone_context(Path(args.zones))
        catalog: list[dict[str, Any]] = []
        copy_existing_layers(config, generated_at, catalog)
        _arba_stats, parcel_zone_rows = build_arba_layers(
            config=config,
            arba_dir=Path(args.arba_dir),
            raw_dir=Path(args.raw_dir),
            zone_context=zone_context,
            generated_at=generated_at,
            catalog=catalog,
        )
        osm_result = build_osm_layers(
            config=config,
            zone_context=zone_context,
            generated_at=generated_at,
            catalog=catalog,
            skip_overpass=args.skip_overpass,
        )
        build_integrated_layer(
            zone_context=zone_context,
            parcel_zone_rows=parcel_zone_rows,
            osm_zone_rows=osm_result["zone_metrics"]["osm_zone_rows"],
            generated_at=generated_at,
            catalog=catalog,
        )
        write_unavailable_docs(config, generated_at)
        write_sources_doc(config, generated_at)
        write_recommendations_doc(generated_at)
        write_catalog(Path("docs/data_catalog.csv"), catalog)
        print(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "zone_count": len(zone_context["features"]),
                    "catalog_rows": len(catalog),
                    "overpass_available": osm_result["payload_available"],
                    "integrated_output": "data/geo/integrated_location_value_zones_hurlingham.geojson",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - command should fail with a useful message.
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
