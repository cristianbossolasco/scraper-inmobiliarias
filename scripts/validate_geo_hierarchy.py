#!/usr/bin/env python3
"""Validate Hurlingham hierarchy GeoJSON outputs without geopandas."""
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

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity


WGS84 = "EPSG:4326"
METRIC_CRS = "EPSG:32721"
EXPECTED_FILES = {
    "partido": "01_partido_hurlingham.geojson",
    "localidades": "02_localidades_hurlingham.geojson",
    "zonas": "03_zonas_hurlingham_final.geojson",
    "microzonas": "03b_microzonas_hurlingham_final.geojson",
    "gaps": "04_gaps_zonas_hurlingham_final.geojson",
}
EXPECTED_LOCALITIES = {"hurlingham", "villatesei", "williamcmorris"}
AREA_TOLERANCE_KM2 = 0.001


def text_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def label(props: dict[str, Any]) -> str:
    return str(
        props.get("canonical_name")
        or props.get("zone_name")
        or props.get("microzone_name")
        or props.get("locality_name")
        or props.get("name")
        or props.get("label")
        or ""
    ).strip()


class GeoHierarchyValidator:
    def __init__(self, geo_dir: Path, report_path: Path) -> None:
        self.geo_dir = geo_dir
        self.report_path = report_path
        self.to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True).transform
        self.results: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.payloads: dict[str, dict[str, Any]] = {}

    def read_json(self, filename: str) -> dict[str, Any] | None:
        path = self.geo_dir / filename
        if not path.exists():
            self.errors.append(f"missing_file:{filename}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            self.errors.append(f"invalid_json:{filename}:{exc.msg}")
        except OSError as exc:
            self.errors.append(f"read_error:{filename}:{exc}")
        return None

    def area_km2(self, geom: Any) -> float:
        return float(transform(self.to_metric, geom).area / 1_000_000)

    def validate_file(self, key: str, filename: str) -> None:
        payload = self.read_json(filename)
        if payload is None:
            return
        self.payloads[key] = payload
        errors = []
        warnings = []
        if payload.get("type") != "FeatureCollection":
            errors.append("not_feature_collection")
        crs = (payload.get("metadata") or {}).get("crs")
        if crs != WGS84:
            errors.append(f"unexpected_crs:{crs}")
        geom_counts: Counter[str] = Counter()
        labels = []
        area_sum = 0.0
        invalid = []
        for index, feature in enumerate(payload.get("features") or []):
            props = feature.get("properties") or {}
            name = label(props)
            if name:
                labels.append(text_key(name))
            try:
                geom = shape(feature.get("geometry"))
            except Exception as exc:  # noqa: BLE001
                invalid.append((index, f"shape_error:{exc}"))
                continue
            geom_counts[geom.geom_type] += 1
            if geom.is_empty:
                invalid.append((index, "empty"))
                continue
            if not geom.is_valid:
                invalid.append((index, explain_validity(geom)))
            area_sum += self.area_km2(geom)
            if not (-59.1 <= geom.bounds[0] <= -58.2 and -59.1 <= geom.bounds[2] <= -58.2):
                warnings.append(f"longitude_bounds_outlier:{index}")
            if not (-34.9 <= geom.bounds[1] <= -34.0 and -34.9 <= geom.bounds[3] <= -34.0):
                warnings.append(f"latitude_bounds_outlier:{index}")
        duplicates = sorted(name for name, count in Counter(labels).items() if name and count > 1)
        if duplicates:
            errors.append(f"duplicate_labels:{','.join(duplicates[:8])}")
        if invalid:
            errors.append(f"invalid_geometries:{len(invalid)}")
        if not payload.get("features") and key != "microzonas":
            warnings.append("empty_feature_collection")
        self.results.append(
            {
                "file": filename,
                "features": len(payload.get("features") or []),
                "area_km2": round(area_sum, 6),
                "geometry_types": dict(sorted(geom_counts.items())),
                "errors": errors,
                "warnings": warnings,
                "invalid_sample": invalid[:5],
            }
        )
        self.errors.extend(f"{filename}:{error}" for error in errors)
        self.warnings.extend(f"{filename}:{warning}" for warning in warnings)

    def feature_geoms(self, key: str) -> list[tuple[dict[str, Any], Any]]:
        output = []
        for feature in (self.payloads.get(key) or {}).get("features") or []:
            try:
                output.append((feature, shape(feature.get("geometry"))))
            except Exception:
                pass
        return output

    def validate_hierarchy(self) -> None:
        partido_items = self.feature_geoms("partido")
        partido_geoms = [geom for _feature, geom in partido_items]
        if not partido_geoms:
            return
        if len(partido_items) != 1:
            self.errors.append(f"partido_feature_count:{len(partido_items)}")
        partido = unary_union(partido_geoms)
        locality_items = self.feature_geoms("localidades")
        zone_items = self.feature_geoms("zonas")
        micro_items = self.feature_geoms("microzonas")
        gap_items = self.feature_geoms("gaps")
        locality_keys = {
            text_key(label(feature.get("properties") or {}))
            for feature, _geom in locality_items
        }
        if locality_keys != EXPECTED_LOCALITIES:
            expected = ",".join(sorted(EXPECTED_LOCALITIES))
            found = ",".join(sorted(locality_keys))
            self.errors.append(f"unexpected_localities:expected={expected}:found={found}")

        for feature, geom in locality_items:
            name = label(feature.get("properties") or {})
            outside_area = self.area_km2(geom.difference(partido))
            if outside_area > AREA_TOLERANCE_KM2:
                self.errors.append(f"locality_outside_partido:{name}:{outside_area:.4f}")

        for left_index, (left_feature, left_geom) in enumerate(locality_items):
            left_name = label(left_feature.get("properties") or {})
            for right_feature, right_geom in locality_items[left_index + 1 :]:
                right_name = label(right_feature.get("properties") or {})
                overlap_area = self.area_km2(left_geom.intersection(right_geom))
                if overlap_area > AREA_TOLERANCE_KM2:
                    self.errors.append(
                        f"locality_overlap:{left_name}:{right_name}:{overlap_area:.4f}"
                    )

        locality_by_key = {
            text_key(label(feature.get("properties") or {})): geom
            for feature, geom in locality_items
        }
        zone_by_key = {
            text_key(label(feature.get("properties") or {})): geom
            for feature, geom in zone_items
        }
        for feature, geom in zone_items:
            props = feature.get("properties") or {}
            name = label(props)
            locality = props.get("parent_locality") or props.get("locality")
            if not locality:
                self.errors.append(f"zone_missing_locality:{name}")
                continue
            locality_geom = locality_by_key.get(text_key(locality))
            if locality_geom is None:
                self.errors.append(f"zone_unknown_locality:{name}:{locality}")
                continue
            if not locality_geom.intersects(geom):
                self.errors.append(f"zone_not_intersecting_locality:{name}:{locality}")
            outside_area = self.area_km2(geom.difference(locality_geom))
            if outside_area > AREA_TOLERANCE_KM2:
                self.errors.append(f"zone_outside_locality:{name}:{locality}:{outside_area:.4f}")

        for left_index, (left_feature, left_geom) in enumerate(zone_items):
            left_name = label(left_feature.get("properties") or {})
            for right_feature, right_geom in zone_items[left_index + 1 :]:
                right_name = label(right_feature.get("properties") or {})
                overlap_area = self.area_km2(left_geom.intersection(right_geom))
                if overlap_area > AREA_TOLERANCE_KM2:
                    self.errors.append(f"zone_overlap:{left_name}:{right_name}:{overlap_area:.4f}")

        for feature, geom in micro_items:
            props = feature.get("properties") or {}
            name = label(props)
            parent_zone = props.get("parent_zone")
            parent_geom = zone_by_key.get(text_key(parent_zone))
            if parent_geom is None:
                self.errors.append(f"microzone_unknown_parent_zone:{name}:{parent_zone}")
                continue
            outside_area = self.area_km2(geom.difference(parent_geom))
            if outside_area > AREA_TOLERANCE_KM2:
                self.errors.append(f"microzone_outside_parent_zone:{name}:{outside_area:.4f}")
            if props.get("level") != 4:
                self.errors.append(f"microzone_wrong_level:{name}")

        for left_index, (left_feature, left_geom) in enumerate(gap_items):
            left_id = (left_feature.get("properties") or {}).get("gap_id") or label(
                left_feature.get("properties") or {}
            )
            for right_feature, right_geom in gap_items[left_index + 1 :]:
                right_id = (right_feature.get("properties") or {}).get("gap_id") or label(
                    right_feature.get("properties") or {}
                )
                overlap_area = self.area_km2(left_geom.intersection(right_geom))
                if overlap_area > AREA_TOLERANCE_KM2:
                    self.errors.append(f"gap_overlap:{left_id}:{right_id}:{overlap_area:.4f}")

        zone_union = unary_union([geom for _feature, geom in zone_items]) if zone_items else None
        for feature, geom in gap_items:
            props = feature.get("properties") or {}
            gap_id = props.get("gap_id") or label(props)
            outside_area = self.area_km2(geom.difference(partido))
            if outside_area > AREA_TOLERANCE_KM2:
                self.errors.append(f"gap_outside_partido:{gap_id}:{outside_area:.4f}")
            if zone_union is not None:
                overlap = self.area_km2(geom.intersection(zone_union))
                if overlap > AREA_TOLERANCE_KM2:
                    self.errors.append(f"gap_overlaps_zones:{gap_id}:{overlap:.4f}")

    def validate_aliases(self) -> None:
        path = self.geo_dir / "zone_aliases_hurlingham.csv"
        if not path.exists():
            self.errors.append("missing_file:zone_aliases_hurlingham.csv")
            return
        zone_labels = {
            text_key(label(feature.get("properties") or {}))
            for feature in (self.payloads.get("zonas") or {}).get("features") or []
        }
        micro_labels = {
            text_key(label(feature.get("properties") or {}))
            for feature in (self.payloads.get("microzonas") or {}).get("features") or []
        }
        locality_labels = {
            text_key(label(feature.get("properties") or {}))
            for feature in (self.payloads.get("localidades") or {}).get("features") or []
        }
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        seen = set()
        for index, row in enumerate(rows, 2):
            source_key = text_key(row.get("source_text_normalized"))
            if not source_key:
                self.errors.append(f"alias_missing_source_text:row_{index}")
            if source_key in seen:
                self.errors.append(f"alias_duplicate_source_text:{row.get('source_text_normalized')}")
            seen.add(source_key)
            match_type = row.get("match_type") or ""
            canonical_name = text_key(row.get("canonical_name"))
            canonical_zone = text_key(row.get("canonical_zone"))
            canonical_locality = text_key(row.get("canonical_locality"))
            if match_type == "microzona":
                if canonical_name not in micro_labels:
                    self.errors.append(f"alias_unknown_microzone:{row.get('canonical_name')}")
                if canonical_zone not in zone_labels:
                    self.errors.append(f"alias_microzone_unknown_parent_zone:{row.get('canonical_zone')}")
            elif match_type in {"zona", "alias_revisar"}:
                if canonical_zone not in zone_labels:
                    self.errors.append(f"alias_unknown_zone:{row.get('canonical_zone')}")
            if canonical_locality and canonical_locality not in locality_labels:
                self.errors.append(f"alias_unknown_locality:{row.get('canonical_locality')}")
        self.results.append(
            {
                "file": "zone_aliases_hurlingham.csv",
                "features": len(rows),
                "area_km2": None,
                "geometry_types": {},
                "errors": [],
                "warnings": [],
                "invalid_sample": [],
            }
        )

    def write_report(self) -> None:
        generated_at = datetime.now(timezone.utc).isoformat()
        lines = [
            "# Geo Hierarchy Data Quality",
            "",
            f"Generated at: {generated_at}",
            "",
            "## Summary",
            "",
            f"- Files checked: {len(self.results)}",
            f"- Errors: {len(self.errors)}",
            f"- Warnings: {len(self.warnings)}",
            "",
            "## Files",
            "",
        ]
        for result in self.results:
            status = "ERROR" if result["errors"] else "WARN" if result["warnings"] else "OK"
            lines.append(f"### {status} `{result['file']}`")
            lines.append("")
            lines.append(f"- Features/rows: {result['features']}")
            if result["area_km2"] is not None:
                lines.append(f"- Area km2: {result['area_km2']}")
            if result["geometry_types"]:
                lines.append(f"- Geometry types: {result['geometry_types']}")
            if result["errors"]:
                lines.append(f"- Errors: {', '.join(result['errors'])}")
            if result["warnings"]:
                lines.append(f"- Warnings: {', '.join(result['warnings'][:10])}")
            if result["invalid_sample"]:
                lines.append(f"- Invalid sample: {result['invalid_sample']}")
            lines.append("")
        if self.errors:
            lines.extend(["## Error Details", ""])
            for error in self.errors:
                lines.append(f"- {error}")
            lines.append("")
        if self.warnings:
            lines.extend(["## Warning Details", ""])
            for warning in self.warnings:
                lines.append(f"- {warning}")
            lines.append("")
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def run(self) -> int:
        for key, filename in EXPECTED_FILES.items():
            self.validate_file(key, filename)
        self.validate_hierarchy()
        self.validate_aliases()
        self.write_report()
        print(
            json.dumps(
                {
                    "checked": len(self.results),
                    "errors": len(self.errors),
                    "warnings": len(self.warnings),
                    "report": str(self.report_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2 if self.errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Hurlingham geo hierarchy outputs.")
    parser.add_argument("--geo-dir", default="data/geo")
    parser.add_argument("--report", default="docs/geo_hierarchy_data_quality.md")
    args = parser.parse_args()
    return GeoHierarchyValidator(Path(args.geo_dir), Path(args.report)).run()


if __name__ == "__main__":
    raise SystemExit(main())
