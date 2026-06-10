import json
import re
from urllib.parse import urlparse

from properties.models import Property
from properties.services.normalization import (
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
)


def clean_text(value):
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


def text_value(text, patterns, cast=None):
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            value = match.group(1).strip()
            return cast(value) if cast else value
    return None


def metric_label_pattern(labels):
    return "|".join(labels)


def value_after_label(text, labels, cast=parse_decimal, unit_pattern=r"(?:m2|m²|mÂ²|mts|mts²|Mts²)?"):
    label_pattern = metric_label_pattern(labels)
    return text_value(
        text,
        [rf"(?:{label_pattern})\s*:?\s*([\d.,]+)\s*{unit_pattern}"],
        cast,
    )


def value_before_label(text, labels, cast=parse_decimal, unit_pattern=r"(?:m2|m²|mÂ²|mts|mts²|Mts²)?"):
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
    text = soup.get_text(" ", strip=True)
    title_node = soup.select_one("h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else "Propiedad"
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
