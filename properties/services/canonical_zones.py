import copy
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from properties.services.zone_names import UNIFIED_HURLINGHAM_CENTRO_ZONE


REQUIRED_CANONICAL_ZONES = (UNIFIED_HURLINGHAM_CENTRO_ZONE,)


def zone_label(props):
    return (props or {}).get("zone_name") or (props or {}).get("name") or (props or {}).get("label") or ""


def zone_key(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def default_zone_path():
    return Path(settings.ZONE_GEOJSON_PATH)


def _file_signature(path):
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None, None)
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=16)
def _load_cached(path_str, mtime_ns, size):
    if mtime_ns is None:
        return {"type": "FeatureCollection", "features": []}, "missing"
    try:
        payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"type": "FeatureCollection", "features": []}, f"json_invalid:{exc.msg}"
    except OSError as exc:
        return {"type": "FeatureCollection", "features": []}, f"read_error:{exc}"
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": []}, "not_feature_collection"
    payload["features"] = payload.get("features") or []
    return payload, ""


def canonical_zone_dataset(path=None):
    path_str, mtime_ns, size = _file_signature(path or default_zone_path())
    payload, error = _load_cached(path_str, mtime_ns, size)
    return {
        "path": path_str,
        "features": payload.get("features") or [],
        "configured": bool(payload.get("features")),
        "metadata": payload.get("metadata") or {},
        "error": error,
        "signature": ":".join(str(part) for part in (path_str, mtime_ns, size)),
    }


def canonical_zones_signature(path=None):
    return canonical_zone_dataset(path).get("signature") or ""


def canonical_zone_labels(path=None):
    return [
        zone_label((feature.get("properties") or {}))
        for feature in canonical_zone_dataset(path)["features"]
        if zone_label((feature.get("properties") or {}))
    ]


def missing_required_zones(path=None, required=None):
    labels = {zone_key(label) for label in canonical_zone_labels(path)}
    return [
        label
        for label in (required or REQUIRED_CANONICAL_ZONES)
        if zone_key(label) not in labels
    ]


def canonicalize_zone_features(metric_features, canonical_path=None):
    canonical = canonical_zone_dataset(canonical_path)
    if not canonical["features"]:
        return [], canonical

    by_key = {}
    for feature in metric_features or []:
        props = feature.get("properties") or {}
        key = zone_key(zone_label(props))
        if key and key not in by_key:
            by_key[key] = feature

    features = []
    for feature in canonical["features"]:
        canonical_props = copy.deepcopy(feature.get("properties") or {})
        label = zone_label(canonical_props)
        metric = by_key.get(zone_key(label))
        metric_props = copy.deepcopy((metric or {}).get("properties") or {})
        merged_props = {
            **canonical_props,
            **metric_props,
            "zone_name": label,
            "name": label,
            "canonical_zone": label,
        }
        features.append(
            {
                "type": "Feature",
                "id": feature.get("id") or canonical_props.get("id") or label,
                "geometry": copy.deepcopy(feature.get("geometry")),
                "properties": merged_props,
            }
        )
    return features, canonical
