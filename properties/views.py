import csv
import hashlib
import io
import json
import re
import statistics
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Max, Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import Agency, Listing, ListingImage, Property, PropertyLocation, ScrapeJob, Source
from .services.data_quality import (
    curated_metric_values,
    curated_price_m2_values,
    curated_price_values,
    property_anomalies,
    valid_area,
    valid_price,
    valid_price_per_m2,
    valid_value,
)
from .services.scraping import (
    ActiveScrapeJobError,
    create_scrape_job,
    mark_stale_running_jobs,
    retry_scrape_job,
    retry_scrape_job_errors,
    serialize_job,
    source_catalog,
    start_scrape_job,
    BLOCKED_SOURCE_SLUGS,
)
from .services.security_scoring import security_layers_payload
from .services.geocoding import address_number, street_key
from .services.normalization import (
    clean_address_for_storage,
    locality_from_neighborhood,
    normalize_address,
    normalize_currency,
    normalize_locality,
    normalize_neighborhood_name,
    normalize_whitespace,
    parse_decimal,
)
from .services.spatial import (
    haversine_km,
    point_in_polygon,
    radius_bbox,
    rtree_property_ids,
)


def _decimal(value):
    try:
        return Decimal(value) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


EDITABLE_PROPERTY_FIELDS = {
    "title",
    "property_type",
    "operation",
    "status",
    "currency",
    "price",
    "address",
    "locality",
    "neighborhood",
    "description",
    "rooms",
    "bedrooms",
    "bathrooms",
    "garages",
    "toilets",
    "covered_area",
    "total_area",
    "land_area",
    "uncovered_area",
    "semicovered_area",
    "front_width",
    "lot_depth",
    "building_floors",
    "age_years",
    "features",
}

DECIMAL_EDIT_FIELDS = {
    "price",
    "bathrooms",
    "covered_area",
    "total_area",
    "land_area",
    "uncovered_area",
    "semicovered_area",
    "front_width",
    "lot_depth",
}

INTEGER_EDIT_FIELDS = {
    "rooms",
    "bedrooms",
    "garages",
    "toilets",
    "building_floors",
    "age_years",
}

CHOICE_EDIT_FIELDS = {
    "property_type": Property.Type.values,
    "operation": ["sale", "rent"],
    "status": Property.Status.values,
}


def same_geocoding_target(before, after):
    return (
        bool(before and after)
        and address_number(before) == address_number(after)
        and street_key(before) == street_key(after)
    )


def _coerce_edit_value(field, value):
    if field in DECIMAL_EDIT_FIELDS:
        return parse_decimal(value)
    if field in INTEGER_EDIT_FIELDS:
        parsed = _int(value)
        if parsed is not None and parsed < 0:
            raise ValueError("Debe ser mayor o igual a cero.")
        return parsed
    if field == "features":
        if isinstance(value, list):
            return [normalize_whitespace(item) for item in value if normalize_whitespace(item)]
        return [
            normalize_whitespace(item)
            for item in re.split(r"[\n,;]", str(value or ""))
            if normalize_whitespace(item)
        ]
    if field == "address":
        return clean_address_for_storage(value) or normalize_whitespace(value)
    if field == "locality":
        return normalize_locality(value or "")
    if field == "neighborhood":
        return normalize_neighborhood_name(value or "") or normalize_whitespace(value)
    if field == "currency":
        return normalize_currency(value or "") if value else ""
    if field in {"title", "description"}:
        return str(value or "").strip()
    return str(value or "").strip()


def _serialize_property_edit(property_obj):
    return {
        "id": property_obj.pk,
        "title": property_obj.title,
        "property_type": property_obj.property_type,
        "operation": property_obj.operation,
        "status": property_obj.status,
        "currency": property_obj.currency,
        "price": str(property_obj.price) if property_obj.price is not None else "",
        "address": property_obj.address,
        "locality": property_obj.locality,
        "neighborhood": property_obj.neighborhood,
        "description": property_obj.description,
        "rooms": property_obj.rooms,
        "bedrooms": property_obj.bedrooms,
        "bathrooms": str(property_obj.bathrooms) if property_obj.bathrooms is not None else "",
        "garages": property_obj.garages,
        "toilets": property_obj.toilets,
        "covered_area": str(property_obj.covered_area) if property_obj.covered_area is not None else "",
        "total_area": str(property_obj.total_area) if property_obj.total_area is not None else "",
        "land_area": str(property_obj.land_area) if property_obj.land_area is not None else "",
        "uncovered_area": str(property_obj.uncovered_area) if property_obj.uncovered_area is not None else "",
        "semicovered_area": str(property_obj.semicovered_area) if property_obj.semicovered_area is not None else "",
        "front_width": str(property_obj.front_width) if property_obj.front_width is not None else "",
        "lot_depth": str(property_obj.lot_depth) if property_obj.lot_depth is not None else "",
        "building_floors": property_obj.building_floors,
        "age_years": property_obj.age_years,
        "features": property_obj.features or [],
        "manual_overrides": property_obj.manual_overrides or {},
        "data_manually_corrected_at": property_obj.data_manually_corrected_at.isoformat()
        if property_obj.data_manually_corrected_at
        else "",
    }


def _fts_ids(query):
    tokens = [token.replace('"', "") for token in query.split() if token.strip()]
    if not tokens:
        return []
    expression = " AND ".join(f'"{token}"*' for token in tokens)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rowid FROM property_fts WHERE property_fts MATCH %s",
            [expression],
        )
        return [row[0] for row in cursor.fetchall()]


def safe_return_to(request):
    fallback = reverse("properties:search")
    target = request.GET.get("return_to") or fallback
    if not target.startswith("/"):
        return fallback
    if url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return fallback


def build_detail_url(property_id, current_query, return_path=None):
    base = reverse("properties:detail", args=[property_id])
    query = current_query.urlencode() if hasattr(current_query, "urlencode") else str(current_query)
    return_to = return_path or reverse("properties:search")
    if query:
        return_to = f"{return_to}?{query}"
    return f"{base}?{urlencode({'return_to': return_to})}"


def _param_values(params, key):
    if hasattr(params, "getlist"):
        values = params.getlist(key)
    else:
        value = params.get(key) if hasattr(params, "get") else None
        values = value if isinstance(value, (list, tuple)) else [value]
    return [str(value) for value in values if value not in (None, "")]


def _canonical_display_locality(property_obj):
    return (
        normalize_locality(property_obj.detected_locality)
        or normalize_locality(property_obj.locality)
        or locality_from_neighborhood(property_obj.detected_neighborhood)
        or locality_from_neighborhood(property_obj.neighborhood)
        or locality_from_neighborhood(property_obj.inferred_neighborhood)
        or "Sin dato"
    )


def filter_context(params):
    multi_keys = (
        "property_type",
        "currency",
        "locality",
        "neighborhood",
        "agency",
        "source",
        "feature",
        "location_quality",
        "favorite",
        "review_state",
        "show_hidden",
        "status",
        "security_level",
        "security_zone",
    )
    return {
        "agencies": Agency.objects.filter(listings__isnull=False).distinct().order_by("name"),
        "sources": Source.objects.exclude(slug__in=BLOCKED_SOURCE_SLUGS).order_by("name"),
        "property_types": Property.Type.choices,
        "statuses": Property.Status.choices,
        "location_confidences": Property.LocationConfidence.choices,
        "security_levels": _security_level_options(),
        "security_zone_options": _security_zone_options(),
        "localities": ["Hurlingham", "Villa Tesei", "William C. Morris"],
        "neighborhood_options": _neighborhood_options(),
        "features": ["Pileta", "Quincho", "Jardin", "Parrilla", "Apto credito"],
        "query_params": params,
        "selected_filters": {
            key: (_selected_neighborhoods(params) if key == "neighborhood" else _param_values(params, key))
            for key in multi_keys
        },
    }


def _neighborhood_options():
    counts = {}
    for neighborhood, detected, inferred in Property.objects.values_list(
        "neighborhood", "detected_neighborhood", "inferred_neighborhood"
    ):
        for name in {neighborhood, detected, inferred}:
            normalized = normalize_neighborhood_name(name)
            if normalized:
                counts[normalized] = counts.get(normalized, 0) + 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    ]


def _security_zone_options():
    return [
        value
        for value in Property.objects.exclude(security_zone_label="")
        .values_list("security_zone_label", flat=True)
        .distinct()
        .order_by("security_zone_label")
    ]


def _security_level_options():
    defaults = ["alta", "media_alta", "media", "baja"]
    values = [
        value
        for value in Property.objects.exclude(security_level="")
        .values_list("security_level", flat=True)
        .distinct()
    ]
    return sorted(set(defaults + values))


def _selected_neighborhoods(params):
    selected = []
    for value in _param_values(params, "neighborhood"):
        normalized = normalize_neighborhood_name(value)
        if normalized and normalized not in selected:
            selected.append(normalized)
    return selected


def query_url(params, overrides=None, remove=None, path="/"):
    data = params.copy()
    for key in remove or []:
        data.pop(key, None)
    for key, value in (overrides or {}).items():
        data.pop(key, None)
        if value not in (None, ""):
            data[key] = value
    query = data.urlencode() if hasattr(data, "urlencode") else urlencode(data, doseq=True)
    return f"{path}?{query}" if query else path


TABLE_SORTS = {
    "price": {"label": "Precio", "key": lambda item, distances: item.price},
    "title": {"label": "Publicacion", "key": lambda item, distances: item.title or ""},
    "agency": {
        "label": "Inmobiliaria",
        "key": lambda item, distances: (
            _primary_listing(item).agency.name
            if _primary_listing(item) and _primary_listing(item).agency
            else ""
        ),
    },
    "source": {
        "label": "Fuente",
        "key": lambda item, distances: (
            _primary_listing(item).source.name if _primary_listing(item) else ""
        ),
    },
    "locality": {"label": "Localidad", "key": lambda item, distances: _canonical_display_locality(item)},
    "bedrooms": {"label": "Dorm.", "key": lambda item, distances: item.bedrooms},
    "bathrooms": {"label": "Banos", "key": lambda item, distances: item.bathrooms},
    "covered_area": {"label": "Cub.", "key": lambda item, distances: item.covered_area},
    "land_area": {"label": "Terr.", "key": lambda item, distances: item.land_area},
    "area": {
        "label": "Superficie",
        "key": lambda item, distances: item.land_area or item.total_area or item.covered_area,
    },
    "price_m2": {"label": "USD/m2", "key": lambda item, distances: valid_price_per_m2(item)},
    "first_seen": {"label": "Alta", "key": lambda item, distances: item.first_seen_at},
    "last_seen": {"label": "Vista", "key": lambda item, distances: item.last_seen_at},
    "reviewed": {"label": "Revision", "key": lambda item, distances: 1 if item.reviewed_at else 0},
    "quality": {"label": "Calidad", "key": lambda item, distances: _quality_score(item)},
    "distance": {"label": "Distancia", "key": lambda item, distances: distances.get(item.pk)},
}

TABLE_FILTER_KEYS = {
    "title",
    "price_min",
    "price_max",
    "agency",
    "source",
    "locality",
    "bedrooms_min",
    "bedrooms_max",
    "bathrooms_min",
    "bathrooms_max",
    "covered_min",
    "covered_max",
    "land_min",
    "land_max",
    "price_m2_min",
    "price_m2_max",
    "security_coverage_min",
    "security_coverage_max",
    "security_risk_min",
    "security_risk_max",
    "security_level",
    "security_zone",
}


def _sort_field(token):
    return token[1:] if token.startswith("-") else token


def _sort_tokens(raw_sort):
    raw_tokens = (raw_sort or "-last_seen").split(",")
    tokens = []
    seen = set()
    for raw_token in raw_tokens:
        token = raw_token.strip()
        field = _sort_field(token)
        if field not in TABLE_SORTS or field in seen:
            continue
        seen.add(field)
        tokens.append(f"-{field}" if token.startswith("-") else field)
    return tokens or ["-last_seen"]


def _sort_value(value):
    if value is None or value == "":
        return None
    if hasattr(value, "timestamp"):
        return value.timestamp()
    if isinstance(value, str):
        return value.casefold()
    return value


def _sort_with_missing_last(items, key_func, reverse=False):
    present = []
    missing = []
    for item in items:
        value = _sort_value(key_func(item))
        if value is None:
            missing.append(item)
        else:
            present.append((item, value))
    present.sort(key=lambda pair: pair[1], reverse=reverse)
    return [item for item, _ in present] + missing


def _apply_sort(properties, sort_tokens, distances):
    items = list(properties)
    for token in reversed(sort_tokens):
        field = _sort_field(token)
        config = TABLE_SORTS[field]
        items = _sort_with_missing_last(
            items,
            lambda item, sort_key=config["key"]: sort_key(item, distances),
            reverse=token.startswith("-"),
        )
    return items


def _query_param_pairs(params, exclude=None):
    excluded = set(exclude or [])
    pairs = []
    for key, values in params.lists():
        if key in excluded:
            continue
        for value in values:
            if value not in (None, ""):
                pairs.append((key, value))
    return pairs


def _sort_url(params, field):
    tokens = _sort_tokens(params.get("sort"))
    current_index = next(
        (index for index, token in enumerate(tokens) if _sort_field(token) == field),
        None,
    )
    if current_index == 0:
        current = tokens[0]
        next_token = field if current.startswith("-") else f"-{field}"
        rest = tokens[1:]
    elif current_index is not None:
        next_token = tokens[current_index]
        rest = [token for index, token in enumerate(tokens) if index != current_index]
    else:
        next_token = field
        rest = tokens
    return query_url(params, {"sort": ",".join([next_token, *rest])}, remove=["page"])


def _remove_sort_url(params, field):
    tokens = [token for token in _sort_tokens(params.get("sort")) if _sort_field(token) != field]
    return query_url(params, {"sort": ",".join(tokens) if tokens else "-last_seen"}, remove=["page"])


def table_context(params):
    sort_tokens = _sort_tokens(params.get("sort"))
    active_by_field = {
        _sort_field(token): {
            "token": token,
            "position": index + 1,
            "direction": "desc" if token.startswith("-") else "asc",
        }
        for index, token in enumerate(sort_tokens)
    }
    columns = [
        {"key": "actions", "label": "Acciones", "sortable": False},
        {"key": "price", "label": "Precio", "sortable": True},
        {"key": "title", "label": "Publicacion", "sortable": True},
        {"key": "agency", "label": "Inmobiliaria", "sortable": True},
        {"key": "source", "label": "Fuente", "sortable": True},
        {"key": "locality", "label": "Localidad", "sortable": True},
        {"key": "bedrooms", "label": "Dorm.", "sortable": True},
        {"key": "bathrooms", "label": "Banos", "sortable": True},
        {"key": "covered_area", "label": "Cub.", "sortable": True},
        {"key": "land_area", "label": "Terr.", "sortable": True},
        {"key": "price_m2", "label": "USD/m2", "sortable": True},
    ]
    for column in columns:
        if not column["sortable"]:
            continue
        state = active_by_field.get(column["key"])
        column.update(
            {
                "sort_url": _sort_url(params, column["key"]),
                "remove_sort_url": _remove_sort_url(params, column["key"]) if state else "",
                "sort_direction": state["direction"] if state else "",
                "sort_position": state["position"] if state else "",
                "sort_icon": "arrow-down" if state and state["direction"] == "desc" else "arrow-up",
            }
        )
    active_sorts = []
    for token in sort_tokens:
        field = _sort_field(token)
        active_sorts.append(
            {
                "field": field,
                "label": TABLE_SORTS[field]["label"],
                "direction": "desc" if token.startswith("-") else "asc",
                "remove_url": _remove_sort_url(params, field),
            }
        )
    return {
        "table_columns": columns,
        "table_sort": {
            column["key"]: column for column in columns if column.get("sortable")
        },
        "active_sorts": active_sorts,
        "clear_sort_url": query_url(params, {"sort": "-last_seen"}, remove=["page"]),
        "clear_table_filters_url": query_url(params, remove=[*TABLE_FILTER_KEYS, "page"]),
        "table_hidden_params": _query_param_pairs(params, exclude=[*TABLE_FILTER_KEYS, "page"]),
    }


def filtered_properties(params, include_listings=True):
    queryset = Property.objects.select_related("location")
    if include_listings:
        listing_queryset = Listing.objects.select_related("agency", "source")
        if include_listings != "summary":
            listing_queryset = listing_queryset.prefetch_related(
                Prefetch("images", queryset=ListingImage.objects.order_by("position"))
            )
        queryset = queryset.prefetch_related(
            Prefetch(
                "listings",
                queryset=listing_queryset,
            )
        )
    queryset = queryset.all()
    query = (params.get("q") or "").strip()
    if query:
        queryset = queryset.filter(pk__in=_fts_ids(query))
    title = (params.get("title") or "").strip()
    if title:
        queryset = queryset.filter(title__icontains=title)

    filters = {
        "property_type": "property_type",
        "operation": "operation",
        "currency": "currency",
        "status": "status",
        "security_level": "security_level",
        "security_zone": "security_zone_label",
    }
    for parameter, field in filters.items():
        values = _param_values(params, parameter)
        if values:
            queryset = queryset.filter(**{f"{field}__in": values})

    locality_values = _param_values(params, "locality")
    if locality_values:
        queryset = queryset.filter(
            Q(locality__in=locality_values) | Q(detected_locality__in=locality_values)
        )

    if not _param_values(params, "status"):
        queryset = queryset.filter(status=Property.Status.ACTIVE)

    neighborhood_values = _selected_neighborhoods(params)
    if neighborhood_values:
        aliases_by_value = {value: {value} for value in neighborhood_values}
        for neighborhood, detected, inferred in Property.objects.values_list(
            "neighborhood", "detected_neighborhood", "inferred_neighborhood"
        ):
            for raw_name in (neighborhood, detected, inferred):
                normalized = normalize_neighborhood_name(raw_name)
                if normalized in aliases_by_value and raw_name:
                    aliases_by_value[normalized].add(raw_name)
        neighborhood_q = Q()
        for aliases in aliases_by_value.values():
            neighborhood_q |= (
                Q(neighborhood__in=aliases)
                | Q(detected_neighborhood__in=aliases)
                | Q(inferred_neighborhood__in=aliases)
            )
        queryset = queryset.filter(neighborhood_q)

    if not params.get("operation") and params.get("show_non_sale") != "1":
        queryset = queryset.filter(operation="sale")

    show_hidden_values = _param_values(params, "show_hidden")
    if "1" not in show_hidden_values:
        queryset = queryset.filter(is_hidden=False)
    if "1" in _param_values(params, "favorite"):
        queryset = queryset.filter(is_favorite=True)
    review_states = set(_param_values(params, "review_state"))
    if review_states == {"pending"}:
        queryset = queryset.filter(reviewed_at__isnull=True)
    elif review_states == {"reviewed"}:
        queryset = queryset.filter(reviewed_at__isnull=False)
    location_qualities = set(_param_values(params, "location_quality"))
    if location_qualities:
        location_q = Q()
        if "reliable" in location_qualities:
            location_q |= Q(location_confidence__in=[
                Property.LocationConfidence.HIGH,
                Property.LocationConfidence.MEDIUM,
            ])
        if "unreliable" in location_qualities:
            location_q |= (
                Q(location_confidence__in=[
                    Property.LocationConfidence.LOW,
                    Property.LocationConfidence.UNKNOWN,
                ])
                | Q(location__isnull=True)
            )
        if "detected" in location_qualities:
            location_q |= (
                ~Q(location_source=Property.LocationSource.UNKNOWN)
                | (Q(address__isnull=False) & ~Q(address=""))
                | (Q(detected_address__isnull=False) & ~Q(detected_address=""))
            )
        queryset = queryset.filter(location_q)

    agency_values = _param_values(params, "agency")
    if agency_values:
        queryset = queryset.filter(listings__agency_id__in=agency_values)
    source_values = _param_values(params, "source")
    if source_values:
        queryset = queryset.filter(listings__source_id__in=source_values)

    ranges = (
        ("price_min", "price__gte", _decimal),
        ("price_max", "price__lte", _decimal),
        ("security_coverage_min", "security_coverage_score__gte", _float),
        ("security_coverage_max", "security_coverage_score__lte", _float),
        ("security_risk_min", "security_risk_score__gte", _float),
        ("security_risk_max", "security_risk_score__lte", _float),
        ("land_min", "land_area__gte", _decimal),
        ("land_max", "land_area__lte", _decimal),
        ("covered_min", "covered_area__gte", _decimal),
        ("covered_max", "covered_area__lte", _decimal),
        ("bedrooms_min", "bedrooms__gte", _int),
        ("bedrooms_max", "bedrooms__lte", _int),
        ("bathrooms_min", "bathrooms__gte", _decimal),
        ("bathrooms_max", "bathrooms__lte", _decimal),
        ("garages_min", "garages__gte", _int),
    )
    for parameter, lookup, parser in ranges:
        value = parser(params.get(parameter))
        if value is not None:
            queryset = queryset.filter(**{lookup: value})

    features = _param_values(params, "feature")
    if features:
        feature_q = Q()
        for feature in features:
            feature_q |= Q(features__icontains=feature) | Q(description__icontains=feature)
        queryset = queryset.filter(feature_q)

    spatial_ids = None
    bounds = [_float(params.get(key)) for key in ("south", "west", "north", "east")]
    if all(value is not None for value in bounds):
        spatial_ids = set(rtree_property_ids(*bounds))

    radius_lat = _float(params.get("radius_lat"))
    radius_lng = _float(params.get("radius_lng"))
    radius_km = _float(params.get("radius_km"))
    distances = {}
    if None not in (radius_lat, radius_lng, radius_km):
        candidate_ids = set(rtree_property_ids(*radius_bbox(radius_lat, radius_lng, radius_km)))
        spatial_ids = candidate_ids if spatial_ids is None else spatial_ids & candidate_ids

    polygon_raw = params.get("polygon")
    polygon = None
    if polygon_raw:
        try:
            polygon = json.loads(polygon_raw)
            lngs = [point[0] for point in polygon]
            lats = [point[1] for point in polygon]
            candidate_ids = set(
                rtree_property_ids(min(lats), min(lngs), max(lats), max(lngs))
            )
            spatial_ids = candidate_ids if spatial_ids is None else spatial_ids & candidate_ids
        except (ValueError, TypeError, IndexError):
            polygon = None

    if spatial_ids is not None:
        queryset = queryset.filter(pk__in=spatial_ids)

    properties = list(queryset.distinct())
    quality_field = params.get("quality_field")
    quality_state = params.get("quality_state")
    if quality_field and quality_state in {"present", "missing"}:
        properties = [
            item for item in properties
            if _quality_matches(item, quality_field, quality_state)
        ]
    price_m2_min = _decimal(params.get("price_m2_min"))
    price_m2_max = _decimal(params.get("price_m2_max"))
    if price_m2_min is not None or price_m2_max is not None:
        selected = []
        for property_obj in properties:
            value = valid_price_per_m2(property_obj)
            if value is None:
                continue
            if price_m2_min is not None and value < price_m2_min:
                continue
            if price_m2_max is not None and value > price_m2_max:
                continue
            selected.append(property_obj)
        properties = selected
    if None not in (radius_lat, radius_lng, radius_km):
        selected = []
        for property_obj in properties:
            if not hasattr(property_obj, "location"):
                continue
            distance = haversine_km(
                radius_lat,
                radius_lng,
                property_obj.location.latitude,
                property_obj.location.longitude,
            )
            if distance <= radius_km:
                distances[property_obj.pk] = distance
                selected.append(property_obj)
        properties = selected

    if polygon:
        properties = [
            property_obj
            for property_obj in properties
            if hasattr(property_obj, "location")
            and point_in_polygon(
                property_obj.location.latitude,
                property_obj.location.longitude,
                polygon,
            )
        ]

    properties = _apply_sort(properties, _sort_tokens(params.get("sort")), distances)
    return properties, distances


def _quality_matches(property_obj, field, state):
    present = False
    if field == "price":
        present = valid_price(property_obj) is not None
    elif field == "surface":
        present = valid_area(property_obj) is not None
    elif field == "location":
        present = hasattr(property_obj, "location")
    elif field == "address":
        present = bool(property_obj.address or property_obj.detected_address)
    elif field == "image":
        present = any(_listing_image_url(listing) for listing in _listings(property_obj))
    elif field == "link":
        present = bool(_listings(property_obj))
    elif field == "agency":
        present = any(listing.agency_id for listing in _listings(property_obj))
    return present if state == "present" else not present


def _listings(property_obj):
    return list(property_obj.listings.all())


def _primary_listing(property_obj):
    listings = _listings(property_obj)
    return next((item for item in listings if item.active), None) or (listings[0] if listings else None)


def _listing_image_url(listing):
    if not listing:
        return ""
    prefetched = getattr(listing, "_prefetched_objects_cache", None)
    if prefetched is None or "images" not in prefetched:
        return ""
    images = list(listing.images.all())
    return images[0].url if images else ""


def _quality_score(property_obj):
    listings = _listings(property_obj)
    return round(
        sum(
            1
            for check in (
                property_obj.price is not None,
                bool(property_obj.covered_area or property_obj.total_area or property_obj.land_area),
                hasattr(property_obj, "location"),
                any(_listing_image_url(listing) for listing in listings),
                bool(listings),
                any(listing.agency_id for listing in listings),
            )
            if check
        )
        / 6
        * 100
    )


def _serialize(property_obj, distance=None, current_query=None):
    location = property_obj.location if hasattr(property_obj, "location") else None
    has_detected_address = bool(property_obj.address or property_obj.detected_address)
    location_state = "mapped" if location else ("detected" if has_detected_address else "unknown")
    precision_label = (
        location.get_precision_display()
        if location
        else ("Direccion detectada" if has_detected_address else "Sin ubicacion")
    )
    listing = _primary_listing(property_obj)
    image = _listing_image_url(listing)
    price_m2 = valid_price_per_m2(property_obj)
    return {
        "id": property_obj.pk,
        "title": property_obj.title,
        "type": property_obj.get_property_type_display(),
        "price": float(property_obj.price) if property_obj.price is not None else None,
        "currency": property_obj.currency,
        "address": property_obj.address,
        "locality": property_obj.locality,
        "neighborhood": property_obj.neighborhood,
        "inferred_neighborhood": property_obj.inferred_neighborhood,
        "zone_conflict": property_obj.zone_conflict,
        "zone_needs_review": property_obj.zone_needs_review,
        "security_coverage_score": property_obj.security_coverage_score,
        "security_risk_score": property_obj.security_risk_score,
        "security_level": property_obj.security_level,
        "security_zone_label": property_obj.security_zone_label,
        "security_source": property_obj.security_source,
        "bedrooms": property_obj.bedrooms,
        "bathrooms": float(property_obj.bathrooms) if property_obj.bathrooms else None,
        "covered_area": float(property_obj.covered_area) if property_obj.covered_area else None,
        "land_area": float(property_obj.land_area) if property_obj.land_area else None,
        "status": property_obj.status,
        "is_favorite": property_obj.is_favorite,
        "is_hidden": property_obj.is_hidden,
        "reviewed": property_obj.reviewed_at is not None,
        "agency_name": listing.agency.name if listing and listing.agency else "",
        "source_name": listing.source.name if listing else "",
        "original_url": listing.url if listing else "",
        "price_m2": float(price_m2) if price_m2 is not None else None,
        "location_confidence": property_obj.location_confidence,
        "location_source": property_obj.location_source,
        "image": image,
        "url": f"/propiedad/{property_obj.pk}/",
        "detail_url": build_detail_url(property_obj.pk, current_query or ""),
        "distance": round(distance, 2) if distance is not None else None,
        "latitude": location.latitude if location else None,
        "longitude": location.longitude if location else None,
        "precision": location.precision if location else "",
        "precision_label": precision_label,
        "location_state": location_state,
        "has_detected_address": has_detected_address,
        "exact": location.is_exact if location else False,
    }


def _serialize_map_property(property_obj, distance=None, current_query=None):
    location = property_obj.location if hasattr(property_obj, "location") else None
    if not location:
        return None
    price_m2 = valid_price_per_m2(property_obj)
    return {
        "id": property_obj.pk,
        "title": property_obj.title,
        "price": float(property_obj.price) if property_obj.price is not None else None,
        "currency": property_obj.currency,
        "status": property_obj.status,
        "locality": property_obj.locality,
        "neighborhood": (
            property_obj.neighborhood
            or property_obj.detected_neighborhood
            or property_obj.inferred_neighborhood
            or ""
        ),
        "zone": (
            property_obj.detected_neighborhood
            or property_obj.neighborhood
            or property_obj.inferred_neighborhood
            or property_obj.locality
            or ""
        ),
        "price_m2": float(price_m2) if price_m2 is not None else None,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "precision": location.precision,
        "precision_label": location.get_precision_display(),
        "exact": location.is_exact,
        "distance": round(distance, 2) if distance is not None else None,
        "detail_url": build_detail_url(property_obj.pk, current_query or ""),
        "url": f"/propiedad/{property_obj.pk}/",
        "is_hidden": property_obj.is_hidden,
        "security_coverage_score": property_obj.security_coverage_score,
        "security_risk_score": property_obj.security_risk_score,
        "security_level": property_obj.security_level,
    }


def _prefetch_property_details(properties):
    ids = [property_obj.pk for property_obj in properties]
    if not ids:
        return []
    detailed = (
        Property.objects.select_related("location")
        .prefetch_related(
            Prefetch(
                "listings",
                queryset=Listing.objects.select_related("agency", "source").prefetch_related(
                    Prefetch("images", queryset=ListingImage.objects.order_by("position"))
                ),
            )
        )
        .filter(pk__in=ids)
    )
    by_id = {property_obj.pk: property_obj for property_obj in detailed}
    return [by_id[property_id] for property_id in ids if property_id in by_id]


def _format_number(value):
    if value is None or value == "":
        return ""
    decimal = Decimal(value)
    if decimal == decimal.to_integral_value():
        return f"{decimal:.0f}"
    return f"{decimal.normalize()}"


def _format_price(value, currency=""):
    formatted = _format_number(value)
    if not formatted:
        return "Consultar"
    return f"{currency} {formatted}".strip()


def _format_area(value):
    formatted = _format_number(value)
    return f"{formatted} m2" if formatted else ""


def _detail_facts(property_obj):
    price_m2 = valid_price_per_m2(property_obj)
    facts = [
        ("Tipo", property_obj.get_property_type_display()),
        ("Operacion", "Venta" if property_obj.operation == "sale" else property_obj.operation.title()),
        ("Estado", property_obj.get_status_display()),
        ("Ambientes", property_obj.rooms),
        ("Dormitorios", property_obj.bedrooms),
        ("Banos", _format_number(property_obj.bathrooms)),
        ("Toilettes", property_obj.toilets),
        ("Cocheras", property_obj.garages),
        ("Cubierta", _format_area(property_obj.covered_area)),
        ("Total", _format_area(property_obj.total_area)),
        ("Terreno", _format_area(property_obj.land_area)),
        ("Libre", _format_area(property_obj.uncovered_area)),
        ("Semicubierta", _format_area(property_obj.semicovered_area)),
        ("Frente", _format_area(property_obj.front_width)),
        ("Fondo", _format_area(property_obj.lot_depth)),
        ("Plantas", property_obj.building_floors),
        ("Antiguedad", f"{property_obj.age_years} anos" if property_obj.age_years else ""),
        ("USD/m2", f"USD {_format_number(price_m2)}" if price_m2 is not None else ""),
        ("Calidad ubicacion", property_obj.get_location_confidence_display()),
    ]
    return [{"label": label, "value": value} for label, value in facts if value not in (None, "")]


def _edit_field(field, label, value, input_type="text", choices=None, rows=0):
    return {
        "field": field,
        "label": label,
        "value": "" if value is None else value,
        "input_type": input_type,
        "choices": choices or [],
        "rows": rows,
    }


def _choice_options(choices):
    return [{"value": value, "label": label} for value, label in choices]


def _property_edit_sections(property_obj):
    return [
        {
            "title": "Identidad",
            "fields": [
                _edit_field("title", "Titulo", property_obj.title),
                _edit_field(
                    "property_type",
                    "Tipo",
                    property_obj.property_type,
                    "select",
                    _choice_options(Property.Type.choices),
                ),
                _edit_field(
                    "operation",
                    "Operacion",
                    property_obj.operation,
                    "select",
                    [{"value": "sale", "label": "Venta"}, {"value": "rent", "label": "Alquiler"}],
                ),
                _edit_field(
                    "status",
                    "Estado",
                    property_obj.status,
                    "select",
                    _choice_options(Property.Status.choices),
                ),
            ],
        },
        {
            "title": "Precio",
            "fields": [
                _edit_field("currency", "Moneda", property_obj.currency),
                _edit_field("price", "Precio", _format_number(property_obj.price), "number"),
            ],
        },
        {
            "title": "Ubicacion",
            "fields": [
                _edit_field("address", "Direccion", property_obj.address),
                _edit_field("locality", "Localidad", property_obj.locality),
                _edit_field("neighborhood", "Zona", property_obj.neighborhood),
            ],
        },
        {
            "title": "Metricas",
            "fields": [
                _edit_field("rooms", "Ambientes", property_obj.rooms, "number"),
                _edit_field("bedrooms", "Dormitorios", property_obj.bedrooms, "number"),
                _edit_field("bathrooms", "Banos", _format_number(property_obj.bathrooms), "number"),
                _edit_field("garages", "Cocheras", property_obj.garages, "number"),
                _edit_field("toilets", "Toilettes", property_obj.toilets, "number"),
                _edit_field("covered_area", "Cubierta m2", _format_number(property_obj.covered_area), "number"),
                _edit_field("total_area", "Total m2", _format_number(property_obj.total_area), "number"),
                _edit_field("land_area", "Terreno m2", _format_number(property_obj.land_area), "number"),
                _edit_field("uncovered_area", "Libre m2", _format_number(property_obj.uncovered_area), "number"),
                _edit_field("semicovered_area", "Semicubierta m2", _format_number(property_obj.semicovered_area), "number"),
                _edit_field("front_width", "Frente m", _format_number(property_obj.front_width), "number"),
                _edit_field("lot_depth", "Fondo m", _format_number(property_obj.lot_depth), "number"),
                _edit_field("building_floors", "Plantas", property_obj.building_floors, "number"),
                _edit_field("age_years", "Antiguedad", property_obj.age_years, "number"),
            ],
        },
        {
            "title": "Texto",
            "fields": [
                _edit_field("description", "Descripcion", property_obj.description, "textarea", rows=5),
                _edit_field("features", "Caracteristicas", "\n".join(property_obj.features or []), "textarea", rows=4),
            ],
        },
    ]


def _listing_domain(url):
    host = urlparse(url or "").netloc
    return host[4:] if host.startswith("www.") else host


def _source_links(property_obj):
    links = []
    for listing in property_obj.listings.all():
        label = listing.source.name
        if listing.agency:
            label = f"{label} - {listing.agency.name}"
        links.append(
            {
                "listing": listing,
                "label": label,
                "domain": _listing_domain(listing.url),
            }
        )
    return links


def _source_link_payload(link):
    listing = link["listing"]
    return {
        "label": link["label"],
        "domain": link["domain"],
        "url": listing.url,
        "source": listing.source.name if listing.source else "",
        "agency": listing.agency.name if listing.agency else "",
        "active": listing.active,
    }


@ensure_csrf_cookie
@require_GET
def property_summary_api(request, pk):
    property_obj = get_object_or_404(
        Property.objects.select_related("location").prefetch_related(
            Prefetch(
                "listings",
                queryset=Listing.objects.select_related("agency", "source").prefetch_related(
                    Prefetch("images", queryset=ListingImage.objects.order_by("position")),
                    "snapshots",
                ),
            )
        ),
        pk=pk,
    )
    listing = _primary_listing(property_obj)
    source_links = [_source_link_payload(link) for link in _source_links(property_obj)]
    location = getattr(property_obj, "location", None)
    price_m2 = valid_price_per_m2(property_obj)
    return JsonResponse(
        {
            "id": property_obj.pk,
            "title": property_obj.title,
            "price": str(property_obj.price) if property_obj.price is not None else "",
            "price_display": _format_price(property_obj.price, property_obj.currency),
            "currency": property_obj.currency,
            "price_m2": float(price_m2) if price_m2 is not None else None,
            "address": property_obj.address or property_obj.detected_address or "",
            "locality": property_obj.locality or property_obj.detected_locality or "",
            "neighborhood": (
                property_obj.neighborhood
                or property_obj.detected_neighborhood
                or property_obj.inferred_neighborhood
                or ""
            ),
            "description": property_obj.description,
            "image": _listing_image_url(listing),
            "facts": _detail_facts(property_obj),
            "source_links": source_links,
            "primary_listing": source_links[0] if source_links else None,
            "detail_url": build_detail_url(property_obj.pk, request.GET, reverse("properties:search")),
            "original_url": listing.url if listing else "",
            "is_favorite": property_obj.is_favorite,
            "is_hidden": property_obj.is_hidden,
            "reviewed": property_obj.reviewed_at is not None,
            "personal_notes": property_obj.personal_notes,
            "security": {
                "coverage_score": property_obj.security_coverage_score,
                "risk_score": property_obj.security_risk_score,
                "level": property_obj.security_level,
                "zone_label": property_obj.security_zone_label,
                "source": property_obj.security_source,
                "evidence": property_obj.security_evidence or {},
                "scored_at": property_obj.security_scored_at.isoformat()
                if property_obj.security_scored_at
                else "",
            },
            "edit_sections": _property_edit_sections(property_obj),
            "edit_payload": _serialize_property_edit(property_obj),
            "location": {
                "latitude": location.latitude if location else None,
                "longitude": location.longitude if location else None,
                "precision": location.precision if location else "",
                "confidence": location.confidence if location else None,
            },
            "map_config": map_config_payload(),
        }
    )


def _price_history_segments(property_obj):
    snapshots = []
    for listing in property_obj.listings.all():
        for snapshot in listing.snapshots.all():
            snapshots.append(snapshot)
    snapshots.sort(key=lambda item: (item.observed_at, item.pk))
    segments = []
    for snapshot in snapshots:
        key = (snapshot.currency or "", snapshot.price)
        if segments and segments[-1]["key"] == key:
            segments[-1]["last_seen"] = snapshot.observed_at
            segments[-1]["count"] += 1
            continue
        segments.append(
            {
                "key": key,
                "first_seen": snapshot.observed_at,
                "last_seen": snapshot.observed_at,
                "same_day": True,
                "count": 1,
                "currency": snapshot.currency or "",
                "price": snapshot.price,
            }
        )
        continue
    for segment in segments:
        segment["same_day"] = segment["first_seen"].date() == segment["last_seen"].date()
    return list(reversed(segments))


def _detail_navigation(property_obj, return_to):
    previous_url = next_url = ""
    parsed = urlparse(return_to)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    properties = []
    if parsed.path == reverse("properties:search"):
        properties, _ = filtered_properties(params)
    if not properties:
        properties, _ = filtered_properties({})
        params = {}
    ids = [item.pk for item in properties]
    if property_obj.pk in ids:
        index = ids.index(property_obj.pk)
        query = urlencode(params)
        if index > 0:
            previous_url = build_detail_url(ids[index - 1], query)
        if index < len(ids) - 1:
            next_url = build_detail_url(ids[index + 1], query)
    if not previous_url:
        previous_property = (
            Property.objects.filter(operation="sale", is_hidden=False, pk__lt=property_obj.pk)
            .order_by("-pk")
            .first()
        )
        if previous_property:
            previous_url = build_detail_url(previous_property.pk, "")
    if not next_url:
        next_property = (
            Property.objects.filter(operation="sale", is_hidden=False, pk__gt=property_obj.pk)
            .order_by("pk")
            .first()
        )
        if next_property:
            next_url = build_detail_url(next_property.pk, "")
    return previous_url, next_url


@ensure_csrf_cookie
def search(request):
    properties, distances = filtered_properties(request.GET, include_listings=False)
    paginator = Paginator(properties, 24)
    page = paginator.get_page(request.GET.get("page"))
    page.object_list = _prefetch_property_details(page.object_list)
    serialized = {
        item.pk: _serialize(item, distances.get(item.pk), request.GET)
        for item in page.object_list
    }
    context = {
        "page": page,
        "serialized": serialized,
        "total": len(properties),
        "agencies": Agency.objects.filter(listings__isnull=False).distinct().order_by("name"),
        "sources": Source.objects.order_by("name"),
        "property_types": Property.Type.choices,
        "statuses": Property.Status.choices,
        "location_confidences": Property.LocationConfidence.choices,
        "localities": ["Hurlingham", "Villa Tesei", "William C. Morris"],
        "features": ["Pileta", "Quincho", "Jardín", "Parrilla", "Apto crédito"],
        "query_params": request.GET,
        "view_mode": request.GET.get("view") or "cards",
        "pagination_urls": {
            "first": query_url(request.GET, {"page": 1}),
            "last": query_url(request.GET, {"page": page.paginator.num_pages}),
            "previous": query_url(request.GET, {"page": page.previous_page_number()}) if page.has_previous() else "",
            "next": query_url(request.GET, {"page": page.next_page_number()}) if page.has_next() else "",
        },
        "pagination_hidden_params": _query_param_pairs(request.GET, exclude=["page"]),
    }
    context.update(filter_context(request.GET))
    context.update(table_context(request.GET))
    return render(request, "properties/search.html", context)


@ensure_csrf_cookie
def detail(request, pk):
    property_obj = get_object_or_404(
        Property.objects.select_related("location").prefetch_related(
            "listings__images",
            "listings__agency",
            "listings__source",
            "listings__snapshots",
            "location_history",
        ),
        pk=pk,
    )
    location = getattr(property_obj, "location", None)
    return_to = safe_return_to(request)
    parsed = urlparse(return_to)
    return_label = "Estadisticas" if parsed.path == reverse("properties:stats") else "Resultados"
    previous_url, next_url = _detail_navigation(property_obj, return_to)
    location_payload = {
        "id": property_obj.pk,
        "latitude": location.latitude if location else None,
        "longitude": location.longitude if location else None,
        "precision": location.precision if location else "",
        "has_location": bool(location),
    }
    source_links = _source_links(property_obj)
    return render(
        request,
        "properties/detail.html",
        {
            "property": property_obj,
            "primary_listing": _primary_listing(property_obj),
            "source_links": source_links,
            "primary_source_link": source_links[0] if source_links else None,
            "detail_facts": _detail_facts(property_obj),
            "edit_sections": _property_edit_sections(property_obj),
            "property_edit_payload": _serialize_property_edit(property_obj),
            "price_history": _price_history_segments(property_obj),
            "map_config": map_config_payload(),
            "property_location": location_payload,
            "return_to": return_to,
            "return_label": return_label,
            "previous_url": previous_url,
            "next_url": next_url,
        },
    )


def properties_geojson(request):
    properties, distances = filtered_properties(request.GET, include_listings=False)
    features = []
    for property_obj in properties:
        item = _serialize_map_property(property_obj, distances.get(property_obj.pk), request.GET)
        if not item:
            continue
        features.append(
            {
                "type": "Feature",
                "id": property_obj.pk,
                "geometry": {
                    "type": "Point",
                    "coordinates": [item["longitude"], item["latitude"]],
                },
                "properties": item,
            }
        )
    return JsonResponse(
        {"type": "FeatureCollection", "features": features, "count": len(properties)}
    )


@require_POST
def update_property_state(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON invalido."}, status=400)
    updates = {}
    if "is_favorite" in payload:
        updates["is_favorite"] = bool(payload["is_favorite"])
    if "is_hidden" in payload:
        updates["is_hidden"] = bool(payload["is_hidden"])
    if "reviewed" in payload:
        updates["reviewed_at"] = timezone.now() if payload["reviewed"] else None
    if not updates:
        return JsonResponse({"error": "No hay cambios para guardar."}, status=400)
    for field, value in updates.items():
        setattr(property_obj, field, value)
    property_obj.save(update_fields=list(updates))
    return JsonResponse(
        {
            "ok": True,
            "is_favorite": property_obj.is_favorite,
            "is_hidden": property_obj.is_hidden,
            "reviewed": property_obj.reviewed_at is not None,
        }
    )


@require_POST
def update_property_note(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON invalido."}, status=400)
    property_obj.personal_notes = str(payload.get("personal_notes") or "")[:5000]
    property_obj.save(update_fields=["personal_notes"])
    return JsonResponse({"ok": True, "personal_notes": property_obj.personal_notes})


@require_POST
def update_property_data(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON invalido."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Payload invalido."}, status=400)

    unknown_fields = sorted(set(payload) - EDITABLE_PROPERTY_FIELDS)
    if unknown_fields:
        return JsonResponse(
            {"error": f"Campos no editables: {', '.join(unknown_fields)}."},
            status=400,
        )

    updates = {}
    errors = {}
    for field, raw_value in payload.items():
        if field in CHOICE_EDIT_FIELDS and raw_value not in CHOICE_EDIT_FIELDS[field]:
            errors[field] = "Opcion invalida."
            continue
        try:
            value = _coerce_edit_value(field, raw_value)
        except ValueError as exc:
            errors[field] = str(exc)
            continue
        if field == "title" and not value:
            errors[field] = "El titulo no puede estar vacio."
            continue
        if getattr(property_obj, field) != value:
            updates[field] = value

    if errors:
        return JsonResponse({"error": "Hay campos invalidos.", "fields": errors}, status=400)
    if not updates:
        return JsonResponse({"ok": True, "changed": [], "property": _serialize_property_edit(property_obj)})

    old_address = property_obj.address or property_obj.detected_address or ""
    now = timezone.now()
    overrides = dict(property_obj.manual_overrides or {})
    for field, value in updates.items():
        setattr(property_obj, field, value)
        overrides[field] = now.isoformat()

    update_fields = set(updates)
    if "address" in updates:
        property_obj.normalized_address = normalize_address(property_obj.address)
        update_fields.add("normalized_address")
    property_obj.manual_overrides = overrides
    property_obj.data_manually_corrected_at = now
    update_fields.update({"manual_overrides", "data_manually_corrected_at"})
    property_obj.save(update_fields=sorted(update_fields))

    if "address" in updates:
        location = getattr(property_obj, "location", None)
        new_address = property_obj.address or property_obj.detected_address or ""
        if location and not location.manually_corrected and not same_geocoding_target(old_address, new_address):
            location.delete()

    return JsonResponse(
        {
            "ok": True,
            "changed": sorted(updates),
            "property": _serialize_property_edit(property_obj),
        }
    )


@require_POST
def update_location(request, pk):
    property_obj = get_object_or_404(Property, pk=pk)
    try:
        payload = json.loads(request.body)
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return JsonResponse({"error": "Coordenadas inválidas."}, status=400)
    bounds = settings.HURLINGHAM_BOUNDS
    outside = not (
        bounds["south"] <= latitude <= bounds["north"]
        and bounds["west"] <= longitude <= bounds["east"]
    )
    location, _ = PropertyLocation.objects.update_or_create(
        property=property_obj,
        defaults={
            "latitude": latitude,
            "longitude": longitude,
            "precision": PropertyLocation.Precision.MANUAL,
            "query": "Corrección manual",
            "provider": "manual",
            "confidence": 1,
            "manually_corrected": True,
            "outside_target": outside,
        },
    )
    return JsonResponse(
        {
            "ok": True,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "precision": location.precision,
            "outside_target": outside,
        }
    )


def map_config_payload():
    return {
        "tile_url": settings.MAP_TILE_URL,
        "attribution": settings.MAP_ATTRIBUTION,
        "bounds": settings.HURLINGHAM_BOUNDS,
        "center": [-58.641, -34.606],
        "zoom": 12,
    }


def map_config(request):
    return JsonResponse(map_config_payload())


@require_GET
def security_layers_api(request):
    return JsonResponse(security_layers_payload())


EXPORT_COLUMNS = (
    "id",
    "titulo",
    "tipo",
    "precio",
    "moneda",
    "precio_m2",
    "direccion",
    "localidad",
    "barrio",
    "localidad_detectada",
    "barrio_detectado",
    "barrio_inferido",
    "conflicto_zona",
    "zona_requiere_revision",
    "direccion_detectada",
    "fuente_localizacion",
    "confianza_localizacion",
    "dormitorios",
    "banos",
    "cubierta_m2",
    "terreno_m2",
    "inmobiliaria",
    "fuente",
    "link_original",
    "favorita",
    "oculta",
    "revisada",
    "notas",
)


def _export_rows(properties):
    for property_obj in properties:
        listing = _primary_listing(property_obj)
        yield {
            "id": property_obj.pk,
            "titulo": property_obj.title,
            "tipo": property_obj.get_property_type_display(),
            "precio": valid_price(property_obj),
            "moneda": property_obj.currency,
            "precio_m2": valid_price_per_m2(property_obj),
            "direccion": property_obj.address,
            "localidad": property_obj.locality,
            "barrio": property_obj.neighborhood,
            "localidad_detectada": property_obj.detected_locality,
            "barrio_detectado": property_obj.detected_neighborhood,
            "barrio_inferido": property_obj.inferred_neighborhood,
            "conflicto_zona": "Si" if property_obj.zone_conflict else "No",
            "zona_requiere_revision": "Si" if property_obj.zone_needs_review else "No",
            "direccion_detectada": property_obj.detected_address,
            "fuente_localizacion": property_obj.get_location_source_display(),
            "confianza_localizacion": property_obj.get_location_confidence_display(),
            "dormitorios": valid_value(property_obj, "bedrooms"),
            "banos": valid_value(property_obj, "bathrooms"),
            "cubierta_m2": valid_value(property_obj, "covered_area"),
            "terreno_m2": valid_value(property_obj, "land_area"),
            "inmobiliaria": listing.agency.name if listing and listing.agency else "",
            "fuente": listing.source.name if listing else "",
            "link_original": listing.url if listing else "",
            "favorita": "Si" if property_obj.is_favorite else "No",
            "oculta": "Si" if property_obj.is_hidden else "No",
            "revisada": "Si" if property_obj.reviewed_at else "No",
            "notas": property_obj.personal_notes,
        }


def export_properties_csv(request):
    properties, _ = filtered_properties(request.GET, include_listings="summary")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="propiedades.csv"'
    response.write("\ufeff")
    writer = csv.DictWriter(response, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(_export_rows(properties))
    return response


def export_properties_xlsx(request):
    try:
        from openpyxl import Workbook
    except ImportError:
        return JsonResponse({"error": "openpyxl no esta instalado."}, status=500)
    properties, _ = filtered_properties(request.GET, include_listings="summary")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Propiedades"
    sheet.append(EXPORT_COLUMNS)
    for row in _export_rows(properties):
        sheet.append([row[column] for column in EXPORT_COLUMNS])
    for column_cells in sheet.columns:
        width = min(max(len(str(cell.value or "")) for cell in column_cells) + 2, 48)
        sheet.column_dimensions[column_cells[0].column_letter].width = width
    output = io.BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="propiedades.xlsx"'
    return response


def _numbers(values):
    return [float(value) for value in values if value not in (None, "")]


def _summary(values):
    numbers = _numbers(values)
    if not numbers:
        return {"avg": None, "median": None, "min": None, "max": None, "std": None}
    return {
        "avg": round(statistics.mean(numbers), 2),
        "median": round(statistics.median(numbers), 2),
        "min": round(min(numbers), 2),
        "max": round(max(numbers), 2),
        "std": round(statistics.pstdev(numbers), 2) if len(numbers) > 1 else 0,
    }


def _safe_label(value):
    return value or "Sin dato"


def _zone_price_statistics(properties, request_query, stats_path):
    grouped = {}
    for property_obj in properties:
        price = valid_price(property_obj)
        if price is None:
            continue
        label = _safe_label(
            property_obj.detected_neighborhood
            or property_obj.neighborhood
            or property_obj.inferred_neighborhood
        )
        grouped.setdefault(label, []).append(float(price))
    items = []
    for label, values in grouped.items():
        values.sort()
        average = statistics.mean(values)
        std = statistics.pstdev(values) if len(values) > 1 else 0
        q1 = _percentile(values, 0.25)
        q3 = _percentile(values, 0.75)
        items.append(
            {
                "label": label,
                "value": len(values),
                "total": len(values),
                "avg": round(average, 2),
                "std": round(std, 2),
                "median": round(statistics.median(values), 2),
                "cv": round((std / average) * 100, 1) if average else 0,
                "min": round(values[0], 2),
                "max": round(values[-1], 2),
                "q1": round(q1, 2) if q1 is not None else None,
                "q3": round(q3, 2) if q3 is not None else None,
                "url": query_url(request_query, {"neighborhood": "" if label == "Sin dato" else label}, path=stats_path),
            }
        )
    items.sort(key=lambda item: item["total"], reverse=True)
    return items[:20]


def _heatmap_points(properties, request_query, max_points=1200):
    points = []
    for property_obj in properties:
        if len(points) >= max_points:
            break
        location = getattr(property_obj, "location", None)
        if not location:
            continue
        price = valid_price(property_obj)
        if price is None:
            continue
        listing = _primary_listing(property_obj)
        area = property_obj.covered_area or property_obj.total_area or property_obj.land_area
        price_m2 = float(price / area) if area else None
        points.append(
            {
                "id": property_obj.pk,
                "title": property_obj.title,
                "price": float(price),
                "price_m2": price_m2,
                "area": float(area) if area else None,
                "currency": property_obj.currency or "",
                "zone": _safe_label(
                    property_obj.detected_neighborhood
                    or property_obj.neighborhood
                    or property_obj.inferred_neighborhood
                ),
                "longitude": location.longitude,
                "latitude": location.latitude,
                "url": build_detail_url(property_obj.pk, request_query, reverse("properties:search")),
                "image": _listing_image_url(listing),
                "is_hidden": property_obj.is_hidden,
                "is_favorite": property_obj.is_favorite,
                "is_reviewed": property_obj.reviewed_at is not None,
            }
        )
    return points


def _counter(items, label_getter):
    counts = {}
    for item in items:
        label = label_getter(item) or "Sin dato"
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _series(items, label_getter, url_getter):
    counts = {}
    urls = {}
    favorites = {}
    reviewed = {}
    pending = {}
    for item in items:
        label = label_getter(item) or "Sin dato"
        property_obj = getattr(item, "property", item)
        counts[label] = counts.get(label, 0) + 1
        urls.setdefault(label, url_getter(item, label))
        favorites.setdefault(label, 0)
        reviewed.setdefault(label, 0)
        pending.setdefault(label, 0)
        if getattr(property_obj, "is_favorite", False):
            favorites[label] += 1
        elif getattr(property_obj, "reviewed_at", None):
            reviewed[label] += 1
        else:
            pending[label] += 1
    return [
        {
            "label": label,
            "value": count,
            "total": count,
            "favorites": favorites.get(label, 0),
            "reviewed": reviewed.get(label, 0),
            "pending": pending.get(label, 0),
            "url": urls.get(label, "/"),
        }
        for label, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _chart_property_payload(property_obj, request_query, stats_path):
    listing = _primary_listing(property_obj)
    image = _listing_image_url(listing)
    price_m2 = valid_price_per_m2(property_obj)
    return {
        "id": property_obj.pk,
        "title": property_obj.title,
        "price": float(property_obj.price) if property_obj.price is not None else None,
        "currency": property_obj.currency,
        "address": property_obj.address or property_obj.detected_address or property_obj.locality or "",
        "zone": _safe_label(
            property_obj.detected_neighborhood
            or property_obj.neighborhood
            or property_obj.inferred_neighborhood
        ),
        "property_type": property_obj.property_type,
        "property_type_label": property_obj.get_property_type_display(),
        "price_m2": float(price_m2) if price_m2 is not None else None,
        "quality_score": _quality_score(property_obj),
        "location_confidence": property_obj.location_confidence,
        "security_coverage_score": property_obj.security_coverage_score,
        "security_risk_score": property_obj.security_risk_score,
        "security_level": property_obj.security_level,
        "security_zone_label": property_obj.security_zone_label,
        "security_source": property_obj.security_source,
        "is_hidden": property_obj.is_hidden,
        "agency": listing.agency.name if listing and listing.agency else "",
        "source": listing.source.name if listing and listing.source else "",
        "image": image,
        "url": build_detail_url(property_obj.pk, request_query, stats_path),
        "is_favorite": property_obj.is_favorite,
        "is_reviewed": property_obj.reviewed_at is not None,
    }


def _zone_type_matrix(properties, request_query, stats_path):
    grouped = {}
    type_labels = dict(Property.Type.choices)
    for property_obj in properties:
        price = valid_price(property_obj)
        price_m2 = valid_price_per_m2(property_obj)
        if price is None and price_m2 is None:
            continue
        zone = _safe_label(
            property_obj.detected_neighborhood
            or property_obj.neighborhood
            or property_obj.inferred_neighborhood
        )
        key = (zone, property_obj.property_type)
        grouped.setdefault(key, {"prices": [], "prices_m2": [], "count": 0})
        grouped[key]["count"] += 1
        if price is not None:
            grouped[key]["prices"].append(float(price))
        if price_m2 is not None:
            grouped[key]["prices_m2"].append(float(price_m2))
    rows = []
    for (zone, property_type), values in grouped.items():
        price_summary = _summary(values["prices"])
        price_m2_summary = _summary(values["prices_m2"])
        rows.append(
            {
                "zone": zone,
                "property_type": property_type,
                "property_type_label": type_labels.get(property_type, property_type or "Sin dato"),
                "count": values["count"],
                "avg_price": price_summary["avg"],
                "median_price": price_summary["median"],
                "std_price": price_summary["std"],
                "avg_price_m2": price_m2_summary["avg"],
                "median_price_m2": price_m2_summary["median"],
                "std_price_m2": price_m2_summary["std"],
                "url": query_url(
                    request_query,
                    {
                        "neighborhood": "" if zone == "Sin dato" else zone,
                        "property_type": property_type,
                    },
                    path=stats_path,
                ),
            }
        )
    rows.sort(key=lambda item: (-item["count"], item["zone"], item["property_type_label"]))
    return rows[:80]


def _liquidity_buckets(properties):
    now = timezone.now()
    buckets = [
        ("0-15 dias", lambda age: age <= 15),
        ("16-45 dias", lambda age: 15 < age <= 45),
        ("46-90 dias", lambda age: 45 < age <= 90),
        ("+90 dias", lambda age: age > 90),
    ]
    rows = [
        {
            "label": label,
            "value": 0,
            "avg_price": None,
            "new": 0,
            "persistent": 0,
            "stale": 0,
            "_prices": [],
        }
        for label, _predicate in buckets
    ]
    for property_obj in properties:
        age_days = (now - property_obj.first_seen_at).days if property_obj.first_seen_at else 0
        last_seen_days = (now - property_obj.last_seen_at).days if property_obj.last_seen_at else 0
        row = next((item for item, (_label, predicate) in zip(rows, buckets) if predicate(age_days)), rows[-1])
        row["value"] += 1
        row["new"] += 1 if age_days <= 15 else 0
        row["persistent"] += 1 if age_days >= 90 else 0
        row["stale"] += 1 if last_seen_days >= 30 else 0
        price = valid_price(property_obj)
        if price is not None:
            row["_prices"].append(float(price))
    for row in rows:
        summary = _summary(row.pop("_prices"))
        row["avg_price"] = summary["avg"]
    return rows


def _load_security_features():
    path = Path(settings.BASE_DIR) / "data" / "seguridad_hurlingham.geojson"
    if not path.exists():
        return {"path": str(path), "features": [], "configured": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(path), "features": [], "configured": False}
    features = payload.get("features") or []
    return {"path": str(path), "features": features, "configured": bool(features)}


def _security_match(location, features):
    if not location:
        return {"score": None, "source": "sin dato", "label": ""}
    matches = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        score = _float(props.get("score"))
        source = props.get("source") or "manual"
        label = props.get("label") or props.get("name") or "Zona de seguridad"
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates") or []
        matched = False
        if geometry_type == "Polygon" and coordinates:
            matched = point_in_polygon(location.latitude, location.longitude, coordinates[0])
        elif geometry_type == "MultiPolygon":
            matched = any(
                polygon and point_in_polygon(location.latitude, location.longitude, polygon[0])
                for polygon in coordinates
            )
        elif geometry_type == "Point" and len(coordinates) >= 2:
            radius_m = _float(props.get("radius_m")) or 250
            distance_m = haversine_km(location.latitude, location.longitude, coordinates[1], coordinates[0]) * 1000
            matched = distance_m <= radius_m
        if matched:
            matches.append({"score": score, "source": source, "label": label})
    if not matches:
        return {"score": None, "source": "sin dato", "label": ""}
    matches.sort(key=lambda item: -1 if item["score"] is None else item["score"])
    return matches[-1]


def _security_price_summary(properties, request_query, stats_path):
    zone_path = Path(settings.BASE_DIR) / "data" / "seguridad_hurlingham.geojson"
    points_path = Path(settings.BASE_DIR) / "data" / "geo" / "security_points_hurlingham.geojson"
    located = [item for item in properties if hasattr(item, "location")]
    rows = []
    for property_obj in properties:
        location = getattr(property_obj, "location", None)
        if not location:
            continue
        price = valid_price(property_obj)
        price_m2 = valid_price_per_m2(property_obj)
        if (
            price is None
            and price_m2 is None
            and property_obj.security_coverage_score is None
        ):
            continue
        rows.append(
            {
                **_chart_property_payload(property_obj, request_query, stats_path),
                "latitude": location.latitude,
                "longitude": location.longitude,
                "security_score": property_obj.security_coverage_score,
                "security_source": property_obj.security_source,
                "security_label": property_obj.security_zone_label,
            }
        )
    scored = [item for item in rows if item["security_score"] is not None]
    risk_price = [
        {
            **item,
            "x": item["security_risk_score"],
            "y": item["price_m2"],
        }
        for item in scored
        if item.get("security_risk_score") is not None and item.get("price_m2") is not None
    ][:500]
    return {
        "configured": zone_path.exists(),
        "path": str(zone_path),
        "points_path": str(points_path),
        "total_with_location": len(located),
        "scored_count": len(scored),
        "rows": sorted(
            scored,
            key=lambda item: (
                item.get("security_risk_score") or 0,
                -(item.get("price_m2") or 0),
            ),
            reverse=True,
        )[:250],
        "risk_price": risk_price,
        "arbitrage": _security_arbitrage(properties, request_query, stats_path),
    }


def _security_arbitrage(properties, request_query, stats_path):
    values = [
        float(valid_price_per_m2(item))
        for item in properties
        if valid_price_per_m2(item) is not None and item.security_coverage_score is not None
    ]
    if not values:
        return []
    median_m2 = statistics.median(values)
    rows = []
    for property_obj in properties:
        price_m2 = valid_price_per_m2(property_obj)
        coverage = property_obj.security_coverage_score
        risk = property_obj.security_risk_score
        if price_m2 is None or coverage is None or risk is None:
            continue
        price_m2_float = float(price_m2)
        if coverage >= 60 and price_m2_float <= median_m2:
            kind = "Oportunidad segura"
            priority = 4
        elif risk >= 55 and price_m2_float <= median_m2:
            kind = "Negociable por riesgo"
            priority = 3
        elif risk >= 55 and price_m2_float > median_m2:
            kind = "Sobreprecio riesgoso"
            priority = 2
        elif coverage >= 60 and price_m2_float > median_m2 * 1.15:
            kind = "Prima de seguridad"
            priority = 1
        else:
            continue
        rows.append(
            {
                **_chart_property_payload(property_obj, request_query, stats_path),
                "kind": kind,
                "priority": priority,
                "median_price_m2": round(median_m2),
                "coverage_score": coverage,
                "risk_score": risk,
            }
        )
    rows.sort(
        key=lambda item: (
            item["priority"],
            item["coverage_score"] if item["kind"] == "Oportunidad segura" else item["risk_score"],
            -(item.get("price_m2") or 0),
        ),
        reverse=True,
    )
    return rows[:40]


def _stats_cache_key(request):
    state = Property.objects.aggregate(
        total=Count("pk"),
        latest_seen=Max("last_seen_at"),
        latest_reviewed=Max("reviewed_at"),
        latest_security=Max("security_scored_at"),
    )
    favorite_count = Property.objects.filter(is_favorite=True).count()
    hidden_count = Property.objects.filter(is_hidden=True).count()
    raw = "|".join(
        [
            request.META.get("QUERY_STRING", ""),
            str(state.get("total") or 0),
            str(state.get("latest_seen") or ""),
            str(state.get("latest_reviewed") or ""),
            str(state.get("latest_security") or ""),
            str(favorite_count),
            str(hidden_count),
        ]
    )
    return "stats:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


ANOMALY_MODEL_OPTIONS = (
    {"key": "rules", "label": "Reglas de calidad"},
    {"key": "iqr_mad", "label": "IQR/MAD por zona/tipo"},
    {"key": "regression", "label": "Regresion superficie-precio"},
    {"key": "source_conflict", "label": "Conflictos de fuente/scraper"},
)


def _anomaly_model_for_category(category):
    if category == "source_conflict":
        return ANOMALY_MODEL_OPTIONS[3]
    return ANOMALY_MODEL_OPTIONS[0]


def _anomaly_severity(value=None, ratio=None, default=60):
    if ratio is not None:
        return max(1, min(100, round(float(ratio) * 100)))
    if value in (None, ""):
        return default
    try:
        parsed = abs(float(value))
    except (TypeError, ValueError):
        return default
    return max(1, min(100, round(parsed)))


def _anomaly_model_summary(rows):
    by_model = {
        option["key"]: {
            "key": option["key"],
            "label": option["label"],
            "count": 0,
            "severity_total": 0,
            "avg_severity": 0,
            "first_property_id": "",
        }
        for option in ANOMALY_MODEL_OPTIONS
    }
    for row in rows:
        key = row.get("model_key") or "rules"
        if key not in by_model:
            continue
        summary = by_model[key]
        summary["count"] += 1
        summary["severity_total"] += row.get("severity") or 0
        if not summary["first_property_id"]:
            summary["first_property_id"] = row["property"].pk
    for summary in by_model.values():
        if summary["count"]:
            summary["avg_severity"] = round(summary["severity_total"] / summary["count"])
        summary.pop("severity_total", None)
    return list(by_model.values())


def _percentile(sorted_values, percentile):
    if not sorted_values:
        return None
    index = (len(sorted_values) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _advanced_anomaly_rows(properties, request_query, stats_path):
    rows = []
    grouped = {}
    for property_obj in properties:
        price_m2 = valid_price_per_m2(property_obj)
        if price_m2 is None:
            continue
        zone = (
            property_obj.detected_neighborhood
            or property_obj.neighborhood
            or property_obj.inferred_neighborhood
            or property_obj.locality
            or "Sin zona"
        )
        key = (zone, property_obj.property_type or "")
        grouped.setdefault(key, []).append((property_obj, float(price_m2)))

    for (zone, property_type), items in grouped.items():
        if len(items) < 7:
            continue
        values = sorted(value for _property, value in items)
        q1 = _percentile(values, 0.25)
        q3 = _percentile(values, 0.75)
        median = statistics.median(values)
        mad_values = [abs(value - median) for value in values]
        mad = statistics.median(mad_values) if mad_values else 0
        iqr = (q3 or 0) - (q1 or 0)
        lower = (q1 or 0) - 1.5 * iqr
        upper = (q3 or 0) + 1.5 * iqr
        for property_obj, value in items:
            robust_z = 0 if not mad else 0.6745 * (value - median) / mad
            if value < lower or value > upper or abs(robust_z) >= 3.5:
                distance = min(abs(value - lower), abs(value - upper)) if value < lower or value > upper else abs(robust_z)
                severity = _anomaly_severity(ratio=(abs(robust_z) / 5 if robust_z else distance / max(1, median)))
                rows.append(
                    {
                        "property": property_obj,
                        "listing": _primary_listing(property_obj),
                        "field": "modelo IQR/MAD",
                        "value": round(value),
                        "reason": f"USD/m2 atipico en {zone} ({property_type or 'tipo sin dato'})",
                        "model_key": "iqr_mad",
                        "model_label": "IQR/MAD por zona/tipo",
                        "severity": severity,
                        "detail_url": build_detail_url(property_obj.pk, request_query, stats_path),
                    }
                )

    points = [
        (property_obj, float(valid_area(property_obj)), float(valid_price(property_obj)))
        for property_obj in properties
        if valid_area(property_obj) is not None and valid_price(property_obj) is not None
    ]
    if len(points) >= 8:
        sum_x = sum(point[1] for point in points)
        sum_y = sum(point[2] for point in points)
        sum_xy = sum(point[1] * point[2] for point in points)
        sum_xx = sum(point[1] * point[1] for point in points)
        count = len(points)
        denominator = count * sum_xx - sum_x * sum_x
        if denominator:
            slope = (count * sum_xy - sum_x * sum_y) / denominator
            intercept = (sum_y - slope * sum_x) / count
            residuals = [price - (slope * area + intercept) for _property, area, price in points]
            std = statistics.stdev(residuals) if len(residuals) > 1 else 0
            if std:
                for (property_obj, area, price), residual in zip(points, residuals):
                    if abs(residual) >= std * 1.8:
                        direction = "por debajo" if residual < 0 else "por encima"
                        severity = _anomaly_severity(ratio=abs(residual) / max(1, std * 3))
                        rows.append(
                            {
                                "property": property_obj,
                                "listing": _primary_listing(property_obj),
                                "field": "regresion superficie-precio",
                                "value": round(residual),
                                "reason": f"precio {direction} de la banda esperada",
                                "model_key": "regression",
                                "model_label": "Regresion superficie-precio",
                                "severity": severity,
                                "detail_url": build_detail_url(property_obj.pk, request_query, stats_path),
                            }
                        )

    deduped = {}
    for row in rows:
        key = (row["property"].pk, row["field"], row["reason"])
        deduped[key] = row
    return list(deduped.values())[:80]


@ensure_csrf_cookie
def market_stats(request):
    cache_key = _stats_cache_key(request)
    cached_context = cache.get(cache_key)
    if cached_context:
        context = cached_context.copy()
        context.update(filter_context(request.GET))
        return render(request, "properties/stats.html", context)

    properties, _ = filtered_properties(request.GET)
    stats_path = reverse("properties:stats")
    latest_job = ScrapeJob.objects.filter(finished_at__isnull=False).order_by("-finished_at").first()
    latest_started = latest_job.started_at if latest_job else None
    listings = [listing for property_obj in properties for listing in _listings(property_obj)]
    duplicate_keys = {}
    for property_obj in properties:
        key = property_obj.normalized_address or property_obj.detected_address.lower()
        if key:
            duplicate_keys.setdefault(key, 0)
            duplicate_keys[key] += 1
    possible_duplicates = sum(count for count in duplicate_keys.values() if count > 1)
    total = len(properties)
    anomaly_rows = []
    for item in properties:
        listing = _primary_listing(item)
        for anomaly in property_anomalies(item):
            model = _anomaly_model_for_category(anomaly.category)
            anomaly_rows.append(
                {
                    "property": item,
                    "listing": listing,
                    "field": anomaly.field,
                    "value": anomaly.value,
                    "reason": anomaly.reason,
                    "model_key": model["key"],
                    "model_label": model["label"],
                    "severity": _anomaly_severity(default=70 if anomaly.category in {"price", "range"} else 55),
                    "detail_url": build_detail_url(item.pk, request.GET, stats_path),
                }
            )
    anomaly_rows.extend(_advanced_anomaly_rows(properties, request.GET, stats_path))
    anomaly_model_summary = _anomaly_model_summary(anomaly_rows)
    quality = {
        "price": sum(1 for item in properties if valid_price(item) is not None),
        "surface": sum(1 for item in properties if valid_area(item) is not None),
        "address": sum(1 for item in properties if item.detected_address or item.address),
        "location": sum(1 for item in properties if hasattr(item, "location")),
        "image": sum(1 for item in properties if any(_listing_image_url(listing) for listing in _listings(item))),
        "link": sum(1 for item in properties if _listings(item)),
        "agency": sum(1 for item in properties if any(listing.agency_id for listing in _listings(item))),
    }
    incomplete = sum(1 for item in properties if _quality_score(item) < 70)
    inconsistent = len({row["property"].pk for row in anomaly_rows}) + sum(1 for item in properties if item.location_notes)
    price_m2_values = curated_price_m2_values(properties)
    context = {
        "total": total,
        "query_params": request.GET,
        "by_locality": _counter(properties, _canonical_display_locality),
        "by_neighborhood": _counter(
            properties,
            lambda item: item.detected_neighborhood
            or item.neighborhood
            or item.inferred_neighborhood,
        ),
        "by_currency": _counter(properties, lambda item: item.currency),
        "by_agency": _counter(listings, lambda item: item.agency.name if item.agency else ""),
        "by_source": _counter(listings, lambda item: item.source.name),
        "price_stats": _summary(curated_price_values(properties)),
        "price_m2_stats": _summary(price_m2_values),
        "total_area_stats": _summary(
            [
                valid_value(item, "land_area") or valid_value(item, "total_area")
                for item in properties
                if valid_value(item, "land_area") or valid_value(item, "total_area")
            ]
        ),
        "covered_area_stats": _summary(curated_metric_values(properties, "covered_area")),
        "bedroom_stats": _summary(curated_metric_values(properties, "bedrooms")),
        "bathroom_stats": _summary(curated_metric_values(properties, "bathrooms")),
        "with_detected_address": sum(1 for item in properties if item.detected_address or item.address),
        "with_map_location": sum(1 for item in properties if hasattr(item, "location")),
        "without_reliable_location": sum(
            1
            for item in properties
            if item.location_confidence not in {
                Property.LocationConfidence.HIGH,
                Property.LocationConfidence.MEDIUM,
            }
        ),
        "possible_duplicates": possible_duplicates,
        "new_since_last_scrape": sum(1 for item in properties if latest_started and item.first_seen_at >= latest_started),
        "quality": quality,
        "quality_percent": {
            key: round(value / total * 100) if total else 0 for key, value in quality.items()
        },
        "quality_links": {
            key: {
                "present": query_url(request.GET, {"quality_field": key, "quality_state": "present"}, path=stats_path),
                "missing": query_url(request.GET, {"quality_field": key, "quality_state": "missing"}, path=stats_path),
            }
            for key in quality
        },
        "incomplete": incomplete,
        "inconsistent": inconsistent,
        "anomaly_rows": anomaly_rows[:120],
        "anomaly_count": len(anomaly_rows),
        "anomaly_model_options": ANOMALY_MODEL_OPTIONS,
        "anomaly_model_summary": anomaly_model_summary,
    }
    price_values = _numbers(curated_price_values(properties))[:500]
    min_price = min(price_values) if price_values else 0
    max_price = max(price_values) if price_values else 0
    step = max((max_price - min_price) / 8, 1) if price_values else 1
    price_buckets = []
    for index in range(8):
        start = min_price + step * index
        end = min_price + step * (index + 1)
        bucket_properties = [
            item
            for item in properties
            if valid_price(item) is not None
            and float(valid_price(item)) >= start
            and (float(valid_price(item)) <= end if index == 7 else float(valid_price(item)) < end)
        ]
        price_buckets.append(
            {
                "label": f"{round(start):,}".replace(",", "."),
                "value": len(bucket_properties),
                "total": len(bucket_properties),
                "favorites": sum(1 for item in bucket_properties if item.is_favorite),
                "reviewed": sum(1 for item in bucket_properties if item.reviewed_at and not item.is_favorite),
                "pending": sum(1 for item in bucket_properties if not item.reviewed_at and not item.is_favorite),
                "url": query_url(request.GET, {"price_min": round(start), "price_max": round(end)}, path=stats_path),
            }
        )
    context["chart_data"] = {
        "by_locality": _series(
            properties,
            _canonical_display_locality,
            lambda item, label: query_url(request.GET, {"locality": label if label != "Sin dato" else ""}, path=stats_path),
        ),
        "by_neighborhood": _series(
            properties,
            lambda item: item.detected_neighborhood
            or item.neighborhood
            or item.inferred_neighborhood,
            lambda item, label: query_url(request.GET, {"neighborhood": label if label != "Sin dato" else ""}, path=stats_path),
        )[:12],
        "by_agency": _series(
            listings,
            lambda item: item.agency.name if item.agency else "",
            lambda item, label: query_url(request.GET, {"agency": item.agency_id if item.agency_id else ""}, path=stats_path),
        )[:12],
        "price_buckets": price_buckets,
        "prices": price_values,
        "zone_price_volatility": _zone_price_statistics(properties, request.GET, stats_path),
        "zone_type_matrix": _zone_type_matrix(properties, request.GET, stats_path),
        "liquidity": _liquidity_buckets(properties),
        "security": _security_price_summary(properties, request.GET, stats_path),
        "heatmap_points": _heatmap_points(properties, request.GET),
        "surfaces": _numbers(
            [
                valid_value(item, "land_area") or valid_value(item, "total_area")
                for item in properties
            ]
        )[:500],
        "bedrooms_price": [
            {
                **_chart_property_payload(item, request.GET, stats_path),
                "x": valid_value(item, "bedrooms"),
                "y": float(valid_price(item)),
            }
            for item in properties
            if valid_value(item, "bedrooms") is not None and valid_price(item) is not None
        ][:500],
        "surface_price": [
            {
                **_chart_property_payload(item, request.GET, stats_path),
                "x": float(valid_area(item)),
                "y": float(valid_price(item)),
            }
            for item in properties
            if valid_area(item) is not None and valid_price(item) is not None
        ][:500],
    }
    cache.set(cache_key, context, 120)
    context.update(filter_context(request.GET))
    return render(request, "properties/stats.html", context)


@ensure_csrf_cookie
def scraping_dashboard(request):
    mark_stale_running_jobs()
    jobs = ScrapeJob.objects.prefetch_related("sources").order_by("-created_at")[:8]
    return render(
        request,
        "properties/scraping.html",
        {
            "sources": source_catalog(include_disabled=True),
            "jobs": [serialize_job(job) for job in jobs],
        },
    )


def _optional_positive_int(value, field_name):
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} debe ser un entero.")
    if parsed < 1:
        raise ValueError(f"{field_name} debe ser positivo.")
    return parsed


def _optional_non_negative_int(value, field_name):
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} debe ser un entero.")
    if parsed < 0:
        raise ValueError(f"{field_name} no puede ser negativo.")
    return parsed


@require_POST
def create_scrape_job_api(request):
    try:
        payload = json.loads(request.body or "{}")
        sources = payload.get("sources") or []
        workers = payload.get("workers") or {}
        scrape_mode = payload.get("scrape_mode") or ScrapeJob.Mode.COMPLETE
        max_pages = _optional_positive_int(payload.get("max_pages"), "max_pages")
        start_page = _optional_positive_int(payload.get("start_page"), "start_page")
        max_listings = _optional_positive_int(payload.get("max_listings"), "max_listings")
        geocode_limit = _optional_non_negative_int(payload.get("geocode_limit"), "geocode_limit")
        request_timeout = _optional_positive_int(
            payload.get("request_timeout_seconds"), "request_timeout_seconds"
        )
        max_errors = _optional_positive_int(
            payload.get("max_errors_per_source"), "max_errors_per_source"
        )
        job = create_scrape_job(
            sources,
            workers,
            max_pages=max_pages,
            start_page=start_page,
            max_listings=max_listings,
            geocode_limit=geocode_limit if geocode_limit is not None else 25,
            scrape_mode=scrape_mode,
            request_timeout_seconds=request_timeout,
            max_errors_per_source=max_errors,
            enforce_single_active=True,
        )
        start_scrape_job(job)
    except ActiveScrapeJobError as exc:
        active = get_object_or_404(ScrapeJob.objects.prefetch_related("sources"), pk=exc.active_job_id)
        payload = serialize_job(active)
        payload["error"] = str(exc)
        return JsonResponse(payload, status=409)
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(serialize_job(job), status=201)


def scrape_job_status_api(request, pk):
    mark_stale_running_jobs()
    job = get_object_or_404(ScrapeJob.objects.prefetch_related("sources"), pk=pk)
    return JsonResponse(serialize_job(job))


@require_POST
def cancel_scrape_job_api(request, pk):
    job = get_object_or_404(ScrapeJob, pk=pk)
    if job.status in {ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING}:
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
    return JsonResponse(serialize_job(job))


@require_POST
def retry_scrape_job_api(request, pk):
    original = get_object_or_404(ScrapeJob, pk=pk)
    if original.status in {ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING}:
        return JsonResponse({"error": "El job todavia esta en curso."}, status=400)
    try:
        job = retry_scrape_job(original, enforce_single_active=True)
        start_scrape_job(job)
    except ActiveScrapeJobError as exc:
        active = get_object_or_404(ScrapeJob.objects.prefetch_related("sources"), pk=exc.active_job_id)
        payload = serialize_job(active)
        payload["error"] = str(exc)
        return JsonResponse(payload, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(serialize_job(job), status=201)


@require_POST
def retry_scrape_job_errors_api(request, pk):
    original = get_object_or_404(ScrapeJob.objects.prefetch_related("sources"), pk=pk)
    if original.status in {ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING}:
        return JsonResponse({"error": "El job todavia esta en curso."}, status=400)
    try:
        job = retry_scrape_job_errors(original, enforce_single_active=True)
        start_scrape_job(job)
    except ActiveScrapeJobError as exc:
        active = get_object_or_404(ScrapeJob.objects.prefetch_related("sources"), pk=exc.active_job_id)
        payload = serialize_job(active)
        payload["error"] = str(exc)
        return JsonResponse(payload, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(serialize_job(job), status=201)
