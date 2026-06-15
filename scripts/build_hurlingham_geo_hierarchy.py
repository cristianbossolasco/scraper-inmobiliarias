#!/usr/bin/env python3
"""Build auditable Hurlingham territorial hierarchy GeoJSON layers."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import shapely
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid


WGS84 = "EPSG:4326"
METRIC_CRS = "EPSG:32721"
PACKAGE_ROOT = "hurlingham_geo_hierarchy_package/"
DEFAULT_ZIP = Path(r"C:\Users\corebi\Downloads\hurlingham_geo_hierarchy_package.zip")
UNIFIED_ZONE_NAME = "Hurlingham Centro (Barrio Ingles)"
UNIFIED_ZONE_ALIAS_KEYS = {
    "barrioingles",
    "bingles",
    "ingles",
    "hurlinghamcentro",
    "hurlinghamcentrobarrioingles",
}
EXPECTED_LOCALITIES = ("Hurlingham", "Villa Tesei", "William C. Morris")
ZONE_SOURCE_PRIORITY = {
    "existing_polygon": 90,
    "relation_members_polygonized": 80,
    "relation_members_polygonized_with_small_gap_closure": 75,
    "polygonized_relation_lines": 60,
    "global_boundary_polygonized_from_label": 55,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def canonical_label(props: dict[str, Any]) -> str:
    return normalize_spaces(
        props.get("canonical_name")
        or props.get("zone_name")
        or props.get("microzone_name")
        or props.get("locality_name")
        or props.get("name")
        or props.get("label")
    )


def canonical_zone_name(value: Any) -> str:
    label = normalize_spaces(value)
    if text_key(label) in UNIFIED_ZONE_ALIAS_KEYS:
        return UNIFIED_ZONE_NAME
    return label


def safe_round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, digits) if math.isfinite(parsed) else None


def round_nested(value: Any, precision: int = 7) -> Any:
    if isinstance(value, float):
        return round(value, precision) if math.isfinite(value) else None
    if isinstance(value, tuple):
        return [round_nested(item, precision) for item in value]
    if isinstance(value, list):
        return [round_nested(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: round_nested(item, precision) for key, item in value.items()}
    return value


def clean_props(props: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for key, value in (props or {}).items():
        if value is None:
            output[key] = None
        elif isinstance(value, float):
            output[key] = safe_round(value, 6)
        elif isinstance(value, (int, bool)):
            output[key] = value
        else:
            output[key] = normalize_spaces(value)
    return output


def polygonal_geom(geom: Any) -> Any:
    geom = make_valid(geom)
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return geom
    parts = [
        part
        for part in getattr(geom, "geoms", [])
        if part.geom_type in {"Polygon", "MultiPolygon"} and not part.is_empty
    ]
    return unary_union(parts) if parts else geom


class GeoHierarchyBuilder:
    def __init__(self, zip_path: Path, base_dir: Path, generated_at: str) -> None:
        self.zip_path = zip_path
        self.base_dir = base_dir
        self.generated_at = generated_at
        self.to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True).transform
        self.to_wgs84 = Transformer.from_crs(METRIC_CRS, WGS84, always_xy=True).transform

    def read_package_json(self, relative_path: str) -> dict[str, Any]:
        with zipfile.ZipFile(self.zip_path) as archive:
            return json.loads(
                archive.read(PACKAGE_ROOT + relative_path).decode("utf-8-sig")
            )

    def read_repo_json(self, relative_path: str) -> dict[str, Any]:
        return json.loads((self.base_dir / relative_path).read_text(encoding="utf-8-sig"))

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> None:
        path = self.base_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def make_collection(
        self,
        features: list[dict[str, Any]],
        *,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_metadata = {
            "generated_at": self.generated_at,
            "crs": WGS84,
            "scope": "Partido de Hurlingham, Buenos Aires, Argentina",
        }
        if metadata:
            merged_metadata.update(metadata)
        return {
            "type": "FeatureCollection",
            "name": name,
            "metadata": merged_metadata,
            "features": features,
        }

    def metric_area_km2(self, geom: Any) -> float:
        return float(transform(self.to_metric, geom).area / 1_000_000)

    def geometry_payload(self, geom: Any) -> dict[str, Any]:
        geom = make_valid(geom)
        if not geom.is_valid:
            geom = geom.buffer(0)
        geom = shapely.set_precision(geom, 1e-9, mode="valid_output")
        if not geom.is_valid:
            geom = geom.buffer(0)
        return mapping(geom)

    def geometry_feature(
        self,
        geom: Any,
        props: dict[str, Any],
        *,
        feature_id: str | None = None,
    ) -> dict[str, Any]:
        geom = make_valid(geom)
        if not geom.is_valid:
            geom = geom.buffer(0)
        properties = clean_props(props)
        properties["area_km2"] = safe_round(self.metric_area_km2(geom), 6)
        if feature_id:
            properties["feature_id"] = feature_id
        feature = {
            "type": "Feature",
            "properties": properties,
            "geometry": self.geometry_payload(geom),
        }
        if feature_id:
            feature["id"] = feature_id
        return feature

    def source_features_by_key(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        output = {}
        for feature in payload.get("features") or []:
            key = text_key(canonical_label(feature.get("properties") or {}))
            if key and key not in output:
                output[key] = feature
        return output

    def relation_id_from_props(self, props: dict[str, Any]) -> int | None:
        for key in ("relation_id", "osm_relation_id"):
            value = props.get(key)
            if value in (None, ""):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        raw_id = str(props.get("@id") or "")
        if raw_id.startswith("relation/"):
            try:
                return int(raw_id.split("/", 1)[1])
            except ValueError:
                return None
        return None

    def assign_locality(self, geom: Any, locality_features: list[dict[str, Any]]) -> str:
        best = None
        point = geom.representative_point()
        for feature in locality_features:
            props = feature.get("properties") or {}
            locality_name = canonical_label(props)
            locality_geom = shape(feature["geometry"])
            if locality_geom.covers(point):
                return locality_name
            intersection_area = locality_geom.intersection(geom).area
            if intersection_area > 0 and (best is None or intersection_area > best[0]):
                best = (intersection_area, locality_name)
        return best[1] if best else ""

    def clip_zone_to_locality(self, geom: Any, locality: str, locality_features: list[dict[str, Any]]) -> Any:
        locality_key = text_key(locality)
        for feature in locality_features:
            props = feature.get("properties") or {}
            if text_key(canonical_label(props)) == locality_key:
                return make_valid(polygonal_geom(geom).intersection(shape(feature["geometry"])))
        return polygonal_geom(geom)

    def zone_priority(self, props: dict[str, Any]) -> tuple[int, float, str]:
        method = str(props.get("source_method") or "")
        confidence = str(props.get("source_confidence") or "")
        confidence_bonus = {
            "medium_high": 25,
            "medium": 15,
            "medium_low": 8,
            "low": 2,
            "very_low": 0,
        }.get(confidence, 0)
        priority = ZONE_SOURCE_PRIORITY.get(method, 40) + confidence_bonus
        area = float(props.get("area_km2") or 0)
        return (priority, area, str(props.get("canonical_name") or props.get("zone_name") or ""))

    def normalize_zone_topology(
        self,
        zone_entries: list[tuple[Any, dict[str, Any], str]],
        locality_features: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        occupied_by_locality: dict[str, Any] = {}
        normalized: list[dict[str, Any]] = []
        zone_entries.sort(key=lambda item: self.zone_priority(item[1]), reverse=True)
        for geom, props, feature_id in zone_entries:
            locality = props.get("parent_locality") or props.get("locality") or ""
            locality_key = text_key(locality)
            geom = self.clip_zone_to_locality(geom, locality, locality_features)
            if locality_key in occupied_by_locality:
                geom = make_valid(geom.difference(occupied_by_locality[locality_key]))
            geom = polygonal_geom(geom)
            if geom.is_empty or self.metric_area_km2(geom) < 0.00001:
                continue
            occupied_by_locality[locality_key] = (
                geom
                if locality_key not in occupied_by_locality
                else make_valid(unary_union([occupied_by_locality[locality_key], geom]))
            )
            normalized.append(self.geometry_feature(geom, props, feature_id=feature_id))
        normalized.sort(key=lambda item: item["properties"]["canonical_name"])
        return normalized

    def build_partido(self) -> dict[str, Any]:
        source = self.read_package_json("data/geo/01_partido_hurlingham.geojson")
        source_features = source.get("features") or []
        geoms = [make_valid(shape(feature["geometry"])) for feature in source_features]
        features = []
        if geoms:
            props = (source_features[0].get("properties") or {}) if source_features else {}
            features.append(
                self.geometry_feature(
                    unary_union(geoms),
                    {
                        "level": 1,
                        "level_name": "partido",
                        "canonical_name": "Partido de Hurlingham",
                        "partido_name": "Partido de Hurlingham",
                        "source_method": props.get("source_method") or "osm_relation",
                        "source_confidence": props.get("source_confidence") or "medium",
                        "needs_manual_review": False,
                        "source_feature_count": len(source_features),
                        "generated_at": self.generated_at,
                    },
                    feature_id="partido_hurlingham",
                )
            )
        payload = self.make_collection(
            features,
            name="01_partido_hurlingham",
            metadata={
                "source_name": "OSM / Overpass package",
                "source_zip": str(self.zip_path),
            },
        )
        self.write_json("data/geo/01_partido_hurlingham.geojson", payload)
        return payload

    def build_localities(self, partido: dict[str, Any]) -> dict[str, Any]:
        source = self.read_package_json("data/geo/02_localidades_hurlingham_approx.geojson")
        partido_geom = unary_union([shape(feature["geometry"]) for feature in partido.get("features") or []])
        grouped: dict[str, dict[str, Any]] = {}
        for feature in source.get("features") or []:
            props = feature.get("properties") or {}
            name = canonical_label(props)
            if not name:
                continue
            bucket = grouped.setdefault(name, {"props": props, "geoms": []})
            bucket["geoms"].append(make_valid(shape(feature["geometry"])))

        ordered_names = [name for name in EXPECTED_LOCALITIES if name in grouped]
        ordered_names.extend(sorted(name for name in grouped if name not in set(ordered_names)))
        occupied = None
        features = []
        for name in ordered_names:
            props = grouped[name]["props"]
            geom = make_valid(unary_union(grouped[name]["geoms"]))
            if not partido_geom.is_empty:
                geom = make_valid(geom.intersection(partido_geom))
            if occupied is not None:
                geom = make_valid(geom.difference(occupied))
            if geom.is_empty:
                continue
            occupied = geom if occupied is None else make_valid(unary_union([occupied, geom]))
            features.append(
                self.geometry_feature(
                    geom,
                    {
                        "level": 2,
                        "level_name": "localidad",
                        "canonical_name": name,
                        "locality_name": name,
                        "parent_partido": "Partido de Hurlingham",
                        "source_method": f"{props.get('source_method') or 'osm_relation_approx'}_dissolved_clipped",
                        "source_confidence": props.get("source_confidence") or "low_medium",
                        "source_warning": props.get("source_warning")
                        or "Localidades aproximadas usadas para jerarquia y fallback.",
                        "needs_manual_review": bool(props.get("needs_manual_review", True)),
                        "source_feature_count": len(grouped[name]["geoms"]),
                        "generated_at": self.generated_at,
                    },
                    feature_id=f"locality_{text_key(name)}",
                )
            )
        payload = self.make_collection(
            features,
            name="02_localidades_hurlingham",
            metadata={
                "source_name": "OSM / Overpass package",
                "source_zip": str(self.zip_path),
                "data_confidence": "low_medium",
            },
        )
        self.write_json("data/geo/02_localidades_hurlingham.geojson", payload)
        return payload

    def build_zones(self, localities: dict[str, Any]) -> dict[str, Any]:
        repo_zones = self.read_repo_json("data/geo/zones/zones_hurlingham.geojson")
        strict = self.source_features_by_key(
            self.read_package_json("data/geo/03_zonas_hurlingham_osm_strict.geojson")
        )
        candidate = self.source_features_by_key(
            self.read_package_json("data/geo/03_zonas_hurlingham_candidate_complete.geojson")
        )
        locality_features = localities.get("features") or []
        zone_entries: list[tuple[Any, dict[str, Any], str]] = []
        seen = set()
        for feature in repo_zones.get("features") or []:
            source_props = feature.get("properties") or {}
            source_name = canonical_label(source_props)
            name = canonical_zone_name(source_name)
            source_key = text_key(source_name)
            key = text_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            geom = make_valid(shape(feature["geometry"]))
            strict_feature = strict.get(source_key) or strict.get(key) or {}
            candidate_feature = candidate.get(source_key) or candidate.get(key) or {}
            strict_props = strict_feature.get("properties") or {}
            candidate_props = candidate_feature.get("properties") or {}
            relation_id = (
                self.relation_id_from_props(source_props)
                or self.relation_id_from_props(strict_props)
                or self.relation_id_from_props(candidate_props)
            )
            locality = self.assign_locality(geom, locality_features)
            if source_key in strict or key in strict:
                confidence = strict_props.get("source_confidence") or "medium_high"
                source_method = (
                    strict_props.get("source_method")
                    or source_props.get("zone_extraction_method")
                    or "osm_strict_polygon"
                )
                needs_review = source_method == "polygonized_relation_lines"
            elif source_key in candidate or key in candidate:
                confidence = candidate_props.get("source_confidence") or "medium"
                source_method = (
                    source_props.get("zone_extraction_method")
                    or candidate_props.get("source_method")
                    or "repo_polygon_candidate_supported"
                )
                needs_review = confidence in {"low", "very_low"} or source_method == "polygonized_relation_lines"
            else:
                confidence = "medium_low"
                source_method = source_props.get("zone_extraction_method") or "repo_polygon_only"
                needs_review = True
            zone_entries.append(
                (
                    geom,
                    {
                        "level": 3,
                        "level_name": "zona",
                        "canonical_name": name,
                        "zone_name": name,
                        "locality": locality,
                        "parent_locality": locality,
                        "parent_partido": "Partido de Hurlingham",
                        "relation_id": relation_id,
                        "osm_relation_id": relation_id,
                        "source_url": source_props.get("source_url")
                        or (f"https://www.openstreetmap.org/relation/{relation_id}" if relation_id else ""),
                        "source_name": source_props.get("source_name") or "OpenStreetMap Overpass API",
                        "source_method": source_method,
                        "source_confidence": confidence,
                        "candidate_source_confidence": candidate_props.get("source_confidence") or "",
                        "strict_source_available": source_key in strict or key in strict,
                        "needs_manual_review": needs_review,
                        "topology_normalized": True,
                        "topology_rule": "clipped_to_locality_and_difference_by_source_priority",
                        "generated_at": self.generated_at,
                    },
                    f"zone_{key}",
                )
            )
        features = self.normalize_zone_topology(zone_entries, locality_features)
        payload = self.make_collection(
            features,
            name="03_zonas_hurlingham_final",
            metadata={
                "source_name": "Merged canonical repo zones plus hierarchy package audit",
                "source_zip": str(self.zip_path),
                "zone_count": len(features),
                "data_confidence": "mixed",
                "notes": "Final audit layer; production ZONE_GEOJSON_PATH is intentionally unchanged.",
            },
        )
        self.write_json("data/geo/03_zonas_hurlingham_final.geojson", payload)
        return payload

    def build_microzones(self, zones: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        microzones = self.make_collection(
            [],
            name="03b_microzonas_hurlingham_final",
            metadata={
                "source_name": "No active microzones",
                "source_zip": str(self.zip_path),
                "data_confidence": "not_applicable",
                "notes": f"Barrio Ingles and Hurlingham Centro are unified as zone `{UNIFIED_ZONE_NAME}`.",
            },
        )
        evidence = self.make_collection(
            [],
            name="03b_microzonas_hurlingham_evidence_points",
            metadata={
                "source_name": "No active Barrio Ingles microzone evidence",
                "privacy": "No property-level evidence exported.",
                "data_confidence": "not_applicable",
                "notes": f"Former Barrio Ingles evidence is suppressed because it is now an alias of `{UNIFIED_ZONE_NAME}`.",
            },
        )
        self.write_json("data/geo/03b_microzonas_hurlingham_final.geojson", microzones)
        self.write_json("data/geo/03b_microzonas_hurlingham_evidence_points.geojson", evidence)
        return microzones, evidence

    def build_gaps(self, partido: dict[str, Any], zones: dict[str, Any]) -> dict[str, Any]:
        partido_geom = unary_union([shape(feature["geometry"]) for feature in partido.get("features") or []])
        zone_features = zones.get("features") or []
        zone_geoms = [shape(feature["geometry"]) for feature in zone_features]
        zone_union = unary_union(zone_geoms) if zone_geoms else None
        gap_geom = make_valid(partido_geom.difference(zone_union)) if zone_union else partido_geom
        candidate = self.read_package_json("data/geo/03_zonas_hurlingham_candidate_complete.geojson")
        candidate_items = [
            (canonical_label(feature.get("properties") or {}), shape(feature["geometry"]))
            for feature in candidate.get("features") or []
        ]
        parts = list(gap_geom.geoms) if hasattr(gap_geom, "geoms") else [gap_geom]
        features = []
        for index, geom in enumerate(parts, 1):
            if geom.is_empty:
                continue
            area_km2 = self.metric_area_km2(geom)
            if area_km2 < 0.00001:
                continue
            candidates = []
            for name, candidate_geom in candidate_items:
                inter_area = self.metric_area_km2(geom.intersection(candidate_geom))
                if inter_area > 0.0005:
                    candidates.append((name, inter_area))
            candidates.sort(key=lambda item: item[1], reverse=True)
            features.append(
                self.geometry_feature(
                    geom,
                    {
                        "level": 99,
                        "level_name": "gap_diagnostico",
                        "gap_id": f"GAP_{index:03d}",
                        "canonical_name": f"Gap {index:03d}",
                        "parent_partido": "Partido de Hurlingham",
                        "likely_missing_zone_candidates": "; ".join(
                            f"{name} ({area:.3f} km2)" for name, area in candidates[:8]
                        ),
                        "source_method": "derived_partido_minus_final_zone_polygons",
                        "source_confidence": "diagnostic",
                        "needs_manual_review": True,
                        "source_warning": "Sector no cubierto por zonas finales. Usar para priorizar revision geometrica.",
                        "generated_at": self.generated_at,
                    },
                    feature_id=f"gap_{index:03d}",
                )
            )
        payload = self.make_collection(
            features,
            name="04_gaps_zonas_hurlingham_final",
            metadata={
                "source_name": "Derived from partido minus final zone polygons",
                "data_confidence": "diagnostic",
                "gap_count": len(features),
            },
        )
        self.write_json("data/geo/04_gaps_zonas_hurlingham_final.geojson", payload)
        return payload

    def read_package_aliases(self) -> list[dict[str, str]]:
        with zipfile.ZipFile(self.zip_path) as archive:
            text = archive.read(
                PACKAGE_ROOT + "data/geo/zone_aliases_hurlingham.csv"
            ).decode("utf-8-sig")
        return list(csv.DictReader(text.splitlines()))

    def write_aliases(self) -> None:
        raw_rows = self.read_package_aliases()
        additions = [
            ("barrio ingles", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario equivalente a Hurlingham Centro."),
            ("ingles", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario equivalente a Hurlingham Centro."),
            ("hurlingham centro", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Nombre anterior de la zona unificada."),
            ("hurlingham centro barrio ingles", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Nombre compuesto de la zona unificada."),
            ("barrio ingles hurlingham", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario frecuente."),
            ("barrio inglés hurlingham", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario frecuente."),
            ("hurlingham barrio ingles", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario frecuente."),
            ("hurlingham barrio inglés", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario frecuente."),
            ("hurlingham centro ingles", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario frecuente."),
            ("hurlingham centro inglés", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Alias inmobiliario frecuente."),
            ("b ingles", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Abreviatura posible."),
            ("b inglés", UNIFIED_ZONE_NAME, UNIFIED_ZONE_NAME, "Hurlingham", "zona", "Abreviatura posible."),
            ("parque quirno", "Parque Quirno", "Parque Quirno", "Hurlingham", "zona", ""),
            ("cartero", "Cartero", "Cartero", "Hurlingham", "zona", ""),
            ("los patitos", "Los Patitos", "Los Patitos", "William C. Morris", "zona", ""),
        ]
        by_key = {}
        for row in raw_rows:
            key = text_key(row.get("source_text_normalized"))
            if (
                key in UNIFIED_ZONE_ALIAS_KEYS
                or text_key(row.get("canonical_name")) in UNIFIED_ZONE_ALIAS_KEYS
                or text_key(row.get("canonical_zone")) in UNIFIED_ZONE_ALIAS_KEYS
            ):
                row = {
                    **row,
                    "canonical_name": UNIFIED_ZONE_NAME,
                    "canonical_zone": UNIFIED_ZONE_NAME,
                    "canonical_locality": "Hurlingham",
                    "match_type": "zona",
                    "notes": "Alias equivalente a la zona unificada.",
                }
            if key and key not in by_key:
                by_key[key] = row
        for source, canonical_name, canonical_zone, locality, match_type, notes in additions:
            key = text_key(source)
            if key in by_key:
                continue
            row = {
                "source_text_normalized": source,
                "canonical_name": canonical_name,
                "canonical_zone": canonical_zone,
                "canonical_locality": locality,
                "match_type": match_type,
                "notes": notes,
            }
            by_key[key] = row
        rows = list(by_key.values())
        path = self.base_dir / "data/geo/zone_aliases_hurlingham.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "source_text_normalized",
            "canonical_name",
            "canonical_zone",
            "canonical_locality",
            "match_type",
            "notes",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def write_docs(
        self,
        zones: dict[str, Any],
        microzones: dict[str, Any],
        gaps: dict[str, Any],
    ) -> None:
        docs_dir = self.base_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        zone_count = len(zones.get("features") or [])
        gap_area = sum(
            (feature.get("properties") or {}).get("area_km2") or 0
            for feature in gaps.get("features") or []
        )
        usage = f"""# Geo Hierarchy Usage

Generated at: {self.generated_at}

## Layers

- `data/geo/01_partido_hurlingham.geojson`: level 1, Partido de Hurlingham.
- `data/geo/02_localidades_hurlingham.geojson`: level 2, Hurlingham, Villa Tesei, William C. Morris.
- `data/geo/03_zonas_hurlingham_final.geojson`: level 3, {zone_count} zonas with assigned locality.
- `data/geo/03b_microzonas_hurlingham_final.geojson`: compatibility layer, currently empty.
- `data/geo/04_gaps_zonas_hurlingham_final.geojson`: diagnostic gaps from partido minus final zones.
- `data/geo/zone_aliases_hurlingham.csv`: textual aliases for source labels.

## Inference Order For A Future Integration

1. Check that the point falls inside the partido.
2. Assign locality from `02_localidades_hurlingham.geojson`.
3. Assign zone from `03_zonas_hurlingham_final.geojson`.
4. Preserve the source text as `source_zone_raw` and compare aliases against inferred geography.

Barrio Ingles and Hurlingham Centro are a single zone: `{UNIFIED_ZONE_NAME}`.
"""
        pending = f"""# Pending Manual Review

Generated at: {self.generated_at}

## Hurlingham Centro / Barrio Ingles

- Current level: zona.
- Canonical name: {UNIFIED_ZONE_NAME}.
- Locality: Hurlingham.
- Action: review the renamed polygon if local evidence later suggests Barrio Ingles should split again.

## Zone Gaps

- Diagnostic gap features: {len(gaps.get('features') or [])}.
- Total gap area: {gap_area:.3f} km2.
- Action: inspect gaps in `/territorio/` and decide whether to refine OSM/manual polygons.

## Source Notes

- Barrio Ingles is no longer exported as a microzone.
- Federal remains a zone.
- Cartero and Los Patitos are retained from the existing canonical repo layer.
"""
        (docs_dir / "geo_hierarchy_usage.md").write_text(usage.rstrip() + "\n", encoding="utf-8")
        (docs_dir / "pending_manual_review.md").write_text(pending.rstrip() + "\n", encoding="utf-8")

    def build(self) -> dict[str, Any]:
        partido = self.build_partido()
        localities = self.build_localities(partido)
        zones = self.build_zones(localities)
        microzones, evidence = self.build_microzones(zones)
        gaps = self.build_gaps(partido, zones)
        self.write_aliases()
        self.write_docs(zones, microzones, gaps)
        return {
            "generated_at": self.generated_at,
            "outputs": {
                "partido": "data/geo/01_partido_hurlingham.geojson",
                "localidades": "data/geo/02_localidades_hurlingham.geojson",
                "zonas": "data/geo/03_zonas_hurlingham_final.geojson",
                "microzonas": "data/geo/03b_microzonas_hurlingham_final.geojson",
                "microzone_evidence": "data/geo/03b_microzonas_hurlingham_evidence_points.geojson",
                "gaps": "data/geo/04_gaps_zonas_hurlingham_final.geojson",
                "aliases": "data/geo/zone_aliases_hurlingham.csv",
            },
            "counts": {
                "partido": len(partido.get("features") or []),
                "localidades": len(localities.get("features") or []),
                "zonas": len(zones.get("features") or []),
                "microzonas": len(microzones.get("features") or []),
                "evidence_points": len(evidence.get("features") or []),
                "gaps": len(gaps.get("features") or []),
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Hurlingham hierarchy GeoJSON files.")
    parser.add_argument("--zip", default=str(DEFAULT_ZIP), help="Hierarchy package zip path.")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()

    zip_path = Path(args.zip)
    if not zip_path.exists():
        print(f"ERROR missing zip: {zip_path}", file=sys.stderr)
        return 2
    builder = GeoHierarchyBuilder(
        zip_path=zip_path,
        base_dir=Path(args.base_dir),
        generated_at=args.generated_at or utc_now(),
    )
    result = builder.build()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
