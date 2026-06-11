import csv
import hashlib
import io
import json
import statistics
from decimal import Decimal, InvalidOperation
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
from django.views.decorators.http import require_POST

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
    )
    return {
        "agencies": Agency.objects.filter(listings__isnull=False).distinct().order_by("name"),
        "sources": Source.objects.order_by("name"),
        "property_types": Property.Type.choices,
        "statuses": Property.Status.choices,
        "location_confidences": Property.LocationConfidence.choices,
        "localities": ["Hurlingham", "Villa Tesei", "William C. Morris"],
        "neighborhood_options": _neighborhood_options(),
        "features": ["Pileta", "Quincho", "Jardin", "Parrilla", "Apto credito"],
        "query_params": params,
        "selected_filters": {key: _param_values(params, key) for key in multi_keys},
    }


def _neighborhood_options():
    counts = {}
    for neighborhood, detected in Property.objects.values_list(
        "neighborhood", "detected_neighborhood"
    ):
        for name in {neighborhood, detected}:
            if name:
                counts[name] = counts.get(name, 0) + 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: item[0].lower())
    ]


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
    "locality": {"label": "Localidad", "key": lambda item, distances: item.locality or ""},
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


def filtered_properties(params):
    queryset = (
        Property.objects.select_related("location")
        .prefetch_related(
            Prefetch(
                "listings",
                queryset=Listing.objects.select_related("agency", "source").prefetch_related(
                    Prefetch("images", queryset=ListingImage.objects.order_by("position"))
                ),
            )
        )
        .all()
    )
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
        "locality": "locality",
        "status": "status",
    }
    for parameter, field in filters.items():
        values = _param_values(params, parameter)
        if values:
            queryset = queryset.filter(**{f"{field}__in": values})

    neighborhood_values = _param_values(params, "neighborhood")
    if neighborhood_values:
        queryset = queryset.filter(
            Q(neighborhood__in=neighborhood_values)
            | Q(detected_neighborhood__in=neighborhood_values)
        )

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


@ensure_csrf_cookie
def search(request):
    properties, distances = filtered_properties(request.GET)
    paginator = Paginator(properties, 24)
    page = paginator.get_page(request.GET.get("page"))
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
    previous_url = next_url = ""
    return_to = safe_return_to(request)
    parsed = urlparse(return_to)
    return_label = "Estadisticas" if parsed.path == reverse("properties:stats") else "Resultados"
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if parsed.path == reverse("properties:search"):
        properties, _ = filtered_properties(params)
        ids = [item.pk for item in properties]
        if property_obj.pk in ids:
            index = ids.index(property_obj.pk)
            if index > 0:
                previous_url = build_detail_url(ids[index - 1], urlencode(params))
            if index < len(ids) - 1:
                next_url = build_detail_url(ids[index + 1], urlencode(params))
    location_payload = {
        "id": property_obj.pk,
        "latitude": location.latitude if location else None,
        "longitude": location.longitude if location else None,
        "precision": location.precision if location else "",
    }
    return render(
        request,
        "properties/detail.html",
        {
            "property": property_obj,
            "map_config": map_config_payload(),
            "property_location": location_payload,
            "return_to": safe_return_to(request),
            "return_label": return_label,
            "previous_url": previous_url,
            "next_url": next_url,
        },
    )


def properties_geojson(request):
    properties, distances = filtered_properties(request.GET)
    features = []
    for property_obj in properties:
        item = _serialize(property_obj, distances.get(property_obj.pk), request.GET)
        if item["latitude"] is None:
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
    properties, _ = filtered_properties(request.GET)
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
    properties, _ = filtered_properties(request.GET)
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
        return {"avg": None, "median": None, "min": None, "max": None}
    return {
        "avg": round(statistics.mean(numbers), 2),
        "median": round(statistics.median(numbers), 2),
        "min": round(min(numbers), 2),
        "max": round(max(numbers), 2),
    }


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
    return {
        "id": property_obj.pk,
        "title": property_obj.title,
        "price": float(property_obj.price) if property_obj.price is not None else None,
        "currency": property_obj.currency,
        "address": property_obj.address or property_obj.detected_address or property_obj.locality or "",
        "agency": listing.agency.name if listing and listing.agency else "",
        "source": listing.source.name if listing and listing.source else "",
        "image": image,
        "url": build_detail_url(property_obj.pk, request_query, stats_path),
        "is_favorite": property_obj.is_favorite,
        "is_reviewed": property_obj.reviewed_at is not None,
    }


def _stats_cache_key(request):
    state = Property.objects.aggregate(
        total=Count("pk"),
        latest_seen=Max("last_seen_at"),
        latest_reviewed=Max("reviewed_at"),
    )
    favorite_count = Property.objects.filter(is_favorite=True).count()
    hidden_count = Property.objects.filter(is_hidden=True).count()
    raw = "|".join(
        [
            request.META.get("QUERY_STRING", ""),
            str(state.get("total") or 0),
            str(state.get("latest_seen") or ""),
            str(state.get("latest_reviewed") or ""),
            str(favorite_count),
            str(hidden_count),
        ]
    )
    return "stats:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    listings = Listing.objects.filter(property__in=properties).select_related("source", "agency", "property")
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
            anomaly_rows.append(
                {
                    "property": item,
                    "listing": listing,
                    "field": anomaly.field,
                    "value": anomaly.value,
                    "reason": anomaly.reason,
                    "detail_url": build_detail_url(item.pk, request.GET, stats_path),
                }
            )
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
        "by_locality": _counter(properties, lambda item: item.detected_locality or item.locality),
        "by_neighborhood": _counter(properties, lambda item: item.detected_neighborhood or item.neighborhood),
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
        "anomaly_rows": anomaly_rows[:40],
        "anomaly_count": len(anomaly_rows),
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
            lambda item: item.detected_locality or item.locality,
            lambda item, label: query_url(request.GET, {"locality": label if label != "Sin dato" else ""}, path=stats_path),
        ),
        "by_neighborhood": _series(
            properties,
            lambda item: item.detected_neighborhood or item.neighborhood,
            lambda item, label: query_url(request.GET, {"neighborhood": label if label != "Sin dato" else ""}, path=stats_path),
        )[:12],
        "by_agency": _series(
            listings,
            lambda item: item.agency.name if item.agency else "",
            lambda item, label: query_url(request.GET, {"agency": item.agency_id if item.agency_id else ""}, path=stats_path),
        )[:12],
        "price_buckets": price_buckets,
        "prices": price_values,
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
