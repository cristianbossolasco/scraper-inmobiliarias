import csv
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings


DEFAULT_CRIME_SUMMARY = Path(settings.BASE_DIR) / "data" / "geo" / "crime_summary_hurlingham.json"
DEFAULT_CRIME_ZONES = Path(settings.BASE_DIR) / "data" / "geo" / "crime_zones_hurlingham.geojson"
DEFAULT_CRIME_HOMICIDE_POINTS = (
    Path(settings.BASE_DIR) / "data" / "geo" / "crime_homicide_radio_points_hurlingham.geojson"
)
DEFAULT_CRIME_TIMESERIES = (
    Path(settings.BASE_DIR) / "data" / "geo" / "crime_hurlingham_municipality_timeseries.csv"
)

CRIME_METHOD_NOTES = {
    "municipal_scope": "Dato oficial agregado a nivel municipio/partido; no es dato barrial.",
    "homicide_points": "Puntos SAT-HD representados por centroide de radio censal; no son ubicaciones exactas.",
    "score_policy": "No se calcula ni se suma un score unico de crimen.",
}


def _file_signature(path):
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None, None)
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def crime_context_signature():
    return "|".join(
        ":".join(str(part) for part in _file_signature(path))
        for path in (
            DEFAULT_CRIME_SUMMARY,
            DEFAULT_CRIME_ZONES,
            DEFAULT_CRIME_HOMICIDE_POINTS,
            DEFAULT_CRIME_TIMESERIES,
        )
    )


@lru_cache(maxsize=24)
def _read_json_cached(path_str, mtime_ns, size):
    if mtime_ns is None:
        return None, "missing"
    try:
        return json.loads(Path(path_str).read_text(encoding="utf-8")), ""
    except json.JSONDecodeError as exc:
        return None, f"json_invalid:{exc.msg}"
    except OSError as exc:
        return None, f"read_error:{exc}"


def _read_json(path):
    path_str, mtime_ns, size = _file_signature(path)
    return _read_json_cached(path_str, mtime_ns, size)


def _feature_collection(path):
    payload, error = _read_json(path)
    if error:
        return {"path": str(Path(path)), "configured": False, "features": [], "error": error}
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return {
            "path": str(Path(path)),
            "configured": False,
            "features": [],
            "error": "not_feature_collection",
        }
    features = payload.get("features") or []
    if not isinstance(features, list):
        features = []
    return {
        "path": str(Path(path)),
        "configured": bool(features),
        "features": features,
        "metadata": payload.get("metadata") or {},
        "error": "",
    }


def _summary_payload(path=None):
    payload, error = _read_json(path or DEFAULT_CRIME_SUMMARY)
    if error or not isinstance(payload, dict):
        return {"configured": False, "path": str(path or DEFAULT_CRIME_SUMMARY), "error": error or "invalid"}
    metrics = payload.get("metrics") or {}
    return {
        "configured": bool(metrics),
        "path": str(path or DEFAULT_CRIME_SUMMARY),
        "generated_at": payload.get("generated_at") or metrics.get("crime_generated_at") or "",
        "metrics": metrics,
        "validation": payload.get("validation") or {},
        "source_row_counts": payload.get("source_row_counts") or {},
        "raw_files": payload.get("raw_files") or {},
        "download_manifest": {
            "generated_at": (payload.get("download_manifest") or {}).get("generated_at") or "",
            "sources": [
                {
                    "key": item.get("key") or "",
                    "name": item.get("name") or "",
                    "role": item.get("role") or "",
                    "url": item.get("url") or "",
                    "status": item.get("status") or "",
                    "size_bytes": item.get("size_bytes"),
                }
                for item in (payload.get("download_manifest") or {}).get("sources", [])
                if isinstance(item, dict)
            ],
        },
        "methodology": CRIME_METHOD_NOTES,
        "error": "",
    }


def _numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value):
    parsed = _numeric(value)
    return int(parsed) if parsed == int(parsed) else round(parsed, 2)


def _period_key(row):
    try:
        year = int(row.get("period_year") or 0)
    except (TypeError, ValueError):
        year = 0
    try:
        month = int(row.get("period_month") or 0)
    except (TypeError, ValueError):
        month = 0
    return year, month


def _group_label(value):
    return str(value or "sin_dato").strip() or "sin_dato"


@lru_cache(maxsize=12)
def _timeseries_cached(path_str, mtime_ns, size):
    if mtime_ns is None:
        return {
            "configured": False,
            "path": path_str,
            "monthly": [],
            "property_monthly": [],
            "property_seasonality": [],
            "error": "missing",
        }

    monthly = {}
    property_monthly = {}
    property_seasonality = {}
    try:
        with Path(path_str).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if (row.get("municipality") or "").strip().lower() != "hurlingham":
                    continue
                if (row.get("province") or "").strip().lower() != "buenos aires":
                    continue
                year, month = _period_key(row)
                if not year or not month:
                    continue
                group = _group_label(row.get("crime_group"))
                measure = _group_label(row.get("measure"))
                value = _numeric(row.get("value"))
                source_key = _group_label(row.get("source_key"))
                if source_key == "snic_departamentos_mensual":
                    key = (year, month, group)
                    monthly.setdefault(
                        key,
                        {
                            "period_year": year,
                            "period_month": month,
                            "period": f"{year:04d}-{month:02d}",
                            "crime_group": group,
                            "cantidad_hechos": 0.0,
                            "cantidad_victimas": 0.0,
                        },
                    )
                    if measure in {"cantidad_hechos", "cantidad_victimas"}:
                        monthly[key][measure] += value
                elif source_key == "sat_propiedad" and measure == "cantidad_hechos":
                    key = (year, month, group)
                    property_monthly.setdefault(
                        key,
                        {
                            "period_year": year,
                            "period_month": month,
                            "period": f"{year:04d}-{month:02d}",
                            "crime_group": group,
                            "cantidad_hechos": 0.0,
                        },
                    )
                    property_monthly[key]["cantidad_hechos"] += value
                    property_seasonality[(year, month)] = property_seasonality.get((year, month), 0.0) + value
    except OSError as exc:
        return {
            "configured": False,
            "path": path_str,
            "monthly": [],
            "property_monthly": [],
            "property_seasonality": [],
            "error": f"read_error:{exc}",
        }

    def normalize_rows(values):
        return [
            {
                **row,
                "cantidad_hechos": _int_value(row.get("cantidad_hechos")),
                "cantidad_victimas": _int_value(row.get("cantidad_victimas")),
            }
            for row in sorted(values, key=lambda item: (item["period_year"], item["period_month"], item.get("crime_group", "")))
        ]

    seasonality = [
        {
            "period_year": year,
            "period_month": month,
            "period": f"{year:04d}-{month:02d}",
            "value": _int_value(value),
        }
        for (year, month), value in sorted(property_seasonality.items())
    ]
    property_rows = [
        {**row, "cantidad_hechos": _int_value(row.get("cantidad_hechos"))}
        for row in sorted(
            property_monthly.values(),
            key=lambda item: (item["period_year"], item["period_month"], item["crime_group"]),
        )
    ]
    return {
        "configured": bool(monthly or property_monthly),
        "path": path_str,
        "monthly": normalize_rows(monthly.values()),
        "property_monthly": property_rows,
        "property_seasonality": seasonality,
        "error": "",
    }


def timeseries_payload(path=None):
    path_str, mtime_ns, size = _file_signature(path or DEFAULT_CRIME_TIMESERIES)
    return _timeseries_cached(path_str, mtime_ns, size)


def _zone_label(props):
    return props.get("zone_name") or props.get("name") or props.get("label") or "Zona"


def _sanitize_zone(feature):
    props = feature.get("properties") or {}
    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": {
            "id": props.get("id") or feature.get("id") or "",
            "label": _zone_label(props),
            "locality": props.get("locality") or "",
            "crime_data_scope": props.get("crime_data_scope") or "municipio",
            "crime_spatial_precision": props.get("crime_spatial_precision") or "low",
            "crime_municipality": props.get("crime_municipality") or "Hurlingham",
            "crime_metric_window_start_year": props.get("crime_metric_window_start_year"),
            "crime_metric_window_end_year": props.get("crime_metric_window_end_year"),
            "reported_crimes_total": props.get("reported_crimes_total"),
            "reported_property_crime_count": props.get("reported_property_crime_count"),
            "reported_robbery_count": props.get("reported_robbery_count"),
            "reported_theft_count": props.get("reported_theft_count"),
            "reported_vehicle_crime_count": props.get("reported_vehicle_crime_count"),
            "reported_homicide_count": props.get("reported_homicide_count"),
            "reported_homicide_victim_count": props.get("reported_homicide_victim_count"),
            "reported_injury_count": props.get("reported_injury_count"),
            "reported_sexual_integrity_count": props.get("reported_sexual_integrity_count"),
            "reported_crime_data_note": props.get("reported_crime_data_note") or CRIME_METHOD_NOTES["municipal_scope"],
        },
    }


def _sanitize_homicide_point(feature):
    props = feature.get("properties") or {}
    return {
        "type": "Feature",
        "id": feature.get("id") or props.get("id") or "",
        "geometry": feature.get("geometry"),
        "properties": {
            "id": props.get("id") or feature.get("id") or "",
            "source": props.get("source") or "SAT Homicidios dolosos",
            "crime_group": props.get("crime_group") or "homicidio",
            "crime_type": props.get("crime_type") or "",
            "period_year": props.get("period_year"),
            "period_month": props.get("period_month"),
            "id_hecho": props.get("id_hecho") or "",
            "victims_count": props.get("victims_count") or 0,
            "radio_censal": props.get("radio_censal") or "",
            "tipo_lugar": props.get("tipo_lugar") or "",
            "clase_arma": props.get("clase_arma") or "",
            "assigned_zone_name": props.get("assigned_zone_name") or "",
            "is_exact_location": props.get("is_exact_location") is True,
            "spatial_precision": props.get("spatial_precision") or "radio_censal_centroid",
            "source_note": props.get("source_note") or CRIME_METHOD_NOTES["homicide_points"],
        },
    }


def homicide_counts_by_zone(point_path=None):
    points = _feature_collection(point_path or DEFAULT_CRIME_HOMICIDE_POINTS)
    counts = {}
    for feature in points["features"]:
        props = feature.get("properties") or {}
        zone = props.get("assigned_zone_name") or "Sin zona"
        counts.setdefault(zone, {"event_count": 0, "victim_count": 0})
        counts[zone]["event_count"] += 1
        counts[zone]["victim_count"] += int(_numeric(props.get("victims_count") or 0))
    return counts


def crime_dashboard_summary(summary_path=None):
    summary = _summary_payload(summary_path)
    if not summary.get("configured"):
        return {
            "configured": False,
            "path": summary.get("path"),
            "error": summary.get("error") or "missing",
            "methodology": CRIME_METHOD_NOTES,
            "summary": {},
        }
    return {
        "configured": True,
        "path": summary["path"],
        "generated_at": summary.get("generated_at") or "",
        "metrics": summary.get("metrics") or {},
        "validation": summary.get("validation") or {},
        "source_row_counts": summary.get("source_row_counts") or {},
        "methodology": CRIME_METHOD_NOTES,
    }


def crime_layers_payload(
    summary_path=None,
    zone_path=None,
    point_path=None,
    timeseries_path=None,
    max_points=1200,
):
    summary = _summary_payload(summary_path)
    zone_dataset = _feature_collection(zone_path or DEFAULT_CRIME_ZONES)
    point_dataset = _feature_collection(point_path or DEFAULT_CRIME_HOMICIDE_POINTS)
    timeseries = timeseries_payload(timeseries_path)

    zones = [_sanitize_zone(feature) for feature in zone_dataset["features"]]
    points = [_sanitize_homicide_point(feature) for feature in point_dataset["features"][:max_points]]
    configured = bool(summary.get("configured") and zone_dataset.get("configured"))
    error = ""
    if not configured:
        error = summary.get("error") or zone_dataset.get("error") or "missing"

    return {
        "configured": configured,
        "paths": {
            "summary": summary.get("path"),
            "zones": zone_dataset.get("path"),
            "homicide_points": point_dataset.get("path"),
            "timeseries": timeseries.get("path"),
        },
        "summary": summary if summary.get("configured") else crime_dashboard_summary(summary_path),
        "zones": {"type": "FeatureCollection", "features": zones},
        "homicide_points": {"type": "FeatureCollection", "features": points},
        "timeseries": {
            "configured": timeseries.get("configured", False),
            "monthly": timeseries.get("monthly", []),
            "property_monthly": timeseries.get("property_monthly", []),
            "property_seasonality": timeseries.get("property_seasonality", []),
            "error": timeseries.get("error", ""),
        },
        "methodology": CRIME_METHOD_NOTES,
        "error": error,
    }
