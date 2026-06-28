import json
import base64
import re
import html
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from django.conf import settings
from properties.models import Property
from properties.services.normalization import (
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
    repair_mojibake_text,
)


def clean_text(value):
    value = repair_mojibake_text(value)
    return re.sub(r"\s+", " ", value or "").strip()


def json_ld_objects(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.JSONDecoder(strict=False).decode(
                script.string or script.get_text()
            )
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, list):
            yield from payload
        else:
            yield payload


def walk_json_ld(value):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph:
            yield from walk_json_ld(graph)
        for child in value.values():
            if isinstance(child, (list, dict)):
                yield from walk_json_ld(child)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json_ld(item)


def first_json_ld(soup, types):
    wanted = {types} if isinstance(types, str) else set(types)
    for payload in json_ld_objects(soup):
        for item in walk_json_ld(payload):
            item_type = item.get("@type")
            if isinstance(item_type, list):
                if wanted.intersection(item_type):
                    return item
            elif item_type in wanted:
                return item
    return None


def _map_float(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _inside_target_bounds(latitude, longitude):
    if latitude is None or longitude is None:
        return False
    bounds = settings.HURLINGHAM_BOUNDS
    return (
        bounds["south"] <= latitude <= bounds["north"]
        and bounds["west"] <= longitude <= bounds["east"]
    )


def _is_default_map_coordinate(latitude, longitude):
    if latitude is None or longitude is None:
        return False
    defaults = ((25.7308309, -80.444149),)
    return any(abs(latitude - lat) < 0.000001 and abs(longitude - lon) < 0.000001 for lat, lon in defaults)


def _add_map_coordinate(candidates, method, latitude, longitude, address="", confidence="high", require_target_bounds=True):
    lat = _map_float(latitude)
    lon = _map_float(longitude)
    if lat is None or lon is None or _is_default_map_coordinate(lat, lon):
        return
    outside_target = not _inside_target_bounds(lat, lon)
    if require_target_bounds and outside_target:
        return
    candidates.append(
        {
            "method": method,
            "latitude": lat,
            "longitude": lon,
            "address": clean_text(address),
            "confidence": confidence,
            "outside_target": outside_target,
        }
    )


def _decode_base64_text(value):
    try:
        return base64.b64decode(str(value), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def extract_map_coordinates(markup, require_target_bounds=True):
    """Extract source-published map coordinates from public listing HTML."""
    soup = BeautifulSoup(markup or "", "html.parser")
    candidates = []

    for tag in soup.select("[data-latitude][data-longitude]"):
        _add_map_coordinate(
            candidates,
            "data-latitude",
            tag.get("data-latitude"),
            tag.get("data-longitude"),
            require_target_bounds=require_target_bounds,
        )

    for tag in soup.select("[data-map]"):
        raw = html.unescape(tag.get("data-map") or "")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            continue
        _add_map_coordinate(
            candidates,
            "data-map",
            payload.get("latitude") or payload.get("lat"),
            payload.get("longitude") or payload.get("lng") or payload.get("lang") or payload.get("lon"),
            payload.get("address") or "",
            require_target_bounds=require_target_bounds,
        )

    for match in re.finditer(r"propertyMapData\s*=\s*(\{.*?\})\s*;", markup or "", re.S):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        _add_map_coordinate(
            candidates,
            "propertyMapData",
            payload.get("latitude") or payload.get("lat"),
            payload.get("longitude") or payload.get("lng") or payload.get("lang") or payload.get("lon"),
            payload.get("address") or "",
            require_target_bounds=require_target_bounds,
        )

    for match in re.finditer(
        r"mapLatOf\s*=\s*['\"](?P<lat>[A-Za-z0-9+/=]+)['\"].{0,240}?mapLngOf\s*=\s*['\"](?P<lon>[A-Za-z0-9+/=]+)['\"]",
        markup or "",
        re.I | re.S,
    ):
        _add_map_coordinate(
            candidates,
            "zonaprop_base64_map",
            _decode_base64_text(match.group("lat")),
            _decode_base64_text(match.group("lon")),
            require_target_bounds=require_target_bounds,
        )

    for payload in json_ld_objects(soup):
        for item in walk_json_ld(payload):
            geo = item.get("geo") if isinstance(item, dict) else None
            if not isinstance(geo, dict):
                continue
            _add_map_coordinate(
                candidates,
                "jsonld_geo",
                geo.get("latitude") or geo.get("lat"),
                geo.get("longitude") or geo.get("lng") or geo.get("lon"),
                require_target_bounds=require_target_bounds,
            )

    patterns = (
        r'"latitude"\s*:\s*"?(?P<lat>-?\d+[\.,]\d+)"?.{0,180}?"longitude"\s*:\s*"?(?P<lon>-?\d+[\.,]\d+)"?',
        r'"lat"\s*:\s*"?(?P<lat>-?\d+[\.,]\d+)"?.{0,180}?"lng"\s*:\s*"?(?P<lon>-?\d+[\.,]\d+)"?',
        r"data-latitude\s*=\s*['\"](?P<lat>-?\d+[\.,]\d+)['\"].{0,180}?data-longitude\s*=\s*['\"](?P<lon>-?\d+[\.,]\d+)['\"]",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, markup or "", re.I | re.S):
            _add_map_coordinate(
                candidates,
                "regex_pair",
                match.group("lat"),
                match.group("lon"),
                require_target_bounds=require_target_bounds,
            )

    latitudes = re.findall(r"-34[\.,]\d{4,}", markup or "")
    longitudes = re.findall(r"-58[\.,]\d{4,}", markup or "")
    if latitudes and longitudes:
        _add_map_coordinate(
            candidates,
            "fallback_latlon",
            latitudes[0],
            longitudes[0],
            confidence="medium",
            require_target_bounds=require_target_bounds,
        )

    output = []
    seen = set()
    preference = {
        "data-latitude": 0,
        "propertyMapData": 1,
        "data-map": 2,
        "zonaprop_base64_map": 3,
        "jsonld_geo": 4,
        "regex_pair": 5,
        "fallback_latlon": 6,
    }
    for item in sorted(candidates, key=lambda candidate: preference.get(candidate["method"], 99)):
        key = (round(item["latitude"], 7), round(item["longitude"], 7))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def first_map_coordinate(markup, allow_fallback=True, require_target_bounds=True):
    for coordinate in extract_map_coordinates(markup, require_target_bounds=require_target_bounds):
        if coordinate["method"] != "fallback_latlon" or allow_fallback:
            return coordinate
    return None


def text_value(text, patterns, cast=None):
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            value = match.group(1).strip()
            return cast(value) if cast else value
    return None


def metric_label_pattern(labels):
    return "|".join(labels)


def value_after_label(text, labels, cast=parse_decimal, unit_pattern=r"(?:m2|m²|mts|mts²|Mts²)?"):
    label_pattern = metric_label_pattern(labels)
    return text_value(
        text,
        [rf"(?:{label_pattern})\s*:?\s*([\d.,]+)\s*{unit_pattern}"],
        cast,
    )


def value_before_label(text, labels, cast=parse_decimal, unit_pattern=r"(?:m2|m²|mts|mts²|Mts²)?"):
    label_pattern = metric_label_pattern(labels)
    return text_value(
        text,
        [rf"([\d.,]+)\s*{unit_pattern}\s*(?:{label_pattern})"],
        cast,
    )


def first_present(*values):
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def evidence_set(data, field, value, source, raw=None):
    if value in (None, ""):
        return
    data[field] = value
    data.setdefault("raw_data", {})
    evidence = data["raw_data"].setdefault("extraction_evidence", {})
    evidence[field] = {"source": source, "raw": clean_text(raw or str(value))[:400]}


def parse_labeled_fields(text, labels):
    """Return label/value pairs from compact listing text.

    labels accepts canonical -> label regex list. It intentionally stops at the
    next known label so values do not absorb following property metadata.
    """
    all_label_patterns = []
    for options in labels.values():
        all_label_patterns.extend(options)
    stop_pattern = "|".join(all_label_patterns)
    fields = {}
    for canonical, options in labels.items():
        label_pattern = "|".join(options)
        match = re.search(
            rf"(?:{label_pattern})\s*:?\s*(.+?)(?=\s+(?:{stop_pattern})(?:\s*:|\s+)|\s*$)",
            text,
            re.I | re.S,
        )
        if match:
            fields[canonical] = clean_text(match.group(1))
    return fields


def parse_surface_pair(text):
    match = re.search(
        r"([\d.,]+)\s*m2\s*\(\s*Built\s+in\s*([\d.,]+)\s*m2\s*\)",
        text,
        re.I,
    )
    if not match:
        return None, None
    return parse_decimal(match.group(1)), parse_decimal(match.group(2))


def external_id_from_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] or path


def basic_html_data(soup, url):
    text = clean_text(soup.get_text(" ", strip=True))
    title_node = soup.select_one("h1") or soup.select_one("title")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else "Propiedad"
    price_text = text_value(
        text,
        [
            r"\b(USD|U\$S|US\$)\s*([\d.,]+)",
            r"\b(ARS|\$)\s*([\d.,]+)",
        ],
    )
    currency_match = re.search(r"\b(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", text, re.I)
    return {
        "external_id": external_id_from_url(url),
        "url": url,
        "title": title,
        "description": "",
        "property_type": infer_property_type(title, text[:1000]),
        "currency": normalize_currency(currency_match.group(1)) if currency_match else "",
        "price": parse_decimal(currency_match.group(2)) if currency_match else parse_decimal(price_text),
        "rooms": text_value(text, [r"(\d+)\s*(?:ambientes|ambiences)"], parse_int),
        "bedrooms": text_value(text, [r"(\d+)\s*(?:dormitorios|habitaciones|rooms)"], parse_int),
        "bathrooms": text_value(text, [r"(\d+(?:[.,]\d+)?)\s*(?:baños|bathrooms)"], parse_decimal),
        "garages": text_value(text, [r"(\d+)\s*(?:cocheras|garages)"], parse_int),
        "covered_area": text_value(
            text,
            [
                r"(?:superficie cubierta|sup\.?\s*cubierta|cubierta|built in)\s*:?\s*([\d.,]+)\s*m",
                r"([\d.,]+)\s*m2\s*\(Built in",
            ],
            parse_decimal,
        ),
        "total_area": text_value(
            text,
            [r"(?:superficie total|sup\.?\s*total|terreno)\s*:?\s*([\d.,]+)\s*m"],
            parse_decimal,
        ),
        "land_area": text_value(
            text, [r"(?:terreno|lote)\s*:?\s*([\d.,]+)\s*m"], parse_decimal
        ),
        "age_years": text_value(text, [r"antig(?:ü|u)edad\s*:?\s*(\d+)"], parse_int),
        "features": [],
        "status": Property.Status.ACTIVE,
        "images": [
            image["src"]
            for image in soup.select("img[src]")
            if image.get("src", "").startswith("http")
        ][:30],
    }
