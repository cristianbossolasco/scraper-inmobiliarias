#!/usr/bin/env python3
"""Build phase-2 official location-intelligence layers for Hurlingham.

This script extends the base GeoJSON package with official or documented
fallback sources for census tracts, education, health, transport, flood risk,
RENABAP, and utilities. It is intentionally independent from Django.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import requests
import shapefile
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid


WGS84 = "EPSG:4326"
METRIC_CRS = "EPSG:32721"
ARBA_CRS = "EPSG:5347"
USER_AGENT = "radar-inmobiliario-location-intelligence-phase2/1.0"

PHASE2_SOURCES = {
    "census_2022_geojson_zip": {
        "url": "https://catalogo.datos.gba.gob.ar/dataset/33b080d2-e369-4076-acd4-511db0e9bffb/resource/151d80d2-87c1-4981-9bea-9aab38a82ec9/download/radios-censales-2022-geojson.zip",
        "raw_path": "data/raw/census/radios-censales-2022-geojson.zip",
        "source_name": "Datos Abiertos PBA / INDEC - Radios censales 2022",
        "source_url": "https://catalogo.datos.gba.gob.ar/dataset/radios-censales",
        "license": "Creative Commons Attribution 4.0",
    },
    "education_official_zip": {
        "url": "https://catalogo.datos.gba.gob.ar/dataset/4becb4b7-0a21-4fef-8f2c-30df7f345a01/resource/08a49256-620d-4d75-9a40-6f57df3be830/download/establecimientos-educativos-08062026.zip",
        "raw_path": "data/raw/education/establecimientos-educativos-08062026.zip",
        "source_name": "Datos Abiertos PBA - Establecimientos educativos",
        "source_url": "https://catalogo.datos.gba.gob.ar/dataset/establecimientos-educativos",
        "license": "Creative Commons Attribution 4.0",
        "temporal_coverage": "2026-06-10 resource / Final 2025 records",
    },
    "health_official_2025_zip": {
        "url": "https://catalogo.datos.gba.gob.ar/dataset/91743f68-bc82-4475-baca-7d5d6908eee8/resource/32ddfa86-3c6e-4754-a9db-dc4af8891b8f/download/establecimientos-salud-publicos-2025.zip",
        "raw_path": "data/raw/health/establecimientos-salud-publicos-2025.zip",
        "source_name": "Datos Abiertos PBA - Establecimientos de salud publicos",
        "source_url": "https://catalogo.datos.gba.gob.ar/dataset/establecimientos-salud",
        "license": "Creative Commons Attribution 4.0",
        "temporal_coverage": "2025",
    },
    "transport_routes_kml": {
        "url": "https://datos.transporte.gob.ar/dataset/d67bd5a0-bd6e-4b02-a7ba-a9dd329b0d5e/resource/434c8107-b3ae-46cc-919e-a98b603c1ced/download/lineas_jn_rmba_cnrt.kml",
        "raw_path": "data/raw/transport/lineas_jn_rmba_cnrt.kml",
        "source_name": "Datos Transporte - Recorridos de servicios de colectivos AMBA",
        "source_url": "https://datos.transporte.gob.ar/dataset/recorridos-de-servicios-de-colectivos-amba",
        "license": "Creative Commons Attribution 4.0",
        "temporal_coverage": "2023-04-12 dataset update",
    },
    "sube_points_geojson": {
        "url": "https://datos.transporte.gob.ar/dataset/88aee5cb-e0fc-4bde-96e9-6e3d213dc43a/resource/7c90f2ec-af99-4e07-99ae-1ab3e8e1a26d/download/sube_red_de_carga_activa_2019-10-01.geojson",
        "raw_path": "data/raw/transport/sube_red_de_carga_activa_2019-10-01.geojson",
        "source_name": "Datos Transporte / SUBE - Puntos de carga",
        "source_url": "https://datos.transporte.gob.ar/dataset/puntos-carga-sube",
        "license": "No especificada",
        "temporal_coverage": "2019-10-01",
    },
    "flood_reconquista_zip": {
        "url": "https://www.ada.gba.gov.ar/web_doc/gis/capas/peligrosidad_reconquista.zip",
        "raw_path": "data/raw/flood/peligrosidad_reconquista.zip",
        "source_name": "Autoridad del Agua PBA - Peligrosidad Cuenca Reconquista",
        "source_url": "https://ada.gba.gov.ar/cartas-de-riesgo-hidrico/",
        "license": "Public official geospatial data; cite ADA PBA.",
        "input_crs": ARBA_CRS,
    },
    "renabap_official_geojson": {
        "url": "https://datosabiertos.desarrollosocial.gob.ar/dataset/0d022767-9390-486a-bff4-ba53b85d730e/resource/97cc7d10-ad4c-46cb-9ee4-becb402adf9f/download/2022-07-13_info_publica_barrios_populares.geojson",
        "fallback_url": "https://raw.githubusercontent.com/OpenDataCordoba/barrios/main/Barrios%20Populares%20de%20Argentina.geojson",
        "raw_path": "data/raw/renabap/renabap_barrios_populares.geojson",
        "source_name": "RENABAP - Registro Nacional de Barrios Populares",
        "source_url": "https://datos.gob.ar/dataset/desarrollo-social-registro-nacional-barrios-populares",
        "license": "No especificada",
        "temporal_coverage": "Official portal updated 2023-09-20; fallback mirror may be older.",
    },
    "gas_segments_zip": {
        "url": "http://datos.minem.gob.ar/dataset/d850c7a4-e2cb-4a2e-9b15-666dd9e27398/resource/ae05ccf6-b486-44ea-89a1-1d315cdbd5bd/download/cantidad-de-usuarios-de-gas-de-red-por-segmento-de-calle.zip",
        "raw_path": "data/raw/utilities/cantidad-de-usuarios-de-gas-de-red-por-segmento-de-calle.zip",
        "source_name": "Secretaria de Energia - Usuarios de gas por segmento de calle",
        "source_url": "https://datos.gob.ar/dataset/energia-cantidad-usuarios-gas-natural-red-por-segmento-calle",
        "license": "No especificada",
        "input_crs": WGS84,
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


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


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text)


def zone_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_text(value)).strip("_")


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def safe_round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


LOSSY_TEXT_REPLACEMENTS = {
    "Direcci\ufffd": "Dirección",
    "N\ufffdmero": "Número",
    "Pa\ufffds": "País",
    "Ubicaci\ufffd": "Ubicación",
    "AG\ufffdERO NORA ALICIA": "AGÜERO NORA ALICIA",
    "CA\ufffdADA DE LA CRUZ": "CAÑADA DE LA CRUZ",
    "CRUCE\ufffdO EVA CRISTINA": "CRUCEÑO EVA CRISTINA",
    "Estaci\ufffdn de Tren": "Estación de Tren",
    "FFCC URQUIZA-Est. Ej\ufffdrcito de los Andes": "FFCC URQUIZA-Est. Ejército de los Andes",
    "FFCC URQUIZA-Est. Rub\ufffdn Dar\ufffdo": "FFCC URQUIZA-Est. Rubén Darío",
    "Locutorio Las Caba\ufffdas": "Locutorio Las Cabañas",
    "MAXI EL \ufffdANDU": "MAXI EL ÑANDU",
    "RGP S.A.(RECAUDACI\ufffdN,GESTI\ufffdN Y PAGOS)": "RGP S.A.(RECAUDACIÓN,GESTIÓN Y PAGOS)",
    "SOFSE-SAN MART\ufffdN": "SOFSE-SAN MARTÍN",
    "Seit\ufffd": "Seitú",
}


def clean_lossy_replacement_chars(value: str) -> str:
    for old, new in LOSSY_TEXT_REPLACEMENTS.items():
        value = value.replace(old, new)
    return value.replace("\ufffd", "")


def clean_property(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return clean_lossy_replacement_chars(value.strip()) or None
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    return value


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
    return {"type": "FeatureCollection", "name": name, "metadata": payload_metadata, "features": features}


def catalog_row(
    *,
    layer_name: str,
    path: Path,
    source_name: str,
    source_url: str,
    downloaded_at: str,
    license_text: str,
    geometry_type: str,
    feature_count: int | str,
    crs: str,
    spatial_precision: str,
    temporal_coverage: str,
    confidence: str,
    notes: str,
) -> dict[str, Any]:
    return {
        "layer_name": layer_name,
        "file_path": str(path),
        "source_name": source_name,
        "source_url": source_url,
        "downloaded_at": downloaded_at,
        "license": license_text,
        "geometry_type": geometry_type,
        "feature_count": feature_count,
        "crs": crs,
        "spatial_precision": spatial_precision,
        "temporal_coverage": temporal_coverage,
        "data_confidence": confidence,
        "notes": notes,
    }


def append_catalog(catalog_path: Path, rows: list[dict[str, Any]]) -> None:
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
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
    existing: list[dict[str, Any]] = []
    if catalog_path.exists():
        with catalog_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                clean = {key.lstrip("\ufeff").strip('"'): value for key, value in row.items()}
                existing.append(clean)
    replace_names = {row["layer_name"] for row in rows}
    existing = [row for row in existing if row.get("layer_name") not in replace_names]
    with catalog_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(rows)


def download_source(key: str, generated_at: str, unavailable: list[dict[str, str]]) -> Path | None:
    source = PHASE2_SOURCES[key]
    path = Path(source["raw_path"])
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    urls = [source["url"]]
    if source.get("fallback_url"):
        urls.append(source["fallback_url"])
    last_error = ""
    for index, url in enumerate(urls):
        try:
            with requests.get(url, timeout=180, stream=True, headers={"User-Agent": USER_AGENT}) as response:
                response.raise_for_status()
                with path.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            if index > 0:
                meta_path = path.with_suffix(path.suffix + ".metadata.json")
                write_json(
                    meta_path,
                    {
                        "generated_at": generated_at,
                        "primary_url_failed": source["url"],
                        "fallback_url_used": url,
                        "source_name": source["source_name"],
                        "source_url": source["source_url"],
                    },
                )
            return path
        except Exception as exc:  # noqa: BLE001 - try fallback or document failure.
            last_error = f"{type(exc).__name__}: {exc}"
            if path.exists():
                path.unlink()
            time.sleep(1)
    unavailable.append(
        {
            "key": key,
            "title": source["source_name"],
            "url": source["url"],
            "reason": last_error or "download_failed",
        }
    )
    return None


def write_unavailable(path: Path, title: str, generated_at: str, reason: str, url: str) -> None:
    write_text(
        path,
        f"""# {title}

Status: not available in phase 2 automated build.

Generated at: {generated_at}

Source URL: {url}

Reason: {reason}

Next step: retry the official source or locate a stable official replacement before using this layer in metrics.
""",
    )


def load_zone_context(path: Path) -> dict[str, Any]:
    zones = read_json(path)
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    to_arba = Transformer.from_crs(WGS84, ARBA_CRS, always_xy=True)
    features = zones.get("features") or []
    geoms_wgs = [shape(feature["geometry"]) for feature in features]
    geoms_metric = [transform(to_metric.transform, geom) for geom in geoms_wgs]
    geoms_arba = [transform(to_arba.transform, geom) for geom in geoms_wgs]
    names = [str((feature.get("properties") or {}).get("zone_name") or "") for feature in features]
    return {
        "payload": zones,
        "features": features,
        "names": names,
        "geoms_wgs": geoms_wgs,
        "geoms_metric": geoms_metric,
        "geoms_arba": geoms_arba,
        "union_wgs": unary_union(geoms_wgs),
        "union_metric": unary_union(geoms_metric),
        "union_arba": unary_union(geoms_arba),
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


def nearest_distance(zone_geom: Any, geoms: list[Any]) -> float | None:
    if not geoms:
        return None
    return min(float(zone_geom.distance(geom)) for geom in geoms)


def norm_positive(values: list[float | None]) -> list[float | None]:
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not valid:
        return [None for _ in values]
    min_value = min(valid)
    max_value = max(valid)
    if min_value == max_value:
        fill = 100.0 if max_value > 0 else 0.0
        return [fill if value is not None else None for value in values]
    return [
        None if value is None else max(0.0, min(100.0, (float(value) - min_value) / (max_value - min_value) * 100.0))
        for value in values
    ]


def norm_inverse_distance(values: list[float | None], cap_m: float) -> list[float | None]:
    output = []
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


def geojson_from_zip(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith((".geojson", ".json"))]
        if not names:
            raise ValueError(f"No GeoJSON found in {path}")
        return json.loads(archive.read(names[0]).decode("utf-8-sig"))


def standard_feature(
    feature: dict[str, Any],
    *,
    geom: Any,
    props: dict[str, Any],
    source: dict[str, Any],
    generated_at: str,
    zone_context: dict[str, Any],
    confidence: str,
    source_note: str = "",
) -> dict[str, Any]:
    zone_idx = assign_zone_index(geom, zone_context["geoms_wgs"])
    output_props = {}
    for key, value in props.items():
        clean_key = clean_property(key)
        if clean_key:
            output_props[str(clean_key)] = clean_property(value)
    output_props.update(
        {
            "assigned_zone_name": zone_context["names"][zone_idx] if zone_idx is not None else None,
            "source_name": source["source_name"],
            "source_url": source["source_url"],
            "license": source.get("license"),
            "generated_at": generated_at,
            "data_confidence": confidence,
        }
    )
    if source_note:
        output_props["source_note"] = source_note
    return {"type": "Feature", "properties": output_props, "geometry": geometry_mapping(geom)}


def build_official_points_layer(
    *,
    source_key: str,
    output_path: Path,
    output_name: str,
    zone_context: dict[str, Any],
    generated_at: str,
    catalog: list[dict[str, Any]],
    unavailable: list[dict[str, str]],
    municipality_field: str | None,
    fallback_text_match: bool,
    confidence: str,
) -> list[dict[str, Any]]:
    raw_path = download_source(source_key, generated_at, unavailable)
    if raw_path is None:
        return []
    source = PHASE2_SOURCES[source_key]
    payload = geojson_from_zip(raw_path) if raw_path.suffix.lower() == ".zip" else read_json(raw_path)
    features: list[dict[str, Any]] = []
    for feature in payload.get("features") or []:
        geom_payload = feature.get("geometry")
        if not geom_payload:
            continue
        geom = shape(geom_payload)
        props = feature.get("properties") or {}
        municipality_ok = False
        if municipality_field:
            municipality_ok = "hurlingham" in normalize_text(props.get(municipality_field))
        if fallback_text_match and not municipality_ok:
            text = " ".join(str(value) for value in props.values() if value is not None)
            municipality_ok = "hurlingham" in normalize_text(text)
        if not municipality_ok and not zone_context["union_wgs"].intersects(geom):
            continue
        if not geom.is_valid:
            geom = make_valid(geom)
        if not isinstance(geom, Point):
            geom = geom.representative_point()
        features.append(
            standard_feature(
                feature,
                geom=geom,
                props=props,
                source=source,
                generated_at=generated_at,
                zone_context=zone_context,
                confidence=confidence,
            )
        )
    metadata = {
        "source_name": source["source_name"],
        "source_url": source["source_url"],
        "raw_path": str(raw_path),
        "license": source.get("license"),
        "temporal_coverage": source.get("temporal_coverage"),
        "data_confidence": confidence,
        "filter_method": "municipality property and geometric intersection with Hurlingham zones",
    }
    write_json(output_path, make_feature_collection(features, name=output_name, generated_at=generated_at, metadata=metadata))
    catalog.append(
        catalog_row(
            layer_name=output_name,
            path=output_path,
            source_name=source["source_name"],
            source_url=source["source_url"],
            downloaded_at=generated_at,
            license_text=source.get("license", ""),
            geometry_type="Point",
            feature_count=len(features),
            crs=WGS84,
            spatial_precision="official_point",
            temporal_coverage=source.get("temporal_coverage", ""),
            confidence=confidence,
            notes="Official points filtered to Hurlingham.",
        )
    )
    return features


def build_education_zones(points: list[dict[str, Any]], zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    point_geoms_m = [transform(Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True).transform, shape(f["geometry"])) for f in points]
    for zone_name, zone_m in zip(zone_context["names"], zone_context["geoms_metric"]):
        area_km2 = float(zone_m.area / 1_000_000)
        assigned = [f for f in points if (f.get("properties") or {}).get("assigned_zone_name") == zone_name]
        public_count = sum(1 for f in assigned if "estatal" in normalize_text((f.get("properties") or {}).get("sector")))
        private_count = sum(1 for f in assigned if "priv" in normalize_text((f.get("properties") or {}).get("sector")))
        kindergarten = [g for f, g in zip(points, point_geoms_m) if "inicial" in normalize_text((f.get("properties") or {}).get("nivel"))]
        primary = [g for f, g in zip(points, point_geoms_m) if "prim" in normalize_text((f.get("properties") or {}).get("nivel"))]
        secondary = [g for f, g in zip(points, point_geoms_m) if "secund" in normalize_text((f.get("properties") or {}).get("nivel"))]
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": safe_round(area_km2, 4),
                "schools_count": len(assigned),
                "schools_per_km2": safe_round(len(assigned) / area_km2, 4) if area_km2 else None,
                "public_schools_count": public_count,
                "private_schools_count": private_count,
                "nearest_school_m": safe_round(nearest_distance(zone_m, point_geoms_m), 2),
                "nearest_kindergarten_m": safe_round(nearest_distance(zone_m, kindergarten), 2),
                "nearest_primary_school_m": safe_round(nearest_distance(zone_m, primary), 2),
                "nearest_secondary_school_m": safe_round(nearest_distance(zone_m, secondary), 2),
            }
        )
    density_scores = norm_positive([row["schools_per_km2"] for row in rows])
    distance_scores = norm_inverse_distance([row["nearest_school_m"] for row in rows], 2500)
    for idx, row in enumerate(rows):
        row["education_access_score"] = weighted_score([(density_scores[idx], 0.45), (distance_scores[idx], 0.55)])
    write_zone_layer(
        rows,
        zone_context,
        generated_at,
        Path("data/geo/education/education_zones_hurlingham.geojson"),
        "education_zones_hurlingham",
        "Datos Abiertos PBA - Establecimientos educativos",
        "https://catalogo.datos.gba.gob.ar/dataset/establecimientos-educativos",
        "Creative Commons Attribution 4.0",
        "official_zone_aggregation",
        "2026 / Final 2025 records",
        "high",
        catalog,
    )
    return rows


def build_health_zones(points: list[dict[str, Any]], zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    point_geoms_m = [transform(to_metric.transform, shape(f["geometry"])) for f in points]
    hospitals = [
        geom
        for feature, geom in zip(points, point_geoms_m)
        if "internacion" in normalize_text((feature.get("properties") or {}).get("cat"))
        or "hospital" in normalize_text((feature.get("properties") or {}).get("nor"))
    ]
    rows = []
    for zone_name, zone_m in zip(zone_context["names"], zone_context["geoms_metric"]):
        area_km2 = float(zone_m.area / 1_000_000)
        assigned = [f for f in points if (f.get("properties") or {}).get("assigned_zone_name") == zone_name]
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": safe_round(area_km2, 4),
                "health_points_count": len(assigned),
                "health_points_per_km2": safe_round(len(assigned) / area_km2, 4) if area_km2 else None,
                "hospitals_count": sum(1 for f in assigned if "hospital" in normalize_text((f.get("properties") or {}).get("nor"))),
                "nearest_health_center_m": safe_round(nearest_distance(zone_m, point_geoms_m), 2),
                "nearest_hospital_m": safe_round(nearest_distance(zone_m, hospitals), 2),
            }
        )
    density_scores = norm_positive([row["health_points_per_km2"] for row in rows])
    distance_scores = norm_inverse_distance([row["nearest_health_center_m"] for row in rows], 3000)
    hospital_scores = norm_inverse_distance([row["nearest_hospital_m"] for row in rows], 5000)
    for idx, row in enumerate(rows):
        row["health_access_score"] = weighted_score(
            [(density_scores[idx], 0.30), (distance_scores[idx], 0.45), (hospital_scores[idx], 0.25)]
        )
    write_zone_layer(
        rows,
        zone_context,
        generated_at,
        Path("data/geo/health/health_zones_hurlingham.geojson"),
        "health_zones_hurlingham",
        "Datos Abiertos PBA - Establecimientos de salud publicos",
        "https://catalogo.datos.gba.gob.ar/dataset/establecimientos-salud",
        "Creative Commons Attribution 4.0",
        "official_zone_aggregation",
        "2025",
        "high",
        catalog,
    )
    return rows


def write_zone_layer(
    rows: list[dict[str, Any]],
    zone_context: dict[str, Any],
    generated_at: str,
    path: Path,
    name: str,
    source_name: str,
    source_url: str,
    license_text: str,
    spatial_precision: str,
    temporal_coverage: str,
    confidence: str,
    catalog: list[dict[str, Any]],
) -> None:
    features = []
    for source_feature, row in zip(zone_context["features"], rows):
        props = dict(row)
        props["source_name"] = source_name
        props["source_url"] = source_url
        props["generated_at"] = generated_at
        props["data_confidence"] = confidence
        features.append({"type": "Feature", "properties": props, "geometry": source_feature["geometry"]})
    payload = make_feature_collection(
        features,
        name=name,
        generated_at=generated_at,
        metadata={
            "source_name": source_name,
            "source_url": source_url,
            "license": license_text,
            "metric_crs_used_for_calculation": METRIC_CRS,
            "data_confidence": confidence,
        },
    )
    write_json(path, payload)
    catalog.append(
        catalog_row(
            layer_name=name,
            path=path,
            source_name=source_name,
            source_url=source_url,
            downloaded_at=generated_at,
            license_text=license_text,
            geometry_type="Polygon",
            feature_count=len(rows),
            crs=WGS84,
            spatial_precision=spatial_precision,
            temporal_coverage=temporal_coverage,
            confidence=confidence,
            notes="Zone-level aggregation for official phase-2 source.",
        )
    )


def build_census_layers(zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]], unavailable: list[dict[str, str]]) -> list[dict[str, Any]]:
    raw_path = download_source("census_2022_geojson_zip", generated_at, unavailable)
    if raw_path is None:
        return []
    source = PHASE2_SOURCES["census_2022_geojson_zip"]
    payload = geojson_from_zip(raw_path)
    tracts = []
    overlap_by_zone: defaultdict[int, float] = defaultdict(float)
    count_by_zone: Counter[int] = Counter()
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        if normalize_text(props.get("NOMDEPTO")) != "hurlingham" and str(props.get("DEPTO")) not in {"408", "135"}:
            geom_test = shape(feature["geometry"])
            if not zone_context["union_wgs"].intersects(geom_test):
                continue
        geom = shape(feature["geometry"])
        if not geom.is_valid:
            geom = make_valid(geom)
        if not zone_context["union_wgs"].intersects(geom):
            continue
        zone_idx = assign_zone_index(geom, zone_context["geoms_wgs"])
        geom_m = transform(to_metric.transform, geom)
        overlap_area = float(geom_m.intersection(zone_context["union_metric"]).area)
        if zone_idx is not None:
            count_by_zone[zone_idx] += 1
            overlap_by_zone[zone_idx] += overlap_area
        out_props = {key.lower(): clean_property(value) for key, value in props.items()}
        out_props.update(
            {
                "assigned_zone_name": zone_context["names"][zone_idx] if zone_idx is not None else None,
                "hurlingham_overlap_m2": safe_round(overlap_area, 2),
                "population_total": None,
                "households_total": None,
                "source_note": "PBA 2022 radio resource provides geometry and radio codes; no population/housing counts were present in the downloaded resource.",
                "source_name": source["source_name"],
                "source_url": source["source_url"],
                "generated_at": generated_at,
                "data_confidence": "medium",
            }
        )
        tracts.append({"type": "Feature", "properties": out_props, "geometry": geometry_mapping(geom)})
    out = Path("data/geo/census/census_tracts_2022_hurlingham.geojson")
    write_json(out, make_feature_collection(tracts, name="census_tracts_2022_hurlingham", generated_at=generated_at, metadata={"source_name": source["source_name"], "source_url": source["source_url"], "license": source["license"], "data_confidence": "medium"}))
    catalog.append(catalog_row(layer_name="census_tracts_2022_hurlingham", path=out, source_name=source["source_name"], source_url=source["source_url"], downloaded_at=generated_at, license_text=source["license"], geometry_type="Polygon,MultiPolygon", feature_count=len(tracts), crs=WGS84, spatial_precision="census_radio", temporal_coverage="2022", confidence="medium", notes="Geometry-only radio census layer; population counts unavailable in source resource."))
    rows = []
    for idx, (zone_name, zone_m) in enumerate(zip(zone_context["names"], zone_context["geoms_metric"])):
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": safe_round(float(zone_m.area / 1_000_000), 4),
                "census_tract_count": count_by_zone[idx],
                "census_overlap_area_m2": safe_round(overlap_by_zone[idx], 2),
                "population_total": None,
                "population_density_km2": None,
                "households_total": None,
                "household_density_km2": None,
                "avg_household_size": None,
                "housing_units_total": None,
                "census_service_deficit_proxy": None,
                "socioeconomic_proxy_score": None,
                "census_data_note": "Population/housing attributes are null because the official PBA 2022 downloadable radio resource inspected here contains geometry and radio identifiers only.",
            }
        )
    write_zone_layer(rows, zone_context, generated_at, Path("data/geo/census/census_zones_hurlingham.geojson"), "census_zones_hurlingham", source["source_name"], source["source_url"], source["license"], "area_weighted_zone_aggregation", "2022", "medium", catalog)
    return rows


def build_renabap_layers(zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]], unavailable: list[dict[str, str]]) -> list[dict[str, Any]]:
    raw_path = download_source("renabap_official_geojson", generated_at, unavailable)
    if raw_path is None:
        write_unavailable(Path("data/geo/renabap/source_unavailable.md"), "RENABAP / barrios populares", generated_at, "Official host and fallback unavailable.", PHASE2_SOURCES["renabap_official_geojson"]["source_url"])
        return []
    source = PHASE2_SOURCES["renabap_official_geojson"]
    payload = read_json(raw_path)
    source_note = "Primary official download was unavailable; public GitHub mirror was used." if raw_path.with_suffix(raw_path.suffix + ".metadata.json").exists() else ""
    features = []
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    renabap_geoms_m = []
    renabap_features_for_metrics = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        text = " ".join(str(value) for value in props.values() if value is not None)
        geom = shape(feature["geometry"])
        if not geom.is_valid:
            geom = make_valid(geom)
        if "hurlingham" not in normalize_text(text) and not zone_context["union_wgs"].intersects(geom):
            continue
        if not zone_context["union_wgs"].buffer(0.01).intersects(geom):
            continue
        out_feature = standard_feature(feature, geom=geom, props=props, source=source, generated_at=generated_at, zone_context=zone_context, confidence="medium_low", source_note=source_note)
        features.append(out_feature)
        renabap_features_for_metrics.append(out_feature)
        renabap_geoms_m.append(transform(to_metric.transform, geom))
    out = Path("data/geo/renabap/renabap_hurlingham.geojson")
    write_json(out, make_feature_collection(features, name="renabap_hurlingham", generated_at=generated_at, metadata={"source_name": source["source_name"], "source_url": source["source_url"], "mirror_note": source_note, "data_confidence": "medium_low"}))
    catalog.append(catalog_row(layer_name="renabap_hurlingham", path=out, source_name=source["source_name"], source_url=source["source_url"], downloaded_at=generated_at, license_text=source["license"], geometry_type="Polygon,MultiPolygon", feature_count=len(features), crs=WGS84, spatial_precision="barrio_popular_polygon", temporal_coverage=source.get("temporal_coverage", ""), confidence="medium_low", notes=source_note))
    rows = []
    for zone_name, zone_m in zip(zone_context["names"], zone_context["geoms_metric"]):
        assigned = [f for f in renabap_features_for_metrics if (f.get("properties") or {}).get("assigned_zone_name") == zone_name]
        overlap_area = 0.0
        families = 0.0
        for feature, geom_m in zip(renabap_features_for_metrics, renabap_geoms_m):
            inter_area = float(zone_m.intersection(geom_m).area) if zone_m.intersects(geom_m) else 0.0
            if inter_area <= 0:
                continue
            overlap_area += inter_area
            props = feature.get("properties") or {}
            source_area = safe_float(props.get("superficie_m2")) or float(geom_m.area) or 1.0
            families += (safe_float(props.get("cantidad_familias_aproximada")) or 0.0) * min(1.0, inter_area / source_area)
        nearest = nearest_distance(zone_m, renabap_geoms_m)
        penalty = weighted_score(
            [
                (min(100.0, overlap_area / max(zone_m.area, 1.0) * 1000.0), 0.60),
                (None if nearest is None else max(0.0, 100.0 * (1.0 - min(nearest, 1500.0) / 1500.0)), 0.40),
            ]
        )
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": safe_round(float(zone_m.area / 1_000_000), 4),
                "inside_renabap": overlap_area > 0,
                "renabap_count": len(assigned),
                "renabap_area_overlap_m2": safe_round(overlap_area, 2),
                "renabap_area_1000m": None,
                "nearest_renabap_m": safe_round(nearest, 2),
                "renabap_families_nearby": safe_round(families, 2),
                "urban_informality_score": penalty,
                "source_note": source_note,
            }
        )
    write_zone_layer(rows, zone_context, generated_at, Path("data/geo/renabap/renabap_zones_hurlingham.geojson"), "renabap_zones_hurlingham", source["source_name"], source["source_url"], source["license"], "zone_intersection_and_distance", source.get("temporal_coverage", ""), "medium_low", catalog)
    return rows


def parse_kml_routes(path: Path, zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = PHASE2_SOURCES["transport_routes_kml"]
    root = ET.fromstring(path.read_bytes())
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    features = []
    for placemark in root.findall(".//k:Placemark", ns):
        coords_text = placemark.findtext(".//k:LineString/k:coordinates", default="", namespaces=ns).strip()
        if not coords_text:
            continue
        coords = []
        for raw in coords_text.split():
            parts = raw.split(",")
            if len(parts) >= 2:
                coords.append((float(parts[0]), float(parts[1])))
        if len(coords) < 2:
            continue
        geom = LineString(coords)
        if not zone_context["union_wgs"].intersects(geom):
            continue
        props = {}
        for data in placemark.findall(".//k:SimpleData", ns):
            props[data.attrib.get("name", "field")] = clean_property(data.text)
        zone_idx = assign_zone_index(geom, zone_context["geoms_wgs"])
        props.update(
            {
                "assigned_zone_name": zone_context["names"][zone_idx] if zone_idx is not None else None,
                "source_name": source["source_name"],
                "source_url": source["source_url"],
                "license": source["license"],
                "generated_at": generated_at,
                "data_confidence": "medium",
            }
        )
        features.append({"type": "Feature", "properties": props, "geometry": geometry_mapping(geom)})
    out = Path("data/geo/transport/transport_routes_hurlingham.geojson")
    write_json(out, make_feature_collection(features, name="transport_routes_hurlingham", generated_at=generated_at, metadata={"source_name": source["source_name"], "source_url": source["source_url"], "license": source["license"], "data_confidence": "medium", "source_format": "KML"}))
    catalog.append(catalog_row(layer_name="transport_routes_hurlingham", path=out, source_name=source["source_name"], source_url=source["source_url"], downloaded_at=generated_at, license_text=source["license"], geometry_type="LineString", feature_count=len(features), crs=WGS84, spatial_precision="official_route_line", temporal_coverage=source.get("temporal_coverage", ""), confidence="medium", notes="Official AMBA bus routes intersecting Hurlingham zones."))
    return features


def build_transport_layers(zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]], unavailable: list[dict[str, str]]) -> list[dict[str, Any]]:
    routes_path = download_source("transport_routes_kml", generated_at, unavailable)
    sube_path = download_source("sube_points_geojson", generated_at, unavailable)
    routes = parse_kml_routes(routes_path, zone_context, generated_at, catalog) if routes_path else []
    sube_features = []
    source = PHASE2_SOURCES["sube_points_geojson"]
    if sube_path:
        payload = read_json(sube_path)
        for feature in payload.get("features") or []:
            props = feature.get("properties") or {}
            lon = safe_float(props.get("Longitud"))
            lat = safe_float(props.get("Latitud"))
            geom = Point(lon, lat) if lon is not None and lat is not None else shape(feature["geometry"])
            text = " ".join(str(value) for value in props.values() if value is not None)
            municipal_match = any(token in normalize_text(text) for token in ["hurlingham", "villa tesei", "william morris"])
            if not municipal_match and not zone_context["union_wgs"].intersects(geom):
                continue
            sube_features.append(standard_feature(feature, geom=geom, props=props, source=source, generated_at=generated_at, zone_context=zone_context, confidence="medium"))
        out = Path("data/geo/transport/sube_points_hurlingham.geojson")
        write_json(out, make_feature_collection(sube_features, name="sube_points_hurlingham", generated_at=generated_at, metadata={"source_name": source["source_name"], "source_url": source["source_url"], "license": source["license"], "temporal_coverage": source["temporal_coverage"], "data_confidence": "medium"}))
        catalog.append(catalog_row(layer_name="sube_points_hurlingham", path=out, source_name=source["source_name"], source_url=source["source_url"], downloaded_at=generated_at, license_text=source["license"], geometry_type="Point", feature_count=len(sube_features), crs=WGS84, spatial_precision="official_point", temporal_coverage=source["temporal_coverage"], confidence="medium", notes="SUBE dataset is old but official; use as access proxy, not current definitive coverage."))
    to_metric = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
    route_geoms_m = [transform(to_metric.transform, shape(feature["geometry"])) for feature in routes]
    sube_geoms_m = [transform(to_metric.transform, shape(feature["geometry"])) for feature in sube_features]
    rows = []
    for zone_name, zone_m in zip(zone_context["names"], zone_context["geoms_metric"]):
        route_hits = [feature for feature, geom in zip(routes, route_geoms_m) if zone_m.intersects(geom)]
        line_names = sorted({str((feature.get("properties") or {}).get("Linea") or "") for feature in route_hits if (feature.get("properties") or {}).get("Linea")})
        sube_assigned = [feature for feature in sube_features if (feature.get("properties") or {}).get("assigned_zone_name") == zone_name]
        nearest_route = nearest_distance(zone_m, route_geoms_m)
        nearest_sube = nearest_distance(zone_m, sube_geoms_m)
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": safe_round(float(zone_m.area / 1_000_000), 4),
                "official_bus_route_count": len(route_hits),
                "official_bus_lines_count": len(line_names),
                "official_bus_lines": ",".join(line_names[:30]),
                "sube_points_count": len(sube_assigned),
                "nearest_official_bus_route_m": safe_round(nearest_route, 2),
                "nearest_sube_point_m": safe_round(nearest_sube, 2),
            }
        )
    route_scores = norm_positive([row["official_bus_lines_count"] for row in rows])
    sube_scores = norm_positive([row["sube_points_count"] for row in rows])
    route_distance_scores = norm_inverse_distance([row["nearest_official_bus_route_m"] for row in rows], 1200)
    sube_distance_scores = norm_inverse_distance([row["nearest_sube_point_m"] for row in rows], 2500)
    for idx, row in enumerate(rows):
        row["transport_access_score"] = weighted_score(
            [(route_scores[idx], 0.40), (route_distance_scores[idx], 0.25), (sube_scores[idx], 0.20), (sube_distance_scores[idx], 0.15)]
        )
    write_zone_layer(rows, zone_context, generated_at, Path("data/geo/transport/transport_zones_hurlingham.geojson"), "transport_zones_hurlingham", "Datos Transporte / SUBE", "https://datos.transporte.gob.ar/", "Mixed: CC BY 4.0 for routes; SUBE license unspecified", "official_zone_aggregation", "Routes 2023 / SUBE 2019", "medium", catalog)
    return rows


FLOOD_LEVELS = {
    "muy baja a nula": 5,
    "baja": 25,
    "media": 50,
    "alta": 75,
    "muy alta": 100,
}


def flood_level_value(value: Any) -> int:
    text = normalize_text(value)
    for key, score in FLOOD_LEVELS.items():
        if key in text:
            return score
    return 0


def build_flood_layers(zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]], unavailable: list[dict[str, str]]) -> list[dict[str, Any]]:
    raw_path = download_source("flood_reconquista_zip", generated_at, unavailable)
    if raw_path is None:
        return []
    source = PHASE2_SOURCES["flood_reconquista_zip"]
    to_wgs = Transformer.from_crs(source.get("input_crs", ARBA_CRS), WGS84, always_xy=True)
    features = []
    zone_bboxes = [zone.bounds for zone in zone_context["geoms_arba"]]
    union_bbox = zone_context["union_arba"].bounds
    zone_weighted = [0.0 for _ in zone_context["names"]]
    zone_area = [0.0 for _ in zone_context["names"]]
    zone_high_area = [0.0 for _ in zone_context["names"]]
    zone_max_score = [0 for _ in zone_context["names"]]
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(raw_path) as archive:
            archive.extractall(tmpdir)
        shp_path = next(Path(tmpdir).glob("*.shp"))
        reader = shapefile.Reader(str(shp_path), encoding="utf-8")
        try:
            field_names = [field[0] for field in reader.fields[1:]]
            for record in reader.iterShapeRecords():
                record_bbox = record.shape.bbox
                if not bbox_intersects(record_bbox, union_bbox):
                    continue
                geom_native = shape(record.shape.__geo_interface__)
                if not geom_native.is_valid:
                    geom_native = make_valid(geom_native)
                if not zone_context["union_arba"].intersects(geom_native):
                    continue
                props = {key: clean_property(value) for key, value in zip(field_names, list(record.record))}
                zone_idx = assign_zone_index(geom_native, zone_context["geoms_arba"])
                risk_score = flood_level_value(props.get("peligrosid"))
                props.update(
                    {
                        "assigned_zone_name": zone_context["names"][zone_idx] if zone_idx is not None else None,
                        "risk_score": risk_score,
                        "source_name": source["source_name"],
                        "source_url": source["source_url"],
                        "generated_at": generated_at,
                        "data_confidence": "high",
                    }
                )
                for idx, zone_arba in enumerate(zone_context["geoms_arba"]):
                    if not bbox_intersects(record_bbox, zone_bboxes[idx]):
                        continue
                    if not zone_arba.intersects(geom_native):
                        continue
                    inter_area = float(zone_arba.intersection(geom_native).area)
                    if inter_area <= 0:
                        continue
                    zone_area[idx] += inter_area
                    zone_weighted[idx] += inter_area * risk_score
                    zone_max_score[idx] = max(zone_max_score[idx], risk_score)
                    if risk_score >= 50:
                        zone_high_area[idx] += inter_area
                geom_wgs = transform(to_wgs.transform, geom_native)
                features.append({"type": "Feature", "properties": props, "geometry": geometry_mapping(geom_wgs)})
        finally:
            reader.close()
    out = Path("data/geo/flood/flood_risk_hurlingham.geojson")
    write_json(out, make_feature_collection(features, name="flood_risk_hurlingham", generated_at=generated_at, metadata={"source_name": source["source_name"], "source_url": source["source_url"], "license": source["license"], "input_crs": source["input_crs"], "data_confidence": "high"}), compact=True)
    catalog.append(catalog_row(layer_name="flood_risk_hurlingham", path=out, source_name=source["source_name"], source_url=source["source_url"], downloaded_at=generated_at, license_text=source["license"], geometry_type="Polygon", feature_count=len(features), crs=WGS84, spatial_precision="official_flood_risk_polygon", temporal_coverage="", confidence="high", notes="Official ADA Reconquista flood-risk polygons clipped/intersected to Hurlingham zones."))
    rows = []
    for idx, (zone_name, zone_arba, zone_m) in enumerate(zip(zone_context["names"], zone_context["geoms_arba"], zone_context["geoms_metric"])):
        weighted = zone_weighted[idx]
        area = zone_area[idx]
        high_area = zone_high_area[idx]
        max_score = zone_max_score[idx]
        score = weighted / area if area else None
        rows.append(
            {
                "zone_name": zone_name,
                "area_km2": safe_round(float(zone_m.area / 1_000_000), 4),
                "flood_risk_overlap_m2": safe_round(area, 2),
                "flood_risk_overlap_pct": safe_round(area / zone_arba.area * 100.0, 4) if zone_arba.area else None,
                "flood_risk_high_or_medium_m2": safe_round(high_area, 2),
                "in_flood_risk_zone": bool(area > 0),
                "flood_risk_level": classify_level(score),
                "flood_penalty_score": safe_round(score, 2),
                "flood_risk_max_score": max_score or None,
            }
        )
    write_zone_layer(rows, zone_context, generated_at, Path("data/geo/flood/flood_zones_hurlingham.geojson"), "flood_zones_hurlingham", source["source_name"], source["source_url"], source["license"], "official_zone_intersection", "", "high", catalog)
    return rows


def bbox_intersects(a: Iterable[float], b: Iterable[float]) -> bool:
    a_minx, a_miny, a_maxx, a_maxy = a
    b_minx, b_miny, b_maxx, b_maxy = b
    return not (a_maxx < b_minx or a_minx > b_maxx or a_maxy < b_miny or a_miny > b_maxy)


def build_utilities_layers(zone_context: dict[str, Any], generated_at: str, catalog: list[dict[str, Any]], unavailable: list[dict[str, str]]) -> list[dict[str, Any]]:
    raw_path = download_source("gas_segments_zip", generated_at, unavailable)
    source = PHASE2_SOURCES["gas_segments_zip"]
    if raw_path is None:
        write_unavailable(Path("data/geo/utilities/gas_segments_source_unavailable.md"), "Gas segments", generated_at, unavailable[-1]["reason"] if unavailable else "download failed", source["source_url"])
        rows = [
            {
                "zone_name": zone_name,
                "area_km2": safe_round(float(zone_m.area / 1_000_000), 4),
                "gas_segment_count": None,
                "gas_users_total": None,
                "gas_network_proxy": None,
                "utility_quality_score": None,
                "source_note": "Gas official resource was listed but unavailable at build time.",
            }
            for zone_name, zone_m in zip(zone_context["names"], zone_context["geoms_metric"])
        ]
        write_zone_layer(rows, zone_context, generated_at, Path("data/geo/utilities/utilities_zones_hurlingham.geojson"), "utilities_zones_hurlingham", source["source_name"], source["source_url"], source["license"], "source_unavailable", "", "none", catalog)
        return rows
    write_unavailable(Path("data/geo/utilities/electric_outages_source_unavailable.md"), "Electric outages historical layer", generated_at, "ENRE page exposes current/snapshot service pages, but no stable historical CSV/GeoJSON endpoint was confirmed in this phase.", "https://www.argentina.gob.ar/enre/estado-de-la-red-electrica-en-el-area-metropolitana-de-buenos-aires")
    return []


def props_by_zone(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = read_json(path)
    output = {}
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        key = zone_key(props.get("zone_name") or props.get("assigned_zone_name") or props.get("label"))
        if key:
            output[key] = props
    return output


def update_integrated_layer(generated_at: str, catalog: list[dict[str, Any]]) -> None:
    path = Path("data/geo/integrated_location_value_zones_hurlingham.geojson")
    payload = read_json(path)
    education = props_by_zone(Path("data/geo/education/education_zones_hurlingham.geojson"))
    health = props_by_zone(Path("data/geo/health/health_zones_hurlingham.geojson"))
    transport = props_by_zone(Path("data/geo/transport/transport_zones_hurlingham.geojson"))
    census = props_by_zone(Path("data/geo/census/census_zones_hurlingham.geojson"))
    renabap = props_by_zone(Path("data/geo/renabap/renabap_zones_hurlingham.geojson"))
    flood = props_by_zone(Path("data/geo/flood/flood_zones_hurlingham.geojson"))
    utilities = props_by_zone(Path("data/geo/utilities/utilities_zones_hurlingham.geojson"))
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        key = zone_key(props.get("zone_name"))
        for source_props, mapping_keys in [
            (education.get(key, {}), ["education_access_score", "schools_count", "nearest_school_m", "nearest_kindergarten_m", "nearest_primary_school_m", "nearest_secondary_school_m"]),
            (health.get(key, {}), ["health_access_score", "nearest_health_center_m", "nearest_hospital_m", "health_points_count"]),
            (transport.get(key, {}), ["transport_access_score", "official_bus_lines_count", "official_bus_route_count", "sube_points_count", "nearest_official_bus_route_m", "nearest_sube_point_m"]),
            (census.get(key, {}), ["population_total", "population_density_km2", "households_total", "household_density_km2", "avg_household_size", "housing_units_total", "census_service_deficit_proxy", "socioeconomic_proxy_score", "census_tract_count"]),
            (renabap.get(key, {}), ["urban_informality_score", "nearest_renabap_m", "renabap_area_overlap_m2", "renabap_families_nearby", "inside_renabap"]),
            (flood.get(key, {}), ["flood_penalty_score", "in_flood_risk_zone", "flood_risk_level", "flood_risk_overlap_pct", "flood_risk_max_score"]),
            (utilities.get(key, {}), ["utility_quality_score", "gas_network_proxy", "gas_segment_count", "gas_users_total"]),
        ]:
            for field in mapping_keys:
                if field in source_props:
                    props[field] = source_props.get(field)
        overall, used = phase2_location_score(props)
        props["overall_location_value_score"] = overall
        props["location_value_level"] = classify_level(overall)
        props["sources_count"] = used
        props["generated_at"] = generated_at
        props["score_methodology"] = "Phase 2 score uses official education, health, transport, flood, census/RENABAP/utilities where available; municipal crime remains context only."
    metadata = payload.setdefault("metadata", {})
    metadata["generated_at"] = generated_at
    metadata["phase2_updated_at"] = generated_at
    metadata["score_weights"] = {
        "security_infrastructure_score": 0.13,
        "transport_access_score": 0.18,
        "education_access_score": 0.14,
        "health_access_score": 0.10,
        "amenity_density_score": 0.12,
        "green_access_score": 0.08,
        "inverse_flood_penalty_score": 0.12,
        "inverse_environmental_penalty_score": 0.05,
        "inverse_urban_informality_score": 0.08,
    }
    write_json(path, payload)
    catalog.append(catalog_row(layer_name="integrated_location_value_zones", path=path, source_name="Local generated integration", source_url="", downloaded_at=generated_at, license_text="Mixed sources; see docs/sources.md", geometry_type="Polygon", feature_count=len(payload.get("features") or []), crs=WGS84, spatial_precision="zone", temporal_coverage="", confidence="medium_high", notes="Integrated layer updated with phase-2 official and fallback sources."))


def phase2_location_score(props: dict[str, Any]) -> tuple[float | None, int]:
    components = [
        (props.get("security_infrastructure_score"), 0.13),
        (props.get("transport_access_score"), 0.18),
        (props.get("education_access_score"), 0.14),
        (props.get("health_access_score"), 0.10),
        (props.get("amenity_density_score"), 0.12),
        (props.get("green_access_score"), 0.08),
        (None if props.get("flood_penalty_score") is None else 100.0 - float(props["flood_penalty_score"]), 0.12),
        (None if props.get("environmental_penalty_score") is None else 100.0 - float(props["environmental_penalty_score"]), 0.05),
        (None if props.get("urban_informality_score") is None else 100.0 - float(props["urban_informality_score"]), 0.08),
    ]
    return weighted_score(components), sum(1 for value, _weight in components if value is not None)


def write_phase2_docs(generated_at: str, unavailable: list[dict[str, str]]) -> None:
    lines = [
        "# Phase 2 Source Research",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Confirmed Sources",
        "",
    ]
    for key, source in PHASE2_SOURCES.items():
        lines.append(f"- `{key}`: {source['source_name']} - {source['source_url']}")
    if unavailable:
        lines.extend(["", "## Unavailable Or Fallback Sources", ""])
        for item in unavailable:
            lines.append(f"- `{item['key']}`: {item['reason']} ({item['url']})")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Census 2022 PBA resource contains radio geometries and identifiers; population and households remain null until an official attribute table is found.",
            "- RENABAP primary host failed DNS resolution in this environment; fallback mirror is marked in metadata.",
            "- Gas official catalog exposes a resource URL, but the resource returned an HTTP error during this build.",
            "- ENRE exposes current AMBA outage pages, but no stable reusable historical endpoint was confirmed.",
        ]
    )
    write_text(Path("docs/phase2_source_research.md"), "\n".join(lines))
    write_text(
        Path("docs/sources.md"),
        f"""# Location Intelligence Sources

Generated at: {generated_at}

## Base And Existing Layers

- Zones: `data/geo/Zonas_Hurlingham_polygons.geojson`, generated from OpenStreetMap relations by the existing local pipeline.
- Security: municipal WP Google Maps markers, police-station seed, OSM zone polygons, and existing local scoring artifacts.
- Crime: SNIC/SAT/PBA municipal datasets. Crime remains municipality scope and low spatial precision; no neighborhood distribution is inferred.
- ARBA GeoARBA: cadastral parcels, blocks, cadastral hierarchy, and side-measure points from local raw archives and GeoARBA/WFS metadata.

## Phase 2 Official Sources

- Census 2022 radios: {PHASE2_SOURCES['census_2022_geojson_zip']['source_url']}
- Education establishments: {PHASE2_SOURCES['education_official_zip']['source_url']}
- Public health establishments 2025: {PHASE2_SOURCES['health_official_2025_zip']['source_url']}
- AMBA bus routes: {PHASE2_SOURCES['transport_routes_kml']['source_url']}
- SUBE charge points: {PHASE2_SOURCES['sube_points_geojson']['source_url']}
- ADA Reconquista flood-risk polygons: {PHASE2_SOURCES['flood_reconquista_zip']['source_url']}
- RENABAP: {PHASE2_SOURCES['renabap_official_geojson']['source_url']}

## Fallbacks And Limitations

- RENABAP primary host failed in this environment; the public mirror was used and marked as `medium_low` confidence.
- Census 2022 PBA downloadable resources inspected here provide radio geometry and identifiers, not population or household totals.
- Gas segments are listed in the national catalog but the download URL returned HTTP 404 during this build.
- ENRE outage pages were researched, but no stable reusable historical CSV/GeoJSON endpoint was confirmed.
- Official zoning/FOT/FOS remains unavailable as vector data; keep zoning attributes null until a reliable source or manual digitization workflow is approved.
""",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build phase-2 Hurlingham location-intelligence layers.")
    parser.add_argument("--zones", default="data/geo/Zonas_Hurlingham_polygons.geojson")
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()
    generated_at = args.generated_at or utc_now()
    unavailable: list[dict[str, str]] = []
    catalog: list[dict[str, Any]] = []
    try:
        zone_context = load_zone_context(Path(args.zones))
        census_rows = build_census_layers(zone_context, generated_at, catalog, unavailable)
        education_points = build_official_points_layer(
            source_key="education_official_zip",
            output_path=Path("data/geo/education/education_points_hurlingham.geojson"),
            output_name="education_points_hurlingham",
            zone_context=zone_context,
            generated_at=generated_at,
            catalog=catalog,
            unavailable=unavailable,
            municipality_field="municipio_nombre",
            fallback_text_match=True,
            confidence="high",
        )
        education_rows = build_education_zones(education_points, zone_context, generated_at, catalog)
        health_points = build_official_points_layer(
            source_key="health_official_2025_zip",
            output_path=Path("data/geo/health/health_points_hurlingham.geojson"),
            output_name="health_points_hurlingham",
            zone_context=zone_context,
            generated_at=generated_at,
            catalog=catalog,
            unavailable=unavailable,
            municipality_field="dom",
            fallback_text_match=True,
            confidence="high",
        )
        health_rows = build_health_zones(health_points, zone_context, generated_at, catalog)
        transport_rows = build_transport_layers(zone_context, generated_at, catalog, unavailable)
        renabap_rows = build_renabap_layers(zone_context, generated_at, catalog, unavailable)
        flood_rows = build_flood_layers(zone_context, generated_at, catalog, unavailable)
        utilities_rows = build_utilities_layers(zone_context, generated_at, catalog, unavailable)
        update_integrated_layer(generated_at, catalog)
        append_catalog(Path("docs/data_catalog.csv"), catalog)
        write_phase2_docs(generated_at, unavailable)
        print(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "census_zones": len(census_rows),
                    "education_points": len(education_points),
                    "health_points": len(health_points),
                    "transport_zones": len(transport_rows),
                    "renabap_zones": len(renabap_rows),
                    "flood_zones": len(flood_rows),
                    "utilities_zones": len(utilities_rows),
                    "unavailable": unavailable,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
