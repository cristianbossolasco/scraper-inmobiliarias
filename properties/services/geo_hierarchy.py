import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from properties.services.canonical_zones import zone_key


LAYER_FILES = {
    "partido": "01_partido_hurlingham.geojson",
    "localidades": "02_localidades_hurlingham.geojson",
    "zonas": "03_zonas_hurlingham_final.geojson",
    "microzonas": "03b_microzonas_hurlingham_final.geojson",
    "gaps": "04_gaps_zonas_hurlingham_final.geojson",
}
REQUIRED_LAYER_KEYS = ("partido", "localidades", "zonas", "gaps")
EVIDENCE_FILE = "03b_microzonas_hurlingham_evidence_points.geojson"


def default_geo_dir():
    return Path(settings.BASE_DIR) / "data" / "geo"


def _file_signature(path):
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None, None)
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def geo_hierarchy_signature(base_dir=None):
    geo_dir = Path(base_dir) if base_dir else default_geo_dir()
    paths = [geo_dir / filename for filename in [*LAYER_FILES.values(), EVIDENCE_FILE]]
    return "|".join(":".join(str(part) for part in _file_signature(path)) for path in paths)


def _read_geojson(path):
    path = Path(path)
    if not path.exists():
        return {"type": "FeatureCollection", "features": [], "metadata": {}}, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"type": "FeatureCollection", "features": [], "metadata": {}}, f"json_invalid:{exc.msg}"
    except OSError as exc:
        return {"type": "FeatureCollection", "features": [], "metadata": {}}, f"read_error:{exc}"
    if payload.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": [], "metadata": {}}, "not_feature_collection"
    payload["features"] = payload.get("features") or []
    payload["metadata"] = payload.get("metadata") or {}
    return payload, ""


def _layer_payload(geo_dir, filename):
    path = Path(geo_dir) / filename
    payload, error = _read_geojson(path)
    return {
        "path": str(path),
        "configured": bool(payload.get("features")),
        "error": error,
        "metadata": payload.get("metadata") or {},
        "geojson": _sanitize_collection(payload),
    }


def _label(props):
    return (
        props.get("canonical_name")
        or props.get("microzone_name")
        or props.get("zone_name")
        or props.get("locality_name")
        or props.get("name")
        or props.get("label")
        or ""
    )


def _sanitize_props(props):
    keys = [
        "level",
        "level_name",
        "canonical_name",
        "partido_name",
        "locality_name",
        "zone_name",
        "microzone_name",
        "parent_partido",
        "parent_locality",
        "parent_zone",
        "locality",
        "relation_id",
        "osm_relation_id",
        "area_km2",
        "source_method",
        "source_confidence",
        "candidate_source_confidence",
        "strict_source_available",
        "needs_manual_review",
        "source_warning",
        "evidence_point_count",
        "evidence_points_used_for_hull",
        "evidence_points_inside_parent_zone",
        "evidence_points_outside_parent_zone",
        "gap_id",
        "likely_missing_zone_candidates",
    ]
    return {key: props.get(key) for key in keys if key in props}


def _sanitize_collection(payload):
    features = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        features.append(
            {
                "type": "Feature",
                "id": feature.get("id") or props.get("gap_id") or zone_key(_label(props)),
                "geometry": feature.get("geometry"),
                "properties": _sanitize_props(props),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _node_id(level, label):
    return f"{level}:{zone_key(label)}"


def _node(level, label, feature=None):
    props = (feature or {}).get("properties") or {}
    return {
        "id": _node_id(level, label),
        "label": label,
        "level": level,
        "level_name": props.get("level_name") or level,
        "feature_id": (feature or {}).get("id") or props.get("gap_id") or zone_key(label),
        "area_km2": props.get("area_km2"),
        "source_confidence": props.get("source_confidence") or "",
        "needs_manual_review": bool(props.get("needs_manual_review")),
        "children": [],
    }


def _build_tree(layers):
    partido_features = layers["partido"]["geojson"]["features"]
    locality_features = layers["localidades"]["geojson"]["features"]
    zone_features = layers["zonas"]["geojson"]["features"]
    microzone_features = layers["microzonas"]["geojson"]["features"]
    root_feature = partido_features[0] if partido_features else None
    root_label = _label((root_feature or {}).get("properties") or {}) or "Partido de Hurlingham"
    root = _node("partido", root_label, root_feature)

    localities = {}
    for feature in sorted(locality_features, key=lambda item: _label(item.get("properties") or {})):
        props = feature.get("properties") or {}
        label = _label(props)
        localities[zone_key(label)] = _node("localidad", label, feature)

    zones_by_key = {}
    for feature in sorted(zone_features, key=lambda item: _label(item.get("properties") or {})):
        props = feature.get("properties") or {}
        label = _label(props)
        node = _node("zona", label, feature)
        zones_by_key[zone_key(label)] = node
        locality_key = zone_key(props.get("parent_locality") or props.get("locality"))
        localities.setdefault(locality_key, _node("localidad", props.get("parent_locality") or "Sin localidad"))
        localities[locality_key]["children"].append(node)

    for feature in sorted(microzone_features, key=lambda item: _label(item.get("properties") or {})):
        props = feature.get("properties") or {}
        label = _label(props)
        node = _node("microzona", label, feature)
        parent = zones_by_key.get(zone_key(props.get("parent_zone")))
        if parent:
            parent["children"].append(node)
        else:
            locality_key = zone_key(props.get("parent_locality"))
            localities.setdefault(locality_key, _node("localidad", props.get("parent_locality") or "Sin localidad"))
            localities[locality_key]["children"].append(node)

    root["children"] = [node for key, node in sorted(localities.items(), key=lambda item: item[1]["label"])]
    return root


def _read_evidence(geo_dir):
    path = Path(geo_dir) / EVIDENCE_FILE
    payload, error = _read_geojson(path)
    features = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        features.append(
            {
                "type": "Feature",
                "geometry": feature.get("geometry"),
                "properties": {
                    "microzone": props.get("microzone") or "Barrio Ingl\u00e9s",
                    "precision": props.get("precision") or "",
                    "inside_parent_zone": bool(props.get("inside_parent_zone")),
                    "matched_zone": props.get("matched_zone") or "",
                    "used_for_hull": bool(props.get("used_for_hull")),
                },
            }
        )
    return {
        "path": str(path),
        "configured": bool(features),
        "error": error,
        "barrio_ingles_points": {"type": "FeatureCollection", "features": features},
        "metadata": payload.get("metadata") or {},
    }


@lru_cache(maxsize=8)
def _geo_hierarchy_payload_cached(geo_dir_str, signature):
    geo_dir = Path(geo_dir_str)
    layers = {
        key: _layer_payload(geo_dir, filename)
        for key, filename in LAYER_FILES.items()
    }
    evidence = _read_evidence(geo_dir)
    return {
        "configured": all(layers[key]["configured"] for key in REQUIRED_LAYER_KEYS),
        "signature": signature,
        "tree": _build_tree(layers),
        "layers": layers,
        "evidence": evidence,
    }


def geo_hierarchy_payload(base_dir=None):
    geo_dir = Path(base_dir) if base_dir else default_geo_dir()
    signature = geo_hierarchy_signature(geo_dir)
    return _geo_hierarchy_payload_cached(str(geo_dir), signature)
