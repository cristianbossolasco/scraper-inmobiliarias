#!/usr/bin/env python3
"""Validate generated location-intelligence GeoJSON outputs."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.validation import explain_validity


EXPECTED_GEOJSONS = [
    "data/geo/integrated_location_value_zones_hurlingham.geojson",
    "data/geo/zones/zones_hurlingham.geojson",
    "data/geo/security/security_points_hurlingham.geojson",
    "data/geo/security/security_zones_hurlingham.geojson",
    "data/geo/crime/crime_zones_hurlingham.geojson",
    "data/geo/crime/crime_homicide_radio_points_hurlingham.geojson",
    "data/geo/parcels/parcels_hurlingham.geojson",
    "data/geo/parcels/blocks_hurlingham.geojson",
    "data/geo/parcels/cadastral_zones_hurlingham.geojson",
    "data/geo/parcels/parcel_side_measure_points_hurlingham.geojson",
    "data/geo/census/census_tracts_2022_hurlingham.geojson",
    "data/geo/census/census_zones_hurlingham.geojson",
    "data/geo/renabap/renabap_hurlingham.geojson",
    "data/geo/renabap/renabap_zones_hurlingham.geojson",
    "data/geo/transport/transport_stops_hurlingham.geojson",
    "data/geo/transport/transport_routes_hurlingham.geojson",
    "data/geo/transport/sube_points_hurlingham.geojson",
    "data/geo/transport/transport_zones_hurlingham.geojson",
    "data/geo/amenities/amenities_osm_hurlingham.geojson",
    "data/geo/amenities/green_spaces_hurlingham.geojson",
    "data/geo/amenities/amenities_zones_hurlingham.geojson",
    "data/geo/education/education_points_hurlingham.geojson",
    "data/geo/education/education_zones_hurlingham.geojson",
    "data/geo/health/health_points_hurlingham.geojson",
    "data/geo/health/health_zones_hurlingham.geojson",
    "data/geo/flood/flood_risk_hurlingham.geojson",
    "data/geo/flood/waterways_hurlingham.geojson",
    "data/geo/flood/flood_zones_hurlingham.geojson",
    "data/geo/externalities/externalities_hurlingham.geojson",
    "data/geo/externalities/externalities_zones_hurlingham.geojson",
    "data/geo/utilities/utilities_zones_hurlingham.geojson",
]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def zone_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def feature_zone_labels(features: list[dict[str, Any]]) -> set[str]:
    labels = set()
    for feature in features:
        props = feature.get("properties") or {}
        label = props.get("zone_name") or props.get("name") or props.get("label") or ""
        if label:
            labels.add(zone_key(label))
    return labels


def canonical_zone_feature_count() -> int | None:
    path = Path("data/geo/zones/zones_hurlingham.geojson")
    if not path.exists():
        return None
    payload = read_json(path)
    return len(payload.get("features") or [])


def validate_geojson(path: Path, required_zones: list[str] | None = None) -> dict[str, Any]:
    payload = read_json(path)
    errors: list[str] = []
    warnings: list[str] = []
    if payload.get("type") != "FeatureCollection":
        errors.append("not_a_feature_collection")
    metadata = payload.get("metadata") or {}
    crs = metadata.get("crs")
    if crs != "EPSG:4326":
        errors.append(f"unexpected_crs:{crs}")
    features = payload.get("features") or []
    geom_counts: Counter[str] = Counter()
    invalid = []
    bounds = None
    for idx, feature in enumerate(features):
        geom_payload = feature.get("geometry")
        if not geom_payload:
            invalid.append((idx, "missing_geometry"))
            continue
        try:
            geom = shape(geom_payload)
        except Exception as exc:  # noqa: BLE001
            invalid.append((idx, f"shape_error:{exc}"))
            continue
        geom_counts[geom.geom_type] += 1
        if geom.is_empty:
            invalid.append((idx, "empty_geometry"))
        elif not geom.is_valid:
            invalid.append((idx, explain_validity(geom)))
        minx, miny, maxx, maxy = geom.bounds
        if bounds is None:
            bounds = [minx, miny, maxx, maxy]
        else:
            bounds = [min(bounds[0], minx), min(bounds[1], miny), max(bounds[2], maxx), max(bounds[3], maxy)]
    if invalid:
        errors.append(f"invalid_geometries:{len(invalid)}")
    if not features:
        warnings.append("empty_feature_collection")
    if path.name == "integrated_location_value_zones_hurlingham.geojson":
        expected_zone_count = canonical_zone_feature_count()
        if expected_zone_count is not None and len(features) != expected_zone_count:
            errors.append(f"integrated_expected_{expected_zone_count}_features_got_{len(features)}")
        for idx, feature in enumerate(features):
            props = feature.get("properties") or {}
            score = props.get("overall_location_value_score")
            if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not (0 <= float(score) <= 100)):
                errors.append(f"invalid_overall_score_feature_{idx}:{score}")
            if props.get("crime_spatial_precision") not in {None, "low"}:
                errors.append(f"unexpected_crime_precision_feature_{idx}:{props.get('crime_spatial_precision')}")
    if required_zones and "zones" in path.stem and "hurlingham" in path.stem:
        labels = feature_zone_labels(features)
        for zone in required_zones:
            if zone_key(zone) not in labels:
                errors.append(f"missing_required_zone:{zone}")
    return {
        "path": str(path),
        "feature_count": len(features),
        "geometry_types": dict(sorted(geom_counts.items())),
        "crs": crs,
        "bounds": [round(value, 7) for value in bounds] if bounds else None,
        "errors": errors,
        "warnings": warnings,
        "sample_invalid": invalid[:5],
    }


def write_report(path: Path, results: list[dict[str, Any]]) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Location Intelligence Data Quality Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
    ]
    errors = [result for result in results if result["errors"]]
    warnings = [result for result in results if result["warnings"]]
    lines.append(f"- Files checked: {len(results)}")
    lines.append(f"- Files with errors: {len(errors)}")
    lines.append(f"- Files with warnings: {len(warnings)}")
    lines.append("")
    lines.append("## Layers")
    lines.append("")
    for result in results:
        status = "ERROR" if result["errors"] else "WARN" if result["warnings"] else "OK"
        geom_types = ", ".join(f"{key}:{value}" for key, value in result["geometry_types"].items()) or "none"
        lines.append(f"### {status} `{result['path']}`")
        lines.append("")
        lines.append(f"- Features: {result['feature_count']}")
        lines.append(f"- CRS: {result['crs']}")
        lines.append(f"- Geometry types: {geom_types}")
        lines.append(f"- Bounds: {result['bounds']}")
        if result["errors"]:
            lines.append(f"- Errors: {', '.join(result['errors'])}")
        if result["warnings"]:
            lines.append(f"- Warnings: {', '.join(result['warnings'])}")
        if result["sample_invalid"]:
            lines.append(f"- Sample invalid geometries: {result['sample_invalid']}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def refresh_catalog_counts(catalog_path: Path, results: list[dict[str, Any]]) -> None:
    if not catalog_path.exists():
        return
    by_path = {result["path"].replace("\\", "/"): result for result in results}
    with catalog_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    for row in rows:
        key = row.get("file_path", "").replace("\\", "/")
        result = by_path.get(key)
        if not result:
            continue
        row["feature_count"] = result["feature_count"]
        row["geometry_type"] = ",".join(result["geometry_types"].keys())
        row["crs"] = result["crs"] or row.get("crs", "")
        if result["errors"]:
            row["notes"] = (row.get("notes", "") + f" Validation errors: {result['errors']}").strip()
    with catalog_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate generated location-intelligence GeoJSON files.")
    parser.add_argument("--report", default="docs/data_quality_report.md")
    parser.add_argument("--catalog", default="docs/data_catalog.csv")
    parser.add_argument("--require-zone", action="append", default=[])
    parser.add_argument("paths", nargs="*", default=EXPECTED_GEOJSONS)
    args = parser.parse_args()

    results = []
    missing = []
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.exists():
            missing.append(str(path))
            continue
        results.append(validate_geojson(path, args.require_zone))
    if missing:
        results.append(
            {
                "path": "missing_files",
                "feature_count": 0,
                "geometry_types": {},
                "crs": None,
                "bounds": None,
                "errors": [f"missing:{path}" for path in missing],
                "warnings": [],
                "sample_invalid": [],
            }
        )
    write_report(Path(args.report), results)
    refresh_catalog_counts(Path(args.catalog), results)
    error_count = sum(len(result["errors"]) for result in results)
    print(
        json.dumps(
            {
                "checked": len(results),
                "missing": missing,
                "errors": error_count,
                "report": args.report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
