import json
import os
import re
from math import ceil
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from django.utils import timezone

from properties.models import Property
from properties.services.normalization import (
    canonical_address_alias,
    classify_address_precision,
    extract_embedded_neighborhood,
    fold_text,
    infer_property_type,
    is_plausible_property_address,
    normalize_locality,
    normalize_currency,
    normalize_neighborhood_name,
    parse_decimal,
    parse_int,
    repair_mojibake_text,
)
from properties.services.location_enrichment import clean_detected_address

from .base import BaseScraper, SourceDefinition
from .parsing import (
    basic_html_data,
    evidence_set,
    external_id_from_url,
    first_json_ld,
    first_present,
    parse_labeled_fields,
    text_value,
    value_after_label,
)
from .paginated import ajax_paginated_discover, declared_total_from_text, paginated_discover


TARGET_ZONES = ("hurlingham", "villa tesei", "villa santos tesei", "william morris")


def clean_text(value):
    value = repair_mojibake_text(value)
    return re.sub(r"\s+", " ", value or "").strip()


def visible_text(soup):
    return clean_text(soup.get_text(" ", strip=True))


def is_target_zone(text):
    folded = (text or "").lower()
    return any(zone in folded for zone in TARGET_ZONES)


def number_before_label(text, labels, cast=parse_decimal):
    label_pattern = "|".join(labels)
    return text_value(
        text,
        [rf"([\d.,]+)\s*(?:m2|mts|m²|Mts²)?\s*(?:{label_pattern})"],
        cast,
    )


def label_before_number(text, labels, cast=parse_decimal):
    label_pattern = "|".join(labels)
    return text_value(
        text,
        [rf"(?:{label_pattern})\s*:?\s*([\d.,]+)\s*(?:m2|mts|m²|Mts²)?"],
        cast,
    )


def parse_multi_unit_offers(text):
    offers = []
    normalized = clean_text(text)
    if not re.search(r"\bUnidad\b", normalized, re.I):
        return offers
    unit_pattern = re.compile(
        r"Unidad\s+(.+?)\s+[–-]\s+(\d+)\s*Ambientes?.*?"
        r"Superficie\s+total\s*:?\s*([\d.,]+)\s*m(?:2|²)?.*?"
        r"Precio\s*:?\s*(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)",
        re.I,
    )
    for match in unit_pattern.finditer(normalized):
        price = parse_decimal(match.group(5))
        surface = parse_decimal(match.group(3))
        rooms = parse_int(match.group(2))
        if price is None or surface is None:
            continue
        offers.append(
            {
                "unit": clean_text(match.group(1)),
                "rooms": rooms,
                "total_area": str(surface),
                "currency": normalize_currency(match.group(4)),
                "price": str(price),
            }
        )
    return offers


def plausible_int(value, maximum):
    if value in (None, ""):
        return None
    parsed = parse_int(value)
    if parsed is not None and 0 <= parsed <= maximum:
        return parsed

    text = str(value)
    if re.fullmatch(r"\s*-\s*\d+(?:[.,]\d+)?\s*", text):
        return None
    for match in re.finditer(r"(?<![-\d.,])(\d+)(?![\d.,])", text):
        candidate = int(match.group(1))
        if 0 <= candidate <= maximum:
            return candidate
    return None


def price_near_label(text):
    match = re.search(r"(?:Precio\s*:?\s*)?(U\$S|U\$D|US\$|USD|ARS|\$)\s*([\d.,]+)", text, re.I)
    if not match:
        return "", None
    return normalize_currency(match.group(1)), parse_decimal(match.group(2))


def split_suggested_text(text):
    return re.split(
        r"Propiedades\s+Sugeridas|Propiedades\s+similares|Tambien\s+puede\s+interesarte|También\s+puede\s+interesarte",
        text or "",
        maxsplit=1,
        flags=re.I,
    )[0]


def normalized_label(value):
    folded = fold_text(value).replace("°", "").replace("º", "")
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def parse_dimension_value(value):
    text = clean_text(value)
    dimension = re.search(
        r"([\d.,]+)\s*(?:x|×|por)\s*([\d.,]+)\s*(?:m2|m²|mts|metros)?",
        text,
        re.I,
    )
    if dimension:
        front = parse_decimal(dimension.group(1))
        depth = parse_decimal(dimension.group(2))
        area = front * depth if front is not None and depth is not None else None
        return area, front, depth
    return parse_decimal(text), None, None


def parse_area_value(value):
    text = clean_text(value)
    range_match = re.search(r"([\d.,]+)\s*/\s*[\d.,]+", text)
    if range_match:
        return parse_decimal(range_match.group(1))
    return parse_decimal(text)


def parse_garage_value(value):
    text = clean_text(value)
    if re.search(r"\bsin\b|no\s+tiene|no\s+posee", text, re.I):
        return 0
    parsed = plausible_int(text, 8)
    if parsed is not None:
        return parsed
    if re.search(r"cochera|garage|garaje", text, re.I):
        return 1
    return None


def price_from_guarnieri_text(text):
    match = re.search(r"(?:Precio\s*:?\s*)?(U\$S|U\$D|US\$|USD|ARS|\$)\s*([\d.,]+)", text or "", re.I)
    if match:
        return normalize_currency(match.group(1)), parse_decimal(match.group(2))
    if re.search(r"\bconsulte\b|consultar", text or "", re.I):
        return "", None
    return "", None


def apply_detail_fields(data, fields, source):
    def first_decimal(value):
        return text_value(str(value or ""), [r"([\d.,]+)"], parse_decimal)

    def first_int(value):
        parsed = first_decimal(value)
        return int(parsed) if parsed is not None else None

    mapping = {
        "rooms": ("rooms", first_int),
        "bedrooms": ("bedrooms", first_int),
        "bathrooms": ("bathrooms", first_decimal),
        "toilets": ("toilets", first_int),
        "garages": ("garages", first_int),
        "covered_area": ("covered_area", first_decimal),
        "total_area": ("total_area", first_decimal),
        "land_area": ("land_area", first_decimal),
        "uncovered_area": ("uncovered_area", first_decimal),
        "semicovered_area": ("semicovered_area", first_decimal),
        "front_width": ("front_width", first_decimal),
        "lot_depth": ("lot_depth", first_decimal),
        "age_years": ("age_years", first_int),
        "building_floors": ("building_floors", first_int),
    }
    for source_key, (target_key, caster) in mapping.items():
        if source_key not in fields:
            continue
        value = caster(fields[source_key])
        evidence_set(data, target_key, value, source, fields[source_key])


def detect_operation(text, url=""):
    path = urlparse(url or "").path.lower()
    if re.search(r"\bventa\b|/venta-|/venta/", path):
        return "sale"
    if re.search(r"\balquiler\b|/alquiler-|/alquiler/|/alcl", path):
        return "rent"
    folded = f"{url} {text or ''}".lower()
    if re.search(r"\b(alquiler|alquilar|en\s+alquiler|alcl)", folded):
        return "rent"
    if re.search(r"\b(venta|vender|en\s+venta|vecl)", folded):
        return "sale"
    return "sale"


def is_listing_page_url(url):
    path = urlparse(url).path.lower().rstrip("/")
    listing_patterns = (
        r"/inmuebles(?:-|$)",
        r"/inmuebles-[^/]+\.html$",
        r"/casas/?$",
        r"/venta/?$",
        r"/site/properties/sale/?$",
        r"/inmobiliaria/ciudad/",
        r"/inmobiliaria/tipo-de-propiedad/",
    )
    return any(re.search(pattern, path) for pattern in listing_patterns)


def absolute_images(scraper, soup):
    images = []
    for image in soup.select("img[src], img[data-src]"):
        src = image.get("src") or image.get("data-src")
        if not src or src.startswith("data:"):
            continue
        images.append(scraper.absolute(src))
    return list(dict.fromkeys(images))[:30]


def links_matching(scraper, soup, patterns, require_target_text=False):
    seen = set()
    base_netloc = urlparse(scraper.definition.base_url).netloc.removeprefix("www.")
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if href.startswith(("mailto:", "tel:", "whatsapp:")):
            continue
        url = scraper.absolute(href.split("#")[0])
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc.removeprefix("www.") != base_netloc:
            continue
        if not any(re.search(pattern, parsed.path, re.I) for pattern in patterns):
            continue
        if require_target_text:
            candidate_text = visible_text(anchor.find_parent(["article", "li", "div"]) or anchor)
            if candidate_text and not is_target_zone(candidate_text):
                continue
            if candidate_text and "alquiler" in candidate_text.lower() and "venta" not in candidate_text.lower():
                continue
        if url not in seen:
            seen.add(url)
            yield url



ADDRESS_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:salon|living|cocina|comedor|dormitorio|habitacion|bano|banos|ambiente|ambientes|garage|cochera|patio|jardin|quincho|lavadero|planta|galeria)\s+de\b",
    re.I,
)


def normalize_argencasas_address(value):
    text = clean_text(value or "")
    text = re.sub(
        r"^(?:direccion|direcci[o?]n|ubicacion|ubicaci[o?]n)\s*:?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.split(
        r"\s*(?:,|\||-)\s*(?:hurlingham|villa\s+tesei|william|buenos\s+aires|argentina)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    match = re.search(
        r"\b([A-Za-z?????????????? .'-]{3,60}?)\s+al\s+(\d{2,5})\b",
        text,
        re.I,
    )
    if match:
        street = clean_text(match.group(1)).strip(" ,.-")
        text = f"{street} {match.group(2)}"
    text = clean_detected_address(text)
    if text and ADDRESS_FALSE_POSITIVE_RE.search(text):
        return ""
    if text and not re.search(r"\d{2,5}|\b(?:y|esquina|entre)\b", text, re.I):
        return ""
    return text


def extract_argencasas_address_from_text(text):
    patterns = [
        r"(?:Direccion|Direcci[o?]n|Direcci??n|Ubicacion|Ubicaci[o?]n|Ubicaci??n)\s*:?\s*([A-Za-z?????????????? .'-]{3,60}\s+al\s+\d{2,5})",
        r"\b([A-Z??????][A-Z?????? .'-]{2,60}\s+al\s+\d{2,5})\b",
    ]
    for pattern in patterns:
        candidate = text_value(text, [pattern])
        address = normalize_argencasas_address(candidate)
        if address:
            return address
    return ""


def extract_zonaprop_address_from_text(text):
    if not text:
        return ""
    property_words = r"\b(?:venta|alquiler|departamento|casa|duplex|d[úu]plex|ph|lote|terreno|local|ambientes?|propiedad)\b"

    def clean_candidate(value):
        pieces = re.split(property_words, value, flags=re.I)
        candidate = pieces[-1] if pieces else value
        candidate = clean_text(candidate).strip(" ,.-")
        candidate = re.sub(
            r"^(?:en\s+)?(?:hurlingham|villa\s+tesei|william(?:\s+c\.?)?\s+morris|buenos\s+aires|argentina)\s+",
            "",
            candidate,
            flags=re.I,
        )
        address = clean_detected_address(candidate)
        return canonical_address_alias(address) if address else ""

    candidates = []
    for match in re.finditer(
        r"\b([^,|]{3,90}?\s+(?:al\s+)?\d{2,5})\s*,\s*(?:Hurlingham|Villa\s+Tesei|William(?:\s+C\.?)?\s+Morris)\b",
        text,
        re.I,
    ):
        candidates.append(match.group(1))
    for candidate in candidates:
        address = clean_candidate(candidate)
        if address:
            return address
    for match in re.finditer(
        r"\b([^,|]{3,60}?)\s*,\s*(?:B\d{4}\s+[^,]+,\s*)?(?:(?:Provincia\s+de\s+Buenos\s+Aires|Buenos\s+Aires|Argentina)\.?\s*,\s*){0,3}(?:Hurlingham|Villa\s+Tesei|William(?:\s+C\.?)?\s+Morris)\b",
        text,
        re.I,
    ):
        candidate = clean_candidate(match.group(1))
        folded = fold_text(candidate)
        if re.search(property_words, folded, re.I):
            continue
        address = canonical_address_alias(clean_detected_address(candidate))
        if address:
            return address
    return ""


def parse_zonaprop_highlights(data, text):
    highlights = {}

    def set_highlight(field, value, raw):
        if value in (None, ""):
            return
        highlights[field] = raw
        evidence_set(data, field, value, "zonaprop_highlights", raw)

    total = text_value(text, [r"([\d.,]+)\s*m(?:2|Â²|²)?\s*tot\.?"], parse_decimal)
    covered = text_value(text, [r"([\d.,]+)\s*m(?:2|Â²|²)?\s*cub\.?"], parse_decimal)
    rooms = text_value(text, [r"(\d+)\s*amb\.?"], parse_int)
    bathrooms = text_value(text, [r"(\d+(?:[.,]\d+)?)\s*ba(?:n|ñ|Ã±)o?s?\.?"], parse_decimal)
    garages = text_value(text, [r"(\d+)\s*coch\.?"], parse_int)
    bedrooms = text_value(text, [r"(\d+)\s*dorm\.?"], parse_int)
    age = text_value(text, [r"(\d+)\s*a(?:n|ñ|Ã±)os?\b"], parse_int)

    set_highlight("total_area", total, f"{total} m² tot." if total is not None else "")
    set_highlight("covered_area", covered, f"{covered} m² cub." if covered is not None else "")
    set_highlight("rooms", rooms, f"{rooms} amb." if rooms is not None else "")
    set_highlight("bathrooms", bathrooms, f"{bathrooms} baño" if bathrooms is not None else "")
    set_highlight("garages", garages, f"{garages} coch." if garages is not None else "")
    set_highlight("bedrooms", bedrooms, f"{bedrooms} dorm." if bedrooms is not None else "")
    set_highlight("age_years", age, f"{age} años" if age is not None else "")

    if re.search(r"\ba\s+estrenar\b", text, re.I):
        set_highlight("age_years", 0, "A estrenar")
        data["condition_category"] = Property.ConditionCategory.NEW
        data.setdefault("raw_data", {})["condition"] = "A estrenar"

    if highlights:
        data.setdefault("raw_data", {})["zonaprop_highlights"] = highlights
    return data


def zonaprop_currency_from_price(displayed_currency, amount):
    displayed = (displayed_currency or "").strip()
    normalized = normalize_currency(displayed)
    if displayed == "$" and amount is not None:
        if amount < 5000000:
            return "USD", {
                "displayed_currency": displayed,
                "amount": str(amount),
                "inferred_currency": "USD",
                "rule": "zonaprop_ambiguous_sale_price_below_5000000",
            }
        return "ARS", {
            "displayed_currency": displayed,
            "amount": str(amount),
            "inferred_currency": "ARS",
            "rule": "zonaprop_ambiguous_sale_price_at_or_above_5000000",
        }
    return normalized, {
        "displayed_currency": displayed,
        "amount": str(amount) if amount is not None else "",
        "inferred_currency": normalized,
        "rule": "explicit_currency",
    }


def _zonaprop_price_from_soup(soup, text):
    price_node = soup.select_one(".price-value")
    candidates = []
    if price_node:
        candidates.append(clean_text(price_node.get_text(" ", strip=True)))
    candidates.append(text[:2000])
    for candidate in candidates:
        match = re.search(r"\bventa\s+(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", candidate, re.I)
        if not match:
            match = re.search(r"\b(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", candidate, re.I)
        if match:
            amount = parse_decimal(match.group(2))
            currency, evidence = zonaprop_currency_from_price(match.group(1), amount)
            return currency, amount, evidence
    return "", None, {}


def _zonaprop_address_from_soup(soup):
    selectors = (
        ".article-map-container h4",
        "[data-qa='posting-map-address']",
        ".section-location h4",
        "h4",
    )
    for selector in selectors:
        for node in soup.select(selector):
            candidate = clean_text(node.get_text(" ", strip=True))
            if not re.search(r"\b(Hurlingham|Villa\s+Tesei|William(?:\s+C\.?)?\s+Morris)\b", candidate, re.I):
                continue
            candidate = re.sub(r"^(?:venta|ubicaci(?:o|ó)n)\s+", "", candidate, flags=re.I).strip()
            candidate = re.sub(r"\b(?:USD|U\$S|US\$|ARS|\$)\s*[\d.,]+\b", "", candidate, flags=re.I).strip(" ,.-")
            address = canonical_address_alias(clean_detected_address(candidate))
            if address and not ADDRESS_FALSE_POSITIVE_RE.search(address):
                return address[:250]
    return ""


def enrich_from_common_text(data, text, default_locality="Hurlingham"):
    address = normalize_argencasas_address(data.get("address"))
    if not address:
        address = extract_zonaprop_address_from_text(text)
    if not address:
        candidate = text_value(
            text,
            [
                r"(?:Direccion|Direcci??n|Ubicacion|Ubicaci??n)\s*:?\s*(.+?)(?:Venta|USD|U\$S|US\$|ARS|\$|Caracteristicas|Caracter??sticas|Descripcion|Descripci??n)",
                r"^(.+?,\s*(?:Hurlingham|Villa Tesei|William Morris)[^\.]*)",
            ],
        )
        address = normalize_argencasas_address(candidate)
    if address:
        data["address"] = address[:250]
    elif data.get("address") and ADDRESS_FALSE_POSITIVE_RE.search(str(data.get("address"))):
        data["address"] = ""
    data["locality"] = (
        "William C. Morris"
        if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I)
        else "Villa Tesei"
        if re.search(r"villa\s+(?:santos\s+)?tesei|ciudad\s+tesei", text, re.I)
        else default_locality
    )
    price = re.search(r"\b(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", text, re.I)
    if price:
        data["currency"] = normalize_currency(price.group(1))
        data["price"] = parse_decimal(price.group(2))
    data["rooms"] = data.get("rooms") or text_value(text, [r"(\d+)\s*(?:ambientes|amb\.|ambs\.)"], parse_int)
    data["bedrooms"] = (data.get("bedrooms") or None) or text_value(
        text,
        [
            r"(?:dormitorios|habitaciones|hab\.)\s*:?\s*(\d+)",
            r"(\d+)\s*(?:dormitorios|habitaciones|hab\.)",
        ],
        parse_int,
    )
    data["bathrooms"] = (data.get("bathrooms") or None) or text_value(
        text,
        [
            r"(?:banos|ba.?os|cuartos de ba.?o|bathrooms)\s*:?\s*(\d+(?:[.,]\d+)?)",
            r"(\d+(?:[.,]\d+)?)\s*(?:banos|baños|bathrooms)",
            r"(?:banos|baños|cuartos de baño|bathrooms)\s*:?\s*(\d+(?:[.,]\d+)?)",
        ],
        parse_decimal,
    )
    data["garages"] = (data.get("garages") or None) or text_value(
        text, [r"(\d+)\s*(?:cocheras|garajes|garage|garages)"], parse_int
    )
    data["covered_area"] = data.get("covered_area") or text_value(
        text,
        [
            r"(?:superficie cubierta|sup\.?\s*cubierta|cubierta|area|área)\s*:?\s*([\d.,]+)\s*(?:m2|mts|m²)",
            r"([\d.,]+)\s*(?:m2|mts|m²)\s*(?:cubiertos|cubierta)",
        ],
        parse_decimal,
    )
    data["land_area"] = data.get("land_area") or text_value(
        text,
        [
            r"(?:superficie terreno|sup\.?\s*terreno|terreno|lote|tamano del lote|tamaño del lote)\s*:?\s*([\d.,]+)\s*(?:m2|mts|m²)",
            r"lote\s*(?:de)?\s*([\d.,]+)\s*(?:m2|mts|m²)",
        ],
        parse_decimal,
    )
    data["total_area"] = data.get("total_area") or text_value(
        text,
        [r"(?:superficie total|sup\.?\s*total|total)\s*:?\s*([\d.,]+)\s*(?:m2|mts|m²)"],
        parse_decimal,
    )
    if data.get("bathrooms") and data["bathrooms"] > 20:
        data["bathrooms"] = None
    data["property_type"] = infer_property_type(data.get("title"), data.get("description"), text[:700])
    data["operation"] = data.get("operation") or detect_operation(text, data.get("url") or "")
    data["location_precision"] = classify_address_precision(data.get("address"))
    data["status"] = Property.Status.ACTIVE
    return data


class CommonDetailScraper(BaseScraper):
    detail_patterns = ()
    require_target_text = False
    default_locality = "Hurlingham"

    def discover(self):
        soup = self.soup(self.definition.search_url)
        yield from links_matching(
            self,
            soup,
            self.detail_patterns,
            require_target_text=self.require_target_text,
        )

    def parse(self, url):
        if is_listing_page_url(url):
            return None
        soup = self.soup(url)
        text = visible_text(soup)
        data = basic_html_data(soup, url)
        payload = first_json_ld(soup, {"House", "Apartment", "Product", "RealEstateListing"})
        if payload:
            address = payload.get("address") or {}
            if isinstance(address, dict):
                data["address"] = address.get("streetAddress") or data.get("address") or ""
                data["locality"] = address.get("addressLocality") or data.get("locality") or self.default_locality
            data["title"] = payload.get("name") or data["title"]
            data["description"] = payload.get("description") or data.get("description") or ""
            image = payload.get("image")
            if isinstance(image, str):
                data["images"] = [image]
            elif isinstance(image, list):
                data["images"] = [item for item in image if isinstance(item, str)]
        if not data.get("images"):
            data["images"] = absolute_images(self, soup)
        data["agency"] = self.definition.name
        return self.enrich_common_data(data, text, soup)

    def enrich_common_data(self, data, text, soup):
        return enrich_from_common_text(data, text, self.default_locality)


class MultiSearchScraper(CommonDetailScraper):
    search_urls = ()

    def discover(self):
        seen = set()
        for search_url in self.search_urls or (self.definition.search_url,):
            soup = self.soup(search_url)
            for url in links_matching(
                self,
                soup,
                self.detail_patterns,
                require_target_text=self.require_target_text,
            ):
                if url not in seen:
                    seen.add(url)
                    yield url


class TokkoSearchScraper(CommonDetailScraper):
    tokko_ajax_path = "/Buscar"
    tokko_query = (
        "q=&currency=ANY&minprice=&maxprice=&minsurface=&maxsurface="
        "&minrooms=&maxrooms=&minbedrooms=&maxbedrooms=&operation=1"
        "&locations=25973&location_type=&ptypes=&o=2,2&watermark="
    )
    fallback_max_pages = 20

    def _ajax_url(self, page):
        return f"{self.definition.base_url}{self.tokko_ajax_path}?{self.tokko_query}&p={page}"

    def _listing_urls(self, soup):
        yield from links_matching(self, soup, self.detail_patterns)

    def discover(self):
        yield from ajax_paginated_discover(
            self,
            self.definition.search_url,
            self._ajax_url,
            self._listing_urls,
            fallback_max_pages=self.fallback_max_pages,
        )


class AnaliaFernandezScraper(TokkoSearchScraper):
    definition = SourceDefinition(
        slug="analia-fernandez",
        name="Analía Fernández Servicios Inmobiliarios",
        base_url="https://www.fernandezpropiedades.com.ar",
        search_url="https://www.fernandezpropiedades.com.ar/Buscar-propiedades-en-Venta-en-Hurlingham-25973",
        crawl_delay=3,
        enabled=False,
        notes="Motor Tokko publico para ventas en Hurlingham; primera pagina HTML y resto por AJAX p=N.",
    )
    detail_patterns = (r"/p/\d+-",)
    require_target_text = True
    tokko_ajax_path = "/Buscar-propiedades-en-Venta-en-Hurlingham-25973"
    fallback_max_pages = 12

    def parse(self, url):
        data = super().parse(url)
        if not data:
            return None
        soup = self.soup(url)
        text = visible_text(soup)
        if not re.search(r"INFORMACI(?:O|Ó)N\s+B(?:A|Á)SICA|SUPERFICIES\s+Y\s+MEDIDAS", text, re.I):
            return data
        labels = {
            "rooms": [r"Ambientes"],
            "bedrooms": [r"Dormitorios"],
            "bathrooms": [r"Ba(?:ñ|n)os"],
            "toilets": [r"Toilettes?"],
            "garages": [r"Cocheras?"],
            "age_text": [r"Antig(?:ü|u)edad"],
            "condition": [r"Condici(?:o|ó)n"],
            "floors_text": [r"Plantas"],
            "land_area": [r"Terreno"],
            "covered_area": [r"Cubierta"],
            "uncovered_area": [r"Descubierta"],
            "semicovered_area": [r"Semicubierta"],
            "total_area": [r"Total\s+Construido"],
            "front_width": [r"Frente"],
            "lot_depth": [r"Fondo"],
        }
        fields = parse_labeled_fields(text, labels)
        if "age_text" in fields:
            age = 0 if re.search(r"a\s*estrenar", fields["age_text"], re.I) else parse_int(fields["age_text"])
            evidence_set(data, "age_years", age, "analia_basic_info", fields["age_text"])
        if "floors_text" in fields:
            evidence_set(data, "building_floors", parse_int(fields["floors_text"]), "analia_basic_info", fields["floors_text"])
        apply_detail_fields(data, fields, "analia_detail_tables")
        address = text_value(
            text,
            [
                r"(?:Direcci(?:o|ó)n)\s*:?\s*(.+?)(?:\s+Ubicaci(?:o|ó)n|\s+Agua|\s+INFORMACI)",
                r"(?:Ubicaci(?:o|ó)n)\s*:?\s*(.+?)(?:\s+Agua|\s+INFORMACI)",
            ],
        )
        if address:
            cleaned_address = clean_detected_address(address)
            data["address"] = canonical_address_alias(cleaned_address)[:250]
        data["location_precision"] = classify_address_precision(data.get("address"))
        data["raw_data"] = data.get("raw_data") or {}
        data["raw_data"]["analia_fields"] = fields
        return data


class MarceloRussoScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="marcelo-russo",
        name="Marcelo Russo Propiedades",
        base_url="https://marcelorussoprop.com.ar",
        search_url="https://marcelorussoprop.com.ar/venta/",
        crawl_delay=3,
        enabled=False,
        notes="WordPress/RealHomes local con fichas /property/ y datos completos.",
    )
    detail_patterns = (r"/property/[^/]+/?$",)
    fallback_max_pages = 40

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return f"{self.definition.search_url.rstrip('/')}/page/{page}/"

    def _is_target_property_url(self, url):
        path = urlparse(url).path.lower()
        return bool(re.search(r"/property/[^/]*(?:hurlingham|tesei|morris)", path))

    def _listing_urls(self, soup):
        seen = set()
        for url in links_matching(self, soup, self.detail_patterns):
            if self._is_target_property_url(url) and url not in seen:
                seen.add(url)
                yield url

        base = self.definition.base_url.rstrip("/")
        markup = str(soup)
        for match in re.finditer(
            rf"(?:{re.escape(base)})?/property/[^\"'<>\s\\]+/?",
            markup,
            re.I,
        ):
            url = self.absolute(match.group(0).split("#")[0])
            if self._is_target_property_url(url) and url not in seen:
                seen.add(url)
                yield url

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls,
            fallback_max_pages=self.fallback_max_pages,
        )

    def parse(self, url):
        soup = self.soup(url)
        data = basic_html_data(soup, url)
        text = visible_text(soup)
        data["agency"] = self.definition.name
        data["images"] = data.get("images") or absolute_images(self, soup)

        title = soup.select_one("h1") or soup.select_one("title")
        if title:
            data["title"] = clean_text(title.get_text(" ", strip=True))
        address_node = soup.select_one(".rh_page__property .rh_page__property_address")
        if address_node:
            data["address"] = clean_text(address_node.get_text(" ", strip=True))

        page_text_node = soup.select_one(".rh_page__property")
        page_text = visible_text(page_text_node or soup)
        currency, price = price_near_label(page_text)
        if price is not None:
            data["currency"] = currency
            data["price"] = price

        meta = {}
        for node in soup.select(".rh_property__meta"):
            parts = [clean_text(part) for part in node.stripped_strings if clean_text(part)]
            if len(parts) >= 2:
                meta[parts[0].lower()] = parts[1]
        data["rooms"] = None
        data["bedrooms"] = plausible_int(meta.get("habitaciones"), 12)
        data["bathrooms"] = parse_decimal(meta.get("cuartos de baño") or meta.get("cuartos de bano"))
        data["garages"] = plausible_int(meta.get("garaje") or meta.get("garage"), 8)
        data["covered_area"] = parse_decimal(meta.get("área") or meta.get("area"))
        data["land_area"] = parse_decimal(meta.get("tamaño del lote") or meta.get("tamano del lote"))
        data["total_area"] = None

        content = soup.select_one(".rh_content")
        data["description"] = visible_text(content) if content else data.get("description") or ""
        data["locality"] = (
            "William C. Morris"
            if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I)
            else "Villa Tesei"
            if re.search(r"villa\s+(?:santos\s+)?tesei|ciudad\s+tesei", text, re.I)
            else "Hurlingham"
        )
        data["property_type"] = infer_property_type(data.get("title"), data.get("description"))
        data["location_precision"] = classify_address_precision(data.get("address"))
        if "raw_data" not in data:
            data["raw_data"] = {}
        data["raw_data"]["parsed_meta"] = meta
        data["status"] = Property.Status.ACTIVE
        return data


class LopezCombaScraper(TokkoSearchScraper):
    definition = SourceDefinition(
        slug="lopez-comba",
        name="Lopez Comba Propiedades",
        base_url="https://www.lopezcomba.ar",
        search_url="https://www.lopezcomba.ar/Buscar?operation=1&locations=25973&o=2,2&1=1",
        crawl_delay=3,
        enabled=False,
        notes="Motor Tokko publico para ventas en Hurlingham; primera pagina HTML y resto por AJAX p=N.",
    )
    detail_patterns = (r"/p/\d+-",)
    require_target_text = True
    fallback_max_pages = 5


class RiquelmeScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="riquelme",
        name="Riquelme Propiedades",
        base_url="https://www.riquelmepropiedades.com.ar",
        search_url="https://www.riquelmepropiedades.com.ar/buscar/?operation=1&type=&bedrooms=&priceFrom=&priceTo=&page=0&view=list&date-from=&date-to=&occupancy=&currency=&zone1=2&zone2=196&zone3=",
        crawl_delay=3,
        enabled=False,
        notes="Busqueda publica de ventas en partido de Hurlingham con page=N cero-based.",
    )
    detail_patterns = (r"/propiedad/",)

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return (
            f"{self.definition.base_url}/buscar/?operation=1&type=&bedrooms=&priceFrom="
            f"&priceTo=&page={page - 1}&view=list&date-from=&date-to=&occupancy="
            f"&currency=&zone1=2&zone2=196&zone3="
        )

    def _listing_urls(self, soup):
        container = soup.select_one(".searchpage-results") or soup
        yield from links_matching(self, container, self.detail_patterns)

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls,
            fallback_max_pages=20,
        )

    def parse(self, url):
        soup = self.soup(url)
        data = super().parse(url)
        if not data:
            return None
        text = visible_text(soup)
        status_badge = self._status_badge(soup, text)
        raw_data = data.setdefault("raw_data", {})
        if status_badge:
            raw_data["riquelme_status_badge"] = status_badge
            if status_badge == "reserved":
                data["status"] = Property.Status.RESERVED
                data["source_status"] = "reserved"
            elif status_badge == "suspended":
                data["status"] = Property.Status.SUSPENDED
                data["source_status"] = "suspended"
            elif status_badge == "sold":
                data["status"] = Property.Status.SOLD
                data["source_status"] = "sold"
            if data.get("currency") == "ARS" and not data.get("price"):
                data["currency"] = ""
        else:
            data["source_status"] = ""
        title = self._detail_title(soup)
        if title:
            data["title"] = title[:300]
        address = self._detail_address(soup, text)
        if address:
            data["address"] = address[:250]
            data["detected_address"] = address[:250]
        else:
            data["address"] = ""
            data["detected_address"] = ""
            raw_data["riquelme_address_untrusted"] = True
        detail_fields = parse_labeled_fields(
            text,
            {
                "covered_area": [r"Sup\.?\s*Cubierta", r"Cubierta"],
                "land_area": [r"Terreno", r"Lote"],
            },
        )
        if detail_fields:
            raw_data["riquelme_detail_fields"] = detail_fields
        data["locality"] = (
            "William C. Morris"
            if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I)
            else "Villa Tesei"
            if re.search(r"villa\s+(?:santos\s+)?tesei", text, re.I)
            else "Hurlingham"
        )
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data

    def _status_badge(self, soup, text):
        status_nodes = " ".join(
            clean_text(node.get_text(" ", strip=True))
            for node in soup.select(
                ".btn-danger, .search-item-status, [class*='reserved'], [class*='suspended'], [class*='sold']"
            )
        )
        status_markup = " ".join(
            " ".join(node.get("class") or [])
            for node in soup.select(
                ".btn-danger, .search-item-status, [class*='reserved'], [class*='suspended'], [class*='sold']"
            )
        )
        status_folded = fold_text(f"{status_nodes} {status_markup}")
        if re.search(r"vendid|search-item-sold", status_folded, re.I):
            return "sold"
        if re.search(r"reservad|search-item-reserved", status_folded, re.I):
            return "reserved"
        if re.search(r"suspendido|no\s+disponible|search-item-suspended", status_folded, re.I):
            return "suspended"
        return ""

    def _detail_title(self, soup):
        for selector in ("h1", ".property-title", ".property-description h3", ".detail-title"):
            node = soup.select_one(selector)
            if not node:
                continue
            title = clean_text(node.get_text(" ", strip=True))
            if title and not re.search(r"buscador\s+de\s+propiedades", title, re.I):
                return title
        return ""

    def _detail_address(self, soup, text):
        nodes = soup.select(".property-description, .property-detail, .property-info, main")
        scoped_text = visible_text(nodes[0]) if nodes else text[:3000]
        candidate = text_value(
            scoped_text,
            [
                r"(?:Direcci(?:o|ó)n|Ubicaci(?:o|ó)n)\s*:?\s*(.+?)(?:\s+Ambientes|\s+Dormitorios|\s+Ba(?:ñ|n)os|\s+Superficie|\s+Precio|\s+Descripción)",
            ],
        )
        if candidate:
            candidate = clean_detected_address(candidate)
            folded = fold_text(candidate)
            if (
                candidate
                and re.search(r"\d{2,5}", candidate)
                and not ADDRESS_FALSE_POSITIVE_RE.search(candidate)
                and not re.search(r"\bcasa\b|refaccion|proxima|estacion|ambientes?", folded, re.I)
            ):
                return candidate
        return ""


class FincasScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="fincas",
        name="Fincas Bienes Raíces",
        base_url="https://www.haurie.argencasas.com",
        search_url="https://www.haurie.argencasas.com/propiedades",
        crawl_delay=3,
        enabled=False,
        notes="Fuente local cuyo catalogo publico esta alojado en haurie.argencasas.com.",
    )
    detail_patterns = (r"/propiedad-[^/]+-\d+-\d+",)
    require_target_text = True

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return f"{self.definition.search_url}?page={page}"

    def _listing_urls(self, soup):
        yield from links_matching(self, soup, self.detail_patterns)

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls,
            fallback_max_pages=15,
        )

    def parse(self, url):
        soup = self.soup(url)
        data = super().parse(url)
        if not data:
            return None
        text = visible_text(soup)
        structured_address = self._structured_address(soup, text)
        if structured_address:
            data["address"] = structured_address[:250]
            data["detected_address"] = structured_address[:250]
            data["raw_data"] = data.get("raw_data") or {}
            data["raw_data"]["haurie_address"] = structured_address
        data["rooms"] = number_before_label(text, [r"Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = number_before_label(text, [r"Dormitorios"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = number_before_label(text, [r"Baños", r"Banos"], parse_decimal) or data.get("bathrooms")
        data["covered_area"] = number_before_label(text, [r"Sup\s*Cubierta"]) or data.get("covered_area")
        data["total_area"] = number_before_label(text, [r"Sup\s*Total"]) or data.get("total_area")
        data["land_area"] = data["total_area"] or data.get("land_area")
        currency, price = price_near_label(text)
        if price is not None:
            data["currency"] = currency
            data["price"] = price
        free_area = number_before_label(text, [r"Sup\s*Libre"])
        data["raw_data"] = data.get("raw_data") or {}
        if free_area is not None:
            data["raw_data"]["free_area"] = str(free_area)
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data

    def _structured_address(self, soup, text):
        selectors = (
            "[itemprop='streetAddress']",
            ".item-address",
            ".property-address",
            ".property-location",
            ".ubicacion",
            ".direccion",
            "address",
            "h1",
            "h2",
        )
        for selector in selectors:
            for node in soup.select(selector):
                address = normalize_argencasas_address(node.get_text(" ", strip=True))
                if address:
                    return address
        return extract_argencasas_address_from_text(text)


class GuarnieriScraper(MultiSearchScraper):
    definition = SourceDefinition(
        slug="guarnieri",
        name="Guarnieri Propiedades",
        base_url="https://guarnieripropiedades.com.ar",
        search_url="https://guarnieripropiedades.com.ar/inmobiliaria/busqueda-avanzada?keyword=&status%5B%5D=en-venta&location%5B%5D=hurlingham&bathrooms=&garage=&min-area=&property_id=&max-area=&bedrooms=&currency=&min-price=&max-price=&nc2ba-de-plantas=&ambientes=&antigc3bcedad=",
        crawl_delay=3,
        enabled=False,
        notes="Busqueda avanzada publica de ventas en Hurlingham con paginacion /page/N.",
    )
    detail_patterns = (r"/inmobiliaria/propiedad/",)

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        query = self.definition.search_url.split("?", 1)[1]
        return f"{self.definition.base_url}/inmobiliaria/busqueda-avanzada/page/{page}?{query}"

    def _listing_urls(self, soup):
        container = soup.select_one(".listing-view") or soup
        yield from links_matching(self, container, self.detail_patterns)

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls,
            fallback_max_pages=40,
        )

    def parse(self, url):
        soup = self.soup(url)
        data = super().parse(url)
        if not data:
            return None
        text = visible_text(soup)
        detail_start = re.search(r"DATOS DE LA PROPIEDAD|DESCRIPCIÓN|DESCRIPCION", text, re.I)
        detail_text = text[detail_start.start():] if detail_start else text
        detail_text = re.split(
            r"Propiedades\s+Sugeridas|Propiedades\s+similares|Tambien\s+puede\s+interesarte|También\s+puede\s+interesarte",
            detail_text,
            flags=re.I,
        )[0]
        data["operation"] = detect_operation(detail_text, url)
        price_match = re.search(r"(?:Precio\s*:?\s*)?(U\$S|US\$|USD|ARS|\$)\s*([\d.]+(?:,\d+)?)", detail_text, re.I)
        if price_match:
            data["currency"] = normalize_currency(price_match.group(1))
            data["price"] = parse_decimal(price_match.group(2))
        metrics_text = detail_text[price_match.start():] if price_match else detail_text
        metric_block_match = re.search(r"Precio\s*:.*?(?:CARACTER[ÍI]STICAS|UBICACI[ÓO]N|$)", detail_text, re.I)
        metric_block = metric_block_match.group(0) if metric_block_match else metrics_text
        data["rooms"] = label_before_number(detail_text, [r"Amb\.?", r"Ambientes"], parse_int) or number_before_label(detail_text, [r"Amb\.?", r"Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = label_before_number(detail_text, [r"Dormitorios"], parse_int) or number_before_label(detail_text, [r"Dormitorios"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = number_before_label(detail_text, [r"Baños", r"Banos"], parse_decimal) or label_before_number(detail_text, [r"Baños", r"Banos"], parse_decimal) or data.get("bathrooms")
        data["garages"] = number_before_label(detail_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int) or label_before_number(detail_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int) or data.get("garages")
        data["rooms"] = label_before_number(metrics_text, [r"Amb\.?", r"Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = label_before_number(metrics_text, [r"Dormitorios"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = label_before_number(metrics_text, [r"Baños", r"Banos"], parse_decimal) or data.get("bathrooms")
        data["garages"] = label_before_number(metrics_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int) or data.get("garages")
        data["rooms"] = text_value(detail_text, [r"(\d+)\s*Amb\.?", r"(\d+)\s*Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = text_value(metric_block, [r"Dormitorios?\s*:?\s*(\d+)"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = text_value(metric_block, [r"Ba(?:ñ|n)o?s?\s*:?\s*(\d+(?:[.,]\d+)?)"], parse_decimal) or data.get("bathrooms")
        data["garages"] = text_value(metric_block, [r"Garages?\s*:?\s*(\d+)", r"Cocheras?\s*:?\s*(\d+)"], parse_int) or data.get("garages")
        data["covered_area"] = label_before_number(detail_text, [r"Sup\.\s*Cubierta", r"Sup\s*Cubierta"]) or data.get("covered_area")
        data["land_area"] = label_before_number(detail_text, [r"Sup\.\s*Terreno", r"Sup\s*Terreno"]) or data.get("land_area")
        if data.get("land_area"):
            data["total_area"] = data["land_area"]
        address = text_value(
            detail_text,
            [r"Dirección\s*:?\s*(.+?)(?:Ciudad\s*:|Barrio\s*:|Propiedades Sugeridas|CARACTERÍSTICAS|CARACTERISTICAS)"],
        )
        if address:
            parsed_address = clean_detected_address(address)
            if parsed_address and (not data.get("address") or not is_plausible_property_address(data.get("address"))):
                data["address"] = parsed_address[:250]
        city = text_value(detail_text, [r"Ciudad\s*:?\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .]+?)(?:Barrio\s*:|Propiedades|$)"])
        if city:
            data["locality"] = clean_text(city)
        neighborhood = text_value(detail_text, [r"Barrio\s*:?\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .]+?)(?:Propiedades|$)"])
        if neighborhood:
            data["neighborhood"] = clean_text(neighborhood)
        unit_offers = parse_multi_unit_offers(detail_text)
        multi_unit_context = bool(
            re.search(
                r"\b(unidad|unidades|departamentos?|dptos?|monoambientes?)\b",
                f"{title} {detail_text[:1400]}",
                re.I,
            )
        )
        if unit_offers and multi_unit_context:
            cheapest = min(unit_offers, key=lambda item: parse_decimal(item["price"]))
            data["currency"] = cheapest["currency"]
            data["price"] = parse_decimal(cheapest["price"])
            data["rooms"] = cheapest["rooms"]
            data["bedrooms"] = None
            data["bathrooms"] = None
            data["garages"] = None
            data["covered_area"] = None
            data["total_area"] = parse_decimal(cheapest["total_area"])
            data["land_area"] = None
            data["source_status"] = "multi_unit"
            data["raw_data"] = data.get("raw_data") or {}
            data["raw_data"]["multi_unit"] = True
            data["raw_data"]["unit_offers"] = unit_offers
            data["raw_data"]["unit_count"] = len(unit_offers)
            garage_prices = re.findall(
                r"(?:cocheras?.*?desde|cocheras?.*?opcionales.*?)\s*:?\s*(?:u\$s|usd|us\$)\s*([\d.,]+).*?(?:a|-)\s*(?:u\$s|usd|us\$)\s*([\d.,]+)",
                detail_text,
                re.I,
            )
            if garage_prices:
                data["raw_data"]["optional_garage_price_range"] = list(garage_prices[0])
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data

    def parse(self, url):
        soup = self.soup(url)
        root = soup.select_one(".elementor-location-single.property") or soup
        title_node = root.select_one("h1") or soup.select_one("h1") or soup.select_one("title")
        title = title_node.get_text(" ", strip=True) if title_node else "Propiedad"
        header_address_node = (
            root.select_one(".property-title-wrap .item-address")
            or root.select_one(".property-header-wrap .item-address")
            or root.select_one("address.item-address")
        )
        header_address = (
            clean_detected_address(header_address_node.get_text(" ", strip=True))
            if header_address_node
            else ""
        )
        header_neighborhood = (
            extract_embedded_neighborhood(header_address_node.get_text(" ", strip=True))
            if header_address_node
            else ""
        )
        page_text = split_suggested_text(visible_text(root))
        detail_start = re.search(r"DATOS DE LA PROPIEDAD", page_text, re.I)
        detail_text = page_text[detail_start.start():] if detail_start else page_text
        details = self._detail_pairs(root)
        overview_text = clean_text(
            " ".join(
                node.get_text(" ", strip=True)
                for node in root.select(".property-overview-data")
            )
        )
        data = {
            "external_id": external_id_from_url(url),
            "url": url,
            "title": title,
            "description": "",
            "property_type": infer_property_type(title, detail_text[:900]),
            "currency": "",
            "price": None,
            "features": [],
            "status": Property.Status.ACTIVE,
            "agency": "Guarnieri Propiedades",
            "locality": "Hurlingham",
            "operation": detect_operation(detail_text, url),
            "images": self._main_images(root),
            "raw_data": {
                "guarnieri_header_address": header_address,
                "guarnieri_detail_pairs": details,
            },
        }
        if header_address:
            data["address"] = header_address
        if header_neighborhood:
            data["neighborhood"] = header_neighborhood

        description_node = root.select_one(".property-description-wrap .block-content-wrap")
        if description_node:
            data["description"] = clean_text(description_node.get_text(" ", strip=True))
        currency, price = price_from_guarnieri_text(detail_text)
        if price is not None:
            data["currency"] = currency
            data["price"] = price

        self._apply_guarnieri_details(data, details)
        self._apply_guarnieri_text_fallbacks(data, detail_text, overview_text, bool(details))

        address = text_value(
            detail_text,
            [r"Direcci(?:ó|o)n\s*:?\s*(.+?)(?:Ciudad\s*:|Barrio\s*:|Propiedades Sugeridas|CARACTER|$)"],
        )
        if address:
            embedded_neighborhood = extract_embedded_neighborhood(address)
            parsed_address = clean_detected_address(address)
            if parsed_address and (not data.get("address") or not is_plausible_property_address(data.get("address"))):
                data["address"] = parsed_address[:250]
            if embedded_neighborhood and not data.get("neighborhood"):
                data["neighborhood"] = embedded_neighborhood
        city = text_value(detail_text, [r"Ciudad\s*:?\s*([A-Za-zÀ-ɏ .]+?)(?:Barrio\s*:|Propiedades|$)"])
        if city:
            data["locality"] = normalize_locality(city)
        neighborhood = text_value(detail_text, [r"Barrio\s*:?\s*([A-Za-zÀ-ɏ .]+?)(?:Propiedades|$)"])
        if neighborhood:
            data["neighborhood"] = normalize_neighborhood_name(neighborhood) or clean_text(neighborhood)

        unit_offers = parse_multi_unit_offers(detail_text)
        multi_unit_context = bool(
            re.search(
                r"\b(unidad|unidades|departamentos?|dptos?|monoambientes?)\b",
                f"{title} {detail_text[:1400]}",
                re.I,
            )
        )
        if unit_offers and multi_unit_context:
            cheapest = min(unit_offers, key=lambda item: parse_decimal(item["price"]))
            data["currency"] = cheapest["currency"]
            data["price"] = parse_decimal(cheapest["price"])
            data["rooms"] = cheapest["rooms"]
            data["bedrooms"] = None
            data["bathrooms"] = None
            data["garages"] = None
            data["covered_area"] = None
            data["total_area"] = parse_decimal(cheapest["total_area"])
            data["land_area"] = None
            data["source_status"] = "multi_unit"
            data["raw_data"] = data.get("raw_data") or {}
            data["raw_data"]["multi_unit"] = True
            data["raw_data"]["unit_offers"] = unit_offers
            data["raw_data"]["unit_count"] = len(unit_offers)
        elif unit_offers:
            data["raw_data"] = data.get("raw_data") or {}
            data["raw_data"]["unit_offer_candidates"] = unit_offers
            data["raw_data"]["unit_offer_candidates_ignored"] = True

        self._mark_guarnieri_metric_conflicts(data, detail_text, unit_offers if not multi_unit_context else [])

        if data.get("land_area") and not data.get("total_area"):
            data["total_area"] = data["land_area"]
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data

    def _detail_pairs(self, root):
        pairs = {}
        for item in root.select(
            ".property-detail-wrap.property-section-wrap .list-lined-item, "
            ".detail-wrap .list-lined-item, "
            ".property-address-wrap .list-lined-item"
        ):
            label_node = item.select_one("strong")
            value_node = item.select_one("span")
            if not label_node or not value_node:
                continue
            label = normalized_label(label_node.get_text(" ", strip=True))
            value = clean_text(value_node.get_text(" ", strip=True))
            if label and value:
                pairs[label] = value
        return pairs

    def _main_images(self, root):
        images = []
        gallery_nodes = root.select(".property-banner, .hs-gallery-v4-grid, .property-top-wrap")
        if not gallery_nodes:
            gallery_nodes = [root]
        for gallery in gallery_nodes:
            for image in gallery.select("img[src], img[data-src], img[data-lazy-src]"):
                src = image.get("data-src") or image.get("data-lazy-src") or image.get("src")
                if not src or src.startswith("data:"):
                    continue
                images.append(self.absolute(src))
        return list(dict.fromkeys(images))[:30]

    def _apply_guarnieri_details(self, data, details):
        for label, value in details.items():
            if label.startswith("precio"):
                currency, price = price_from_guarnieri_text(value)
                if price is not None:
                    data["currency"] = currency
                    data["price"] = price
            elif label.startswith("sup cubierta"):
                data["covered_area"] = parse_area_value(value)
            elif label.startswith("sup terreno"):
                area, front, depth = parse_dimension_value(value)
                if area is not None:
                    data["land_area"] = area
                if front is not None:
                    data["front_width"] = front
                if depth is not None:
                    data["lot_depth"] = depth
            elif label.startswith("dormitorio") or label.startswith("habitacion"):
                data["bedrooms"] = parse_int(value)
            elif label.startswith("bano"):
                data["bathrooms"] = parse_decimal(value)
            elif label.startswith("garages") or label.startswith("garage"):
                garages = parse_garage_value(value)
                if garages is not None:
                    data["garages"] = garages
            elif label.startswith("tipo"):
                data["property_type"] = infer_property_type(value, data.get("title"))
            elif label.startswith("estado"):
                data["operation"] = detect_operation(value, data.get("url") or "")
            elif label.startswith("direccion"):
                embedded_neighborhood = extract_embedded_neighborhood(value)
                if embedded_neighborhood and not data.get("neighborhood"):
                    data["neighborhood"] = embedded_neighborhood
                address = clean_detected_address(value)
                if address:
                    data["address"] = address[:250]
            elif label.startswith("ciudad"):
                data["locality"] = normalize_locality(value)
            elif label.startswith("barrio"):
                data["neighborhood"] = normalize_neighborhood_name(value) or clean_text(value)
            else:
                embedded_neighborhood = extract_embedded_neighborhood(value)
                if embedded_neighborhood and not data.get("neighborhood"):
                    data["neighborhood"] = embedded_neighborhood

    def _mark_guarnieri_metric_conflicts(self, data, detail_text, unit_offers=None):
        details = (data.get("raw_data") or {}).get("guarnieri_detail_pairs") or {}
        if not details:
            return
        conflicts = []

        structured_price = data.get("price")
        if structured_price is not None:
            for currency, raw_price in re.findall(r"(U\$S|U\$D|US\$|USD|ARS|\$)\s*([\d.,]+)", detail_text or "", re.I):
                parsed = parse_decimal(raw_price)
                if parsed is not None and str(parsed) != str(structured_price):
                    conflicts.append(
                        {
                            "field": "price",
                            "structured": str(structured_price),
                            "text": str(parsed),
                            "evidence": f"{currency} {raw_price}",
                        }
                    )

        text_covered = text_value(
            detail_text,
            [r"SUP\s*CUBIERTA\s*:?\s*([\d.,]+)\s*(?:m2|m²|mts)?"],
            parse_area_value,
        )
        if text_covered is not None and data.get("covered_area") is not None and str(text_covered) != str(data.get("covered_area")):
            conflicts.append(
                {
                    "field": "covered_area",
                    "structured": str(data.get("covered_area")),
                    "text": str(text_covered),
                    "evidence": "SUP CUBIERTA en descripcion",
                }
            )

        text_land = text_value(
            detail_text,
            [
                r"SOBRE\s+LOTE.*?\(([\d.,]+)\s*(?:m2|m²|m²)\)",
                r"LOTE.*?\(([\d.,]+)\s*(?:m2|m²|m²)\)",
            ],
            parse_area_value,
        )
        if text_land is not None and data.get("land_area") is not None and str(text_land) != str(data.get("land_area")):
            conflicts.append(
                {
                    "field": "land_area",
                    "structured": str(data.get("land_area")),
                    "text": str(text_land),
                    "evidence": "LOTE en descripcion",
                }
            )

        for offer in unit_offers or []:
            parsed = parse_decimal(offer.get("price"))
            if parsed is not None and structured_price is not None and str(parsed) != str(structured_price):
                conflicts.append(
                    {
                        "field": "price",
                        "structured": str(structured_price),
                        "text": str(parsed),
                        "evidence": "oferta parcial ignorada",
                    }
                )

        if conflicts:
            data["source_status"] = "metric_conflict_review"
            data["raw_data"] = data.get("raw_data") or {}
            data["raw_data"]["guarnieri_metric_conflicts"] = conflicts

    def _apply_guarnieri_text_fallbacks(self, data, detail_text, overview_text, has_structured_details=False):
        data["rooms"] = (
            label_before_number(detail_text, [r"Amb\.?", r"Ambientes"], parse_int)
            or number_before_label(detail_text, [r"Amb\.?", r"Ambientes"], parse_int)
            or text_value(overview_text, [r"(\d+)\s*Amb\.?", r"(\d+)\s*Ambientes"], parse_int)
            or data.get("rooms")
        )
        if data.get("bedrooms") is None:
            data["bedrooms"] = (
                label_before_number(detail_text, [r"Dormitorios"], parse_int)
                or number_before_label(detail_text, [r"Dormitorios"], parse_int)
                or text_value(detail_text, [r"Dormitorios?\s*:?\s*(\d+)"], parse_int)
            )
        if data.get("bathrooms") is None:
            data["bathrooms"] = (
                number_before_label(detail_text, [r"Baños", r"Baños", r"Banos"], parse_decimal)
                or label_before_number(detail_text, [r"Baños", r"Baños", r"Banos"], parse_decimal)
                or text_value(detail_text, [r"Ba(?:ñ|n)o?s?\s*:?\s*(\d+(?:[.,]\d+)?)"], parse_decimal)
            )
        if data.get("bathrooms") is None:
            data["bathrooms"] = (
                number_before_label(detail_text, [r"Baños"], parse_decimal)
                or label_before_number(detail_text, [r"Baños"], parse_decimal)
                or text_value(detail_text, [r"Baños?\s*:?\s*(\d+(?:[.,]\d+)?)"], parse_decimal)
            )
        if data.get("garages") is None:
            data["garages"] = (
                label_before_number(detail_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int)
                or number_before_label(detail_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int)
            )
        if data.get("covered_area") is None:
            data["covered_area"] = label_before_number(
                detail_text,
                [r"Sup\.\s*Cubierta", r"Sup\s*Cubierta", r"Superficie\s+Cubierta"],
            )
        if data.get("land_area") is None:
            raw_land = text_value(
                detail_text,
                [r"Sup\.?\s*Terreno\s*:?\s*([\d.,]+\s*(?:x|×|por)\s*[\d.,]+|[\d.,]+)\s*(?:m2|m²|mts)?"],
            )
            if not raw_land:
                raw_land = text_value(
                    detail_text,
                    [r"Terreno\s*:?\s*([\d.,]+\s*(?:x|por)\s*[\d.,]+|[\d.,]+)\s*(?:m2|m²|mts)?"],
                )
            if raw_land:
                area, front, depth = parse_dimension_value(raw_land)
                data["land_area"] = area
                if front is not None:
                    data["front_width"] = front
                if depth is not None:
                    data["lot_depth"] = depth
        precise_rooms = text_value(
            detail_text,
            [r"(?<!\d)(\d+)\s*amb(?:\.|\b)", r"(?<!\d)(\d+)\s*ambientes?"],
            parse_int,
        )
        if precise_rooms is not None:
            data["rooms"] = precise_rooms
        precise_bedrooms = text_value(
            detail_text,
            [r"(?<!\d)(\d+)\s*dormitorios?", r"Dormitorios?\s*:?\s*(\d+)"],
            parse_int,
        )
        if precise_bedrooms is not None and (not has_structured_details or data.get("bedrooms") is None):
            data["bedrooms"] = precise_bedrooms
        precise_bathrooms = text_value(
            detail_text,
            [
                r"(?<!\d)(\d+(?:[.,]\d+)?)\s*ba(?:ñ|n)os?\b",
                r"Ba(?:ñ|n)os?\s*:?\s*(\d+(?:[.,]\d+)?)",
            ],
            parse_decimal,
        )
        if precise_bathrooms is not None and (not has_structured_details or data.get("bathrooms") is None):
            data["bathrooms"] = precise_bathrooms
        precise_garages = text_value(
            detail_text,
            [
                r"(?<!\d)(\d+)\s*(?:Garages?|Garajes?|Cocheras?)\b",
                r"(?:Garages?|Garajes?|Cocheras?)\s*:?\s*(\d+)\b",
            ],
            parse_int,
        )
        if precise_garages is not None and 0 <= precise_garages <= 8 and (not has_structured_details or data.get("garages") is None):
            data["garages"] = precise_garages
        if data.get("rooms") is not None and data["rooms"] > 20:
            data["rooms"] = None
        if data.get("garages") is not None and data["garages"] > 20:
            data["garages"] = None


class InmueblesClarinScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="inmuebles-clarin",
        name="Inmuebles Clarín",
        base_url="https://www.inmuebles.clarin.com",
        search_url="https://www.inmuebles.clarin.com/casas/venta/hurlingham-hurlingham",
        crawl_delay=4,
        enabled=False,
        notes="Duplicado de Argenprop, no recomendado. Mantener fuera de scrape --all; usar solo manualmente para contrastar cobertura.",
    )
    detail_patterns = (r"/(?:casa|departamento|ph|terreno)-en-venta", r"/\d+--")

    fallback_max_pages = 80

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return f"{self.definition.search_url}?pagina-{page}"

    def _listing_urls_from_soup(self, soup):
        yield from links_matching(self, soup, self.detail_patterns, require_target_text=True)

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls_from_soup,
            fallback_max_pages=self.fallback_max_pages,
        )

    def parse(self, url):
        data = super().parse(url)
        if not data:
            return None
        soup = self.soup(url)
        text = visible_text(soup)
        metrics = {
            "rooms": first_present(
                text_value(text, [r"(\d+)\s*ambientes"], parse_int),
                value_after_label(text, [r"Ambientes?"], parse_int, unit_pattern=""),
            ),
            "bedrooms": first_present(
                text_value(text, [r"(\d+)\s*dormitorios?"], parse_int),
                value_after_label(text, [r"Dormitorios?"], parse_int, unit_pattern=""),
            ),
            "bathrooms": first_present(
                text_value(text, [r"(\d+(?:[.,]\d+)?)\s*ba(?:ñ|n)os?"], parse_decimal),
                value_after_label(text, [r"Ba(?:ñ|n)os?"], parse_decimal, unit_pattern=""),
            ),
            "garages": first_present(
                text_value(text, [r"(\d+)\s*cocheras?"], parse_int),
                value_after_label(text, [r"Cocheras?"], parse_int, unit_pattern=""),
            ),
            "covered_area": first_present(
                text_value(text, [r"([\d.,]+)\s*m(?:2|²)?\s*Cubierta"], parse_decimal),
                value_after_label(text, [r"Superficie\s+Cubierta", r"Sup\.?\s*Cubierta"], parse_decimal),
            ),
            "total_area": first_present(
                text_value(text, [r"([\d.,]+)\s*m(?:2|²)?\s*Totales"], parse_decimal),
                value_after_label(text, [r"Superficie\s+Total", r"Sup\.?\s*Total", r"Totales?"], parse_decimal),
            ),
            "land_area": first_present(
                text_value(text, [r"([\d.,]+)\s*m(?:2|²)?\s*Terreno"], parse_decimal),
                value_after_label(text, [r"Superficie\s+Terreno", r"Sup\.?\s*Terreno", r"Terreno"], parse_decimal),
            ),
        }
        for field, value in metrics.items():
            evidence_set(data, field, value, "inmuebles_clarin_metrics")
        return data


class PatagonPropScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="patagonprop",
        name="PatagonProp",
        base_url="https://patagonprop.com",
        search_url="https://patagonprop.com/buscar/inmuebles-venta-en_hurlingham_hurlingham_buenos_aires_1_2_196_46-from_0",
        crawl_delay=4,
        enabled=False,
        notes="Portal con paginacion por offset from_N, equivalente a Mapaprop.",
    )
    detail_patterns = (r"/property/", r"/propiedad/")
    page_size = 12
    status_badges = {
        "vendida": (Property.Status.SOLD, "sold"),
        "vendido": (Property.Status.SOLD, "sold"),
        "reservada": (Property.Status.RESERVED, "reserved"),
        "reservado": (Property.Status.RESERVED, "reserved"),
        "suspendida": (Property.Status.SUSPENDED, "suspended"),
        "suspendido": (Property.Status.SUSPENDED, "suspended"),
    }

    def _page_url(self, page):
        offset = (page - 1) * self.page_size
        return re.sub(r"from_\d+", f"from_{offset}", self.definition.search_url)

    def _listing_urls(self, soup):
        yield from links_matching(self, soup, self.detail_patterns)

    def _status_badge(self, text):
        match = re.search(
            r"\b(vendida|vendido|reservada|reservado|suspendida|suspendido)\b",
            text or "",
            re.I,
        )
        if not match:
            return None
        raw = match.group(1)
        status, source_status = self.status_badges[raw.lower()]
        return {"raw": raw, "status": status, "source_status": source_status}

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls,
            fallback_max_pages=40,
        )

    def parse(self, url):
        data = super().parse(url)
        if not data:
            return None
        soup = self.soup(url)
        text = visible_text(soup)
        status_badge = self._status_badge(text)
        labels = {
            "operation": [r"Operaci(?:o|ó)n"],
            "address": [r"Direcci(?:o|ó)n"],
            "location": [r"Ubicaci(?:o|ó)n"],
            "code": [r"C(?:o|ó)digo"],
            "property_type_text": [r"Tipo"],
            "total_area": [r"Superficie\s+total", r"Superficie"],
            "rooms": [r"Ambientes?"],
            "bedrooms": [r"Habitaciones?", r"Dormitorios?"],
            "bathrooms": [r"Ba(?:ñ|n)os?"],
            "garages": [r"Garages?", r"Cocheras?"],
        }
        fields = parse_labeled_fields(text, labels)
        if not any(fields.get(key) for key in ("operation", "address", "location", "code", "property_type_text")):
            return data
        if fields.get("address"):
            data["address"] = clean_detected_address(fields["address"])[:250]
        if fields.get("location"):
            location = clean_text(fields["location"])
            data["locality"] = (
                "William C. Morris"
                if re.search(r"william\s+(?:c\.\s*)?morris", location, re.I)
                else "Villa Tesei"
                if re.search(r"villa\s+(?:santos\s+)?tesei", location, re.I)
                else "Hurlingham"
                if re.search(r"hurlingham", location, re.I)
                else location.split(",", 1)[0]
            )
        if fields.get("operation"):
            data["operation"] = detect_operation(fields["operation"], url)
        if fields.get("property_type_text"):
            data["property_type"] = infer_property_type(fields["property_type_text"], data.get("title"))
        currency, price = price_near_label(text)
        if price is not None:
            data["currency"] = currency
            data["price"] = price
        if status_badge:
            data["status"] = status_badge["status"]
            data["source_status"] = status_badge["source_status"]
            if data.get("price") == parse_decimal("1"):
                data["price"] = None
        apply_detail_fields(data, fields, "patagonprop_detail")
        highlighted_area = text_value(text, [r"superficie\s+([\d.,]+)\s*m2"], parse_decimal)
        if highlighted_area is not None:
            evidence_set(data, "total_area", highlighted_area, "patagonprop_highlights")
            evidence_set(data, "land_area", highlighted_area, "patagonprop_highlights")
        if (
            not status_badge
            and data.get("price")
            and (
                data["price"] == parse_decimal("1")
                or (data.get("currency") == "ARS" and data["price"] < 1000000)
            )
        ):
            data["source_status"] = "price_age_review"
        data["raw_data"] = data.get("raw_data") or {}
        data["raw_data"]["patagonprop_fields"] = fields
        if status_badge:
            data["raw_data"]["patagonprop_status_badge"] = status_badge["raw"]
            data["raw_data"]["patagonprop_status_source"] = "detail_badge"
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data


class GABienesScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="ga-bienes",
        name="GA Bienes Inmuebles",
        base_url="https://www.gabienesinmuebles.com",
        search_url="https://www.gabienesinmuebles.com",
        crawl_delay=4,
        enabled=False,
        notes="Sitio propio parece renderizar propiedades por JS. Scraper parcial si expone HTML.",
    )
    detail_patterns = (r"/(?:propiedad|inmueble|venta)[^/]*",)
    require_target_text = True

    api_url = "https://www.gabienesinmuebles.com/api/properties"

    def _items(self):
        response = self.get(self.api_url)
        return response.json()

    def discover(self):
        for item in self._items():
            if not item.get("activo") or item.get("es_borrador"):
                continue
            if item.get("tipo_operacion") != "venta":
                continue
            text = f"{item.get('ubicacion') or ''} {item.get('descripcion') or ''}"
            if is_target_zone(text):
                yield f"{self.definition.base_url}/propiedad/{item['id']}"

    def parse(self, url):
        external_id = url.rstrip("/").rsplit("/", 1)[-1]
        item = next(
            (candidate for candidate in self._items() if str(candidate.get("id")) == external_id),
            None,
        )
        if not item:
            return None
        images = []
        try:
            images = json.loads(item.get("fotos") or "[]")
        except json.JSONDecodeError:
            images = []
        text = f"{item.get('ubicacion') or ''} {item.get('descripcion') or ''}"
        locality = (
            "William C. Morris"
            if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I)
            else "Villa Tesei"
            if re.search(r"villa\s+(?:santos\s+)?tesei", text, re.I)
            else "Hurlingham"
        )
        data = {
            "external_id": external_id,
            "url": url,
            "title": f"{(item.get('tipo_unidad') or 'Propiedad').title()} en {item.get('ubicacion') or locality}",
            "description": item.get("descripcion") or "",
            "address": item.get("ubicacion") or "",
            "locality": locality,
            "agency": self.definition.name,
            "property_type": infer_property_type(item.get("tipo_unidad"), item.get("descripcion")),
            "currency": normalize_currency(item.get("moneda") or ""),
            "price": parse_decimal(item.get("precio")),
            "rooms": item.get("cantidad_ambientes"),
            "bathrooms": parse_decimal(item.get("cantidad_banos")),
            "covered_area": parse_decimal(item.get("metros_cuadrados_cubiertos")),
            "total_area": parse_decimal(item.get("metros_cuadrados_totales")),
            "land_area": parse_decimal(item.get("metros_cuadrados_totales")),
            "age_years": item.get("antiguedad"),
            "features": ["Apto crédito"] if item.get("apto_credito") else [],
            "status": Property.Status.ACTIVE,
            "images": [self.absolute(image) for image in images if isinstance(image, str)][:30],
            "location_precision": classify_address_precision(item.get("ubicacion")),
            "raw_data": item,
        }
        return data


class PaulaFossatiScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="paula-fossati",
        name="Paula Fossati Estudio Inmobiliario",
        base_url="https://www.paulafossati.com.ar",
        search_url="https://www.paulafossati.com.ar/site/properties/sale",
        crawl_delay=4,
        enabled=False,
        notes="Fuente parcial encontrada por fichas aisladas en Villa Tesei.",
    )
    detail_patterns = (r"/site/properties/\d+/",)
    require_target_text = True

    def parse(self, url):
        data = super().parse(url)
        if not data:
            return None
        text = " ".join(
            [
                data.get("title") or "",
                data.get("description") or "",
                data.get("address") or "",
                json.dumps(data.get("raw_data") or {}, ensure_ascii=False),
            ]
        )
        soup = self.soup(url)
        page_text = visible_text(soup)
        labels = {
            "address": [r"Direcci(?:o|ó)n"],
            "neighborhood": [r"Barrio"],
            "locality": [r"Ciudad"],
            "province": [r"Provincia"],
            "country": [r"Pa(?:i|í)s"],
            "code": [r"C(?:o|ó)digo"],
            "category": [r"Categor(?:i|í)a"],
            "status_text": [r"Estado"],
            "sale_price": [r"Venta"],
            "location": [r"Ubicaci(?:o|ó)n"],
            "garages": [r"Estacionamientos"],
            "total_area": [r"Superficie\s+total"],
            "covered_area": [r"Superficie\s+cubierta"],
            "uncovered_area": [r"Superficie\s+descubierta"],
            "age_text": [r"A(?:ñ|n)o\s+de\s+construcci(?:o|ó)n"],
            "front_width": [r"Frente"],
            "lot_depth": [r"Fondo"],
        }
        fields = parse_labeled_fields(page_text, labels)
        if fields.get("address"):
            address = clean_detected_address(fields["address"])
            if address:
                data["address"] = address[:250]
        if fields.get("locality"):
            data["locality"] = clean_text(fields["locality"])
        if fields.get("neighborhood"):
            neighborhood = normalize_neighborhood_name(fields["neighborhood"])
            if neighborhood:
                data["neighborhood"] = neighborhood
        if not data.get("neighborhood") and re.search(r"villa\s+(?:santos\s+)?tesei", text, re.I):
            data["neighborhood"] = "Santos Tesei"
        apply_detail_fields(data, fields, "paula_fossati_detail_table")
        if fields.get("age_text"):
            year = parse_int(fields["age_text"])
            if year and 1800 < year < 2100:
                data["raw_data"] = data.get("raw_data") or {}
                data["raw_data"]["construction_year"] = year
        dimensions = re.search(
            r"terreno\s*:?\s*([\d.,]+)\s*m\s*de\s*frente\s*x\s*([\d.,]+)\s*m\s*de\s*fondo",
            page_text,
            re.I,
        )
        if dimensions:
            front = parse_decimal(dimensions.group(1))
            depth = parse_decimal(dimensions.group(2))
            if front is not None and depth is not None:
                area = front * depth
                data["land_area"] = area
                data["total_area"] = area
                data["front_width"] = front
                data["lot_depth"] = depth
                data["raw_data"] = data.get("raw_data") or {}
                data["raw_data"]["front_meters"] = str(front)
                data["raw_data"]["depth_meters"] = str(depth)
                data["raw_data"]["dimension_evidence"] = dimensions.group(0)
        data["raw_data"] = data.get("raw_data") or {}
        data["raw_data"]["paula_fields"] = fields
        data["location_precision"] = classify_address_precision(data.get("address"))
        data["operation"] = detect_operation(f"{text} {page_text}", url)
        return data


class RemaxDataworkScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="remax-datawork",
        name="REMAX DataWork",
        base_url="https://remaxdatawork.com.ar",
        search_url="https://remaxdatawork.com.ar/propiedades-en-venta-buenos-aires/casas-en-venta-hurlingham/",
        crawl_delay=5,
        enabled=False,
        notes="Landing SEO de red/franquicia; scraper parcial si expone listado real.",
    )
    detail_patterns = (r"/propiedad", r"/properties")

    def discover(self):
        soup = self.soup(self.definition.search_url)
        for url in links_matching(self, soup, (r"/propiedad/[^/]+",), require_target_text=True):
            if not is_listing_page_url(url):
                yield url

    def parse(self, url):
        if is_listing_page_url(url) or re.search(r"/propiedades-en-venta-", url, re.I):
            return None
        return super().parse(url)


class RemaxArgentinaScraper(BaseScraper):
    definition = SourceDefinition(
        slug="remax",
        name="RE/MAX Argentina",
        base_url="https://www.remax.com.ar",
        search_url=(
            "https://www.remax.com.ar/listings/buy?page=0&pageSize=24&sort=-createdAt"
            "&in:operationId=1&locations=in:::63@%3Cb%3EHurlingham%3C%2Fb%3E::::"
            "&landingPath=comprar-propiedades&filterCount=0&viewMode=listViewMode"
        ),
        crawl_delay=4,
        enabled=False,
        notes="Portal nacional RE/MAX Argentina; API publica usada por la pagina de resultados.",
    )
    api_base = "https://api-ar.redremax.com/remaxweb-ar/api"
    page_size = 24
    image_base = "https://d1acdg20u0pmxj.cloudfront.net"

    def _api_get(self, path, **params):
        self.throttle()
        response = self.session.get(
            f"{self.api_base}/{path.lstrip('/')}",
            params=params,
            headers={
                "Accept": "application/json",
                "Origin": self.definition.base_url,
                "Referer": f"{self.definition.base_url}/",
            },
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        return response.json()

    def _find_all(self, page):
        return self._api_get(
            "listings/findAllWithEntrepreneurships",
            page=page,
            pageSize=self.page_size,
            sort="-createdAt",
            **{
                "in": "operationId:1",
                "locations": "in:::63@<b>Hurlingham</b>::::",
                "landingPath": "comprar-propiedades",
            },
        )

    def _listing_url(self, item):
        slug = item.get("slug")
        if slug:
            return f"{self.definition.base_url}/listings/{slug}"
        internal_id = item.get("internalId")
        if internal_id:
            return f"{self.definition.base_url}/listingsByInternalId/{internal_id}"
        entity_id = item.get("entityId") or item.get("id")
        return f"{self.definition.base_url}/listings/{entity_id}"

    def discover(self):
        start_page = max((self.start_page or 1) - 1, 0)
        declared_total = None
        total_pages = None
        seen = set()
        pages_seen = 0
        page = start_page
        while total_pages is None or page < total_pages:
            if self.max_pages is not None and pages_seen >= self.max_pages:
                break
            if self.should_cancel():
                break
            payload = self._find_all(page)
            data = payload.get("data") or {}
            items = data.get("data") or []
            if declared_total is None:
                declared_total = parse_int(data.get("totalItems") or len(items))
            if total_pages is None:
                total_pages = parse_int(data.get("totalPages")) or 1
            pages_seen += 1
            if not items:
                break
            for item in items:
                text = json.dumps(item, ensure_ascii=False)
                if not is_target_zone(text):
                    continue
                if (item.get("operation") or {}).get("value") not in (None, "sale"):
                    continue
                url = self._listing_url(item)
                if url in seen:
                    continue
                seen.add(url)
                yield url
                if self.max_listings is not None and len(seen) >= self.max_listings:
                    self.discovery_stats = {
                        "declared_total": declared_total,
                        "pages_seen": pages_seen,
                        "urls_discovered": len(seen),
                        "coverage_ratio": None,
                        "limited_by_max_listings": True,
                        "limited_by_max_pages": self.max_pages is not None,
                    }
                    return
            page += 1

        limited_run = (self.start_page or 1) > 1 or self.max_pages is not None or self.max_listings is not None
        self.discovery_stats = {
            "cancelled": self.should_cancel(),
            "declared_total": declared_total,
            "pages_seen": pages_seen,
            "urls_discovered": len(seen),
            "coverage_ratio": (
                round((len(seen) / declared_total) * 100, 1)
                if declared_total and not limited_run
                else None
            ),
            "limited_by_max_listings": False,
            "limited_by_max_pages": self.max_pages is not None,
        }

    def _item_from_url(self, url):
        path = urlparse(url).path.strip("/")
        if path.startswith("listingsByInternalId/"):
            internal_id = path.rsplit("/", 1)[-1]
            payload = self._api_get(f"listings/findByInternalId/{internal_id}")
        else:
            slug = path.rsplit("/", 1)[-1]
            payload = self._api_get(f"listings/findBySlug/{slug}")
        return payload.get("data") or {}

    def _image_url(self, image):
        value = (image.get("value") or image.get("rawValue")) if isinstance(image, dict) else image
        if not value or not isinstance(value, str):
            return None
        if value.startswith("http"):
            return value
        return f"{self.image_base}/{value.lstrip('/')}"

    def parse(self, url):
        item = self._item_from_url(url)
        if not item:
            return None
        if (item.get("operation") or {}).get("value") != "sale":
            return None
        text = json.dumps(item, ensure_ascii=False)
        if not is_target_zone(text):
            return None

        geo = item.get("geo") or {}
        associate = item.get("associate") or {}
        office = associate.get("office") or {}
        coordinates = (item.get("location") or {}).get("coordinates") or []
        images = []
        for image in item.get("photos") or []:
            image_url = self._image_url(image)
            if image_url:
                images.append(image_url)
        features = [
            feature.get("value")
            for feature in item.get("features") or []
            if isinstance(feature, dict) and feature.get("value")
        ]
        if item.get("aptCredit"):
            features.append("Apto credito")
        data = {
            "external_id": item.get("id") or item.get("entityId") or item.get("internalId"),
            "url": url,
            "title": item.get("title") or "RE/MAX Argentina",
            "description": item.get("description") or "",
            "address": item.get("displayAddress") or "",
            "locality": (
                "William C. Morris"
                if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I)
                else "Villa Tesei"
                if re.search(r"villa\s+(?:santos\s+)?tesei", text, re.I)
                else "Hurlingham"
            ),
            "neighborhood": clean_text(geo.get("neighborhood") or ""),
            "agency": office.get("name") or associate.get("officeName") or self.definition.name,
            "agency_url": f"{self.definition.base_url}/{office.get('slug')}" if office.get("slug") else "",
            "property_type": infer_property_type((item.get("type") or {}).get("value"), item.get("title")),
            "operation": "sale",
            "currency": normalize_currency((item.get("currency") or {}).get("value") or ""),
            "price": parse_decimal(item.get("price")),
            "rooms": parse_int(item.get("totalRooms")),
            "bedrooms": parse_int(item.get("bedrooms")),
            "bathrooms": parse_decimal(item.get("bathrooms")),
            "toilets": parse_int(item.get("toilets")),
            "garages": parse_int(item.get("parkingSpaces")),
            "covered_area": parse_decimal(item.get("dimensionCovered")),
            "total_area": parse_decimal(item.get("dimensionTotalBuilt")),
            "land_area": parse_decimal(item.get("dimensionLand")),
            "uncovered_area": parse_decimal(item.get("dimensionUncovered")),
            "semicovered_area": parse_decimal(item.get("dimensionSemicovered")),
            "age_years": None,
            "features": list(dict.fromkeys(features)),
            "images": images[:30],
            "status": Property.Status.ACTIVE,
            "location_precision": classify_address_precision(item.get("displayAddress")),
            "raw_data": {"remax": item},
        }
        year_built = parse_int(item.get("yearBuilt"))
        if year_built and 1800 < year_built < 2100:
            data["raw_data"]["yearBuilt"] = year_built
        if len(coordinates) >= 2:
            data["longitude"] = float(coordinates[0])
            data["latitude"] = float(coordinates[1])
        return data


class Century21Scraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="century21-hurlingham",
        name="Century 21 Hurlingham",
        base_url="https://century21.com.ar",
        search_url="https://century21.com.ar/v/resultados/operacion_venta/en-pais_argentina/en-estado_gba-oeste/en-municipio_gba-oeste-hurlingham",
        crawl_delay=5,
        enabled=False,
        notes="Red/franquicia; discovery desde JSON publico ?json=true de la pagina de resultados.",
    )
    detail_patterns = (r"/propiedad/", r"/ficha/", r"/detalle/")

    def _json_url(self):
        separator = "&" if "?" in self.definition.search_url else "?"
        return f"{self.definition.search_url}{separator}json=true"

    def _payload(self):
        return self.get(self._json_url()).json()

    def discover(self):
        payload = self._payload()
        results = payload.get("results") or payload.get("data") or []
        declared_total = parse_int(payload.get("totalHits") or payload.get("total") or len(results))
        seen = set()
        for item in results:
            text = json.dumps(item, ensure_ascii=False)
            if re.search(r"\balquiler\b", text, re.I) and not re.search(r"\bventa\b", text, re.I):
                continue
            href = item.get("urlCorrectaPropiedad") or item.get("url") or item.get("permalink")
            if not href:
                continue
            url = self.absolute(href)
            if url in seen:
                continue
            seen.add(url)
            yield url
            if self.max_listings is not None and len(seen) >= self.max_listings:
                break
        self.discovery_stats = {
            "declared_total": declared_total,
            "pages_seen": 1,
            "urls_discovered": len(seen),
            "coverage_ratio": (
                round((len(seen) / declared_total) * 100, 1)
                if declared_total
                else None
            ),
            "limited_by_max_listings": self.max_listings is not None and len(seen) >= self.max_listings,
            "limited_by_max_pages": self.max_pages is not None,
        }

    def _detail_json_url(self, url):
        return f"{url}{'&' if '?' in url else '?'}json=true"

    def parse(self, url):
        payload = self.get(self._detail_json_url(url)).json()
        entity = payload.get("entity") or payload.get("propiedad") or payload
        if not isinstance(entity, dict):
            return None
        title = (
            entity.get("titulo")
            or entity.get("title")
            or payload.get("title")
            or entity.get("encabezado")
            or "Century 21 Hurlingham"
        )
        features = self._features(payload, entity)
        latitude = parse_decimal(entity.get("lat") or entity.get("latitude"))
        longitude = parse_decimal(entity.get("lon") or entity.get("lng") or entity.get("longitude"))
        age = parse_int(
            self._feature_value(entity, "Antiguedad")
            or self._feature_value(entity, "Antigüedad")
            or first_present(entity.get("antiguedad"), entity.get("antigüedad"), entity.get("edad"))
        )
        raw_age = age
        if age is not None and age < 0:
            age = None
        if age and 1900 <= age <= 2100:
            age = 0 if age <= timezone.now().year + 1 else None
        data = {
            "external_id": str(entity.get("id") or external_id_from_url(url)),
            "url": url,
            "title": clean_text(title)[:300],
            "description": clean_text(entity.get("descripcion") or entity.get("description") or ""),
            "address": clean_text(
                first_present(
                    entity.get("direccion"),
                    entity.get("direccionFormat"),
                    entity.get("displayAddress"),
                    entity.get("calleNumOfna"),
                    entity.get("calle"),
                )
            )[:250],
            "locality": self._locality(entity),
            "agency": self.definition.name,
            "agency_url": self.definition.base_url,
            "property_type": infer_property_type(title, entity.get("tipoPropiedadTxt"), entity.get("tipoInmuebleTxt")),
            "operation": "sale",
            "currency": normalize_currency(entity.get("moneda") or entity.get("currency") or ""),
            "price": parse_decimal(entity.get("precioVenta") or entity.get("precio") or entity.get("price")),
            "rooms": parse_int(entity.get("ambientes") or self._feature_value(entity, "Ambientes")),
            "bedrooms": parse_int(
                entity.get("dormitorios")
                or entity.get("recamaras")
                or entity.get("habitaciones")
                or self._feature_value(entity, "Dormitorios Totales")
                or self._feature_value(entity, "Dormitorio")
            ),
            "bathrooms": parse_decimal(
                entity.get("banios")
                or entity.get("banos")
                or entity.get("bathrooms")
                or self._feature_value(entity, "Baños Completos")
                or self._feature_value(entity, "Bano")
            ),
            "covered_area": parse_decimal(entity.get("m2C") or entity.get("superficieCubierta") or self._feature_value(entity, "Superficie Construcción") or self._feature_value(entity, "Superficie cubierta")),
            "total_area": parse_decimal(entity.get("m2T") or entity.get("superficieTotal") or self._feature_value(entity, "Superficie Terreno/Total") or self._feature_value(entity, "Superficie total")),
            "land_area": parse_decimal(entity.get("m2T") or entity.get("superficieTotal") or self._feature_value(entity, "Superficie Terreno/Total")),
            "age_years": age,
            "features": features,
            "images": self._images(payload),
            "status": Property.Status.ACTIVE,
            "source_status": "",
            "location_precision": "exact" if latitude is not None and longitude is not None else classify_address_precision(entity.get("direccion") or ""),
            "raw_data": {
                "century21_entity": entity,
                "century21_amenities": payload.get("amenities") or [],
                "century21_amenities_text": payload.get("amenitiesTxt") or [],
                "century21_age_raw": raw_age,
            },
        }
        if latitude is not None and longitude is not None:
            data["latitude"] = float(latitude)
            data["longitude"] = float(longitude)
            data["location_source"] = Property.LocationSource.MAP
            data["location_confidence"] = Property.LocationConfidence.HIGH
            data["location_evidence"] = {"source": "century21_json", "lat": str(latitude), "lon": str(longitude)}
        return data

    def _feature_value(self, entity, label):
        haystacks = []
        for key in ("caracteristicasJSON", "caracteristicas", "features"):
            value = entity.get(key)
            if value:
                haystacks.append(value)
        for haystack in haystacks:
            if isinstance(haystack, str):
                try:
                    haystack = json.loads(haystack)
                except json.JSONDecodeError:
                    value = text_value(haystack, [rf"{re.escape(label)}\s*:?\s*([^,\n]+)"])
                    if value:
                        return value
                    continue
            if isinstance(haystack, dict):
                for key, value in haystack.items():
                    if fold_text(str(key)) == fold_text(label):
                        return value
                if isinstance(haystack.get("campos"), list):
                    haystack = haystack["campos"]
            if isinstance(haystack, list):
                for item in haystack:
                    if not isinstance(item, dict):
                        continue
                    names = [item.get("label"), item.get("nombre"), item.get("name"), item.get("key")]
                    if any(name and fold_text(str(name)).endswith(fold_text(label)) for name in names):
                        return item.get("valor") or item.get("value")
        return None

    def _features(self, payload, entity):
        features = []
        amenities_text = payload.get("amenitiesTxt") or []
        if isinstance(amenities_text, str):
            amenities_text = re.split(r"[,;\n]+", amenities_text)
        for value in amenities_text:
            if isinstance(value, str):
                features.append(clean_text(value))
        for value in payload.get("amenities") or []:
            if isinstance(value, dict):
                features.append(clean_text(value.get("nombre") or value.get("name") or value.get("title") or ""))
            elif isinstance(value, str):
                features.append(clean_text(value))
        for value in entity.get("amenidades") or entity.get("amenities") or []:
            if isinstance(value, str):
                features.append(clean_text(value))
        raw_features = entity.get("caracteristicasJSON")
        if isinstance(raw_features, str):
            try:
                raw_features = json.loads(raw_features)
            except json.JSONDecodeError:
                raw_features = {}
        for item in (raw_features or {}).get("campos", []) if isinstance(raw_features, dict) else []:
            if isinstance(item, dict) and item.get("valor") is True:
                features.append(clean_text(item.get("label") or item.get("nombre") or ""))
        return [item for item in dict.fromkeys(features) if item]

    def _images(self, payload):
        images = []
        for photo in payload.get("fotos") or payload.get("images") or []:
            if isinstance(photo, str):
                images.append(photo)
            elif isinstance(photo, dict):
                images.append(photo.get("url") or photo.get("src") or photo.get("fotoPath") or photo.get("path"))
        return [image for image in images if image][:30]

    def _locality(self, entity):
        text = json.dumps(entity, ensure_ascii=False)
        if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I):
            return "William C. Morris"
        if re.search(r"villa\s+(?:santos\s+)?tesei", text, re.I):
            return "Villa Tesei"
        return normalize_locality(entity.get("municipio") or entity.get("localidad") or "Hurlingham")


class MercadoLibreScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="mercadolibre",
        name="MercadoLibre Inmuebles",
        base_url="https://api.mercadolibre.com",
        search_url="https://api.mercadolibre.com/sites/MLA/search",
        crawl_delay=6,
        enabled=False,
        notes="Marketplace por API oficial. Requiere token si MercadoLibre limita la busqueda publica.",
    )
    queries = (
        "casa venta hurlingham",
        "departamento venta hurlingham",
        "ph venta hurlingham",
        "terreno venta hurlingham",
        "casa venta villa tesei",
        "departamento venta villa tesei",
        "casa venta william morris",
        "terreno venta william morris",
    )

    def api_get(self, url, **params):
        headers = {"Accept": "application/json"}
        token = os.environ.get("MELI_ACCESS_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self.session.get(url, params=params, headers=headers, timeout=self.request_timeout)
        response.raise_for_status()
        return response.json()

    def discover(self):
        seen = set()
        limit = 50
        for query in self.queries:
            offset = 0
            page = 0
            while self.max_pages is None or page < self.max_pages:
                payload = self.api_get(
                    self.definition.search_url,
                    q=query,
                    limit=limit,
                    offset=offset,
                )
                results = payload.get("results") or []
                if not results:
                    break
                for item in results:
                    item_id = item.get("id")
                    text = f"{item.get('title') or ''} {item.get('permalink') or ''}"
                    if item_id and item_id not in seen and "alquiler" not in text.lower() and is_target_zone(text):
                        seen.add(item_id)
                        yield f"https://api.mercadolibre.com/items/{item_id}"
                page += 1
                offset += limit

    def parse(self, url):
        item_id = url.rstrip("/").rsplit("/", 1)[-1]
        item = self.api_get(f"https://api.mercadolibre.com/items/{item_id}")
        title = item.get("title") or "MercadoLibre Inmuebles"
        permalink = item.get("permalink") or url
        attributes = {attr.get("id") or attr.get("name"): attr.get("value_name") for attr in item.get("attributes") or []}
        text = " ".join(str(value or "") for value in [title, item.get("subtitle"), item.get("location")])
        if "alquiler" in text.lower():
            return None
        location = item.get("location") or {}
        address = clean_text(
            " ".join(
                filter(
                    None,
                    [
                        location.get("address_line"),
                        location.get("neighborhood", {}).get("name") if isinstance(location.get("neighborhood"), dict) else "",
                        location.get("city", {}).get("name") if isinstance(location.get("city"), dict) else "",
                    ],
                )
            )
        )
        images = [picture.get("secure_url") or picture.get("url") for picture in item.get("pictures") or []]
        seller = item.get("seller_id")
        data = {
            "external_id": item_id,
            "url": permalink,
            "title": title,
            "description": "",
            "address": address,
            "locality": (
                "William C. Morris"
                if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I)
                else "Villa Tesei"
                if re.search(r"villa\s+(?:santos\s+)?tesei", text, re.I)
                else "Hurlingham"
            ),
            "agency": f"MercadoLibre seller {seller}" if seller else "MercadoLibre",
            "property_type": infer_property_type(title, json.dumps(attributes, ensure_ascii=False)),
            "operation": "sale",
            "currency": normalize_currency(item.get("currency_id") or ""),
            "price": parse_decimal(item.get("price")),
            "rooms": parse_int(attributes.get("ROOMS") or attributes.get("Ambientes")),
            "bedrooms": parse_int(attributes.get("BEDROOMS") or attributes.get("Dormitorios")),
            "bathrooms": parse_decimal(attributes.get("FULL_BATHROOMS") or attributes.get("Baños")),
            "covered_area": parse_decimal(attributes.get("COVERED_AREA") or attributes.get("Superficie cubierta")),
            "total_area": parse_decimal(attributes.get("TOTAL_AREA") or attributes.get("Superficie total")),
            "land_area": parse_decimal(attributes.get("TOTAL_AREA") or attributes.get("Superficie total")),
            "images": [image for image in images if image][:30],
            "status": Property.Status.ACTIVE,
            "location_precision": classify_address_precision(address),
            "raw_data": {"item": item, "attributes": attributes},
        }
        coordinates = location.get("latitude"), location.get("longitude")
        if coordinates[0] is not None and coordinates[1] is not None:
            data["latitude"] = float(coordinates[0])
            data["longitude"] = float(coordinates[1])
        return data


class ZonapropScraper(CommonDetailScraper):
    max_public_pages = 5
    price_split_step = 2500
    initial_price_probe = 100000
    max_price_probe = 5000000
    segment_capacity_ratio = 0.8
    min_complete_coverage_ratio = 98.0

    definition = SourceDefinition(
        slug="zonaprop",
        name="Zonaprop",
        base_url="https://www.zonaprop.com.ar",
        search_url="https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham.html",
        crawl_delay=6,
        enabled=False,
        notes=(
            "Trial limitado por link directo; deshabilitado para --all. "
            "No automatizar popup, login, captcha ni challenge Cloudflare."
        ),
    )
    detail_patterns = (r"/propiedades/clasificado/",)

    def soup(self, url):
        return self._ensure_not_blocked(super().soup(url))

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return "https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham-pagina-%s.html" % page

    def _sorted_url(self):
        return f"{self.definition.base_url}/inmuebles-venta-hurlingham-hurlingham-orden-precio-ascendente.html"

    def _price_url(self, min_price=None, max_price=None, page=1):
        stem = f"{self.definition.base_url}/inmuebles-venta-hurlingham-hurlingham"
        if min_price is None and max_price is None:
            url = f"{stem}-orden-precio-ascendente.html"
        elif min_price is None:
            url = f"{stem}-menos-{int(max_price)}-dolar-orden-precio-ascendente.html"
        elif max_price is None:
            url = f"{stem}-mas-{int(min_price)}-dolar-orden-precio-ascendente.html"
        else:
            url = f"{stem}-{int(min_price)}-{int(max_price)}-dolar-orden-precio-ascendente.html"
        if page == 1:
            return url
        return url[:-5] + f"-pagina-{page}.html"

    def _ensure_not_blocked(self, soup):
        text = soup.get_text(" ", strip=True)
        markup = str(soup)
        haystack = f"{text}\n{markup}".lower()
        markers = (
            "just a moment",
            "enable javascript and cookies to continue",
            "_cf_chl_opt",
            "__cf_chl_tk",
            "cf_chl_rt_tk",
        )
        if any(marker in haystack for marker in markers):
            raise RuntimeError("request blocked by Cloudflare challenge")
        return soup

    def _canonical_listing_url(self, url):
        parsed = urlparse(url)
        path = parsed.path.lower()
        if not path.startswith("/propiedades/clasificado/"):
            return None
        if "alquiler" in path or "/alcl" in path:
            return None
        if "venta" not in path and not path.startswith("/propiedades/clasificado/vecl"):
            return None
        return parsed._replace(query="", fragment="").geturl()

    def _listing_urls(self, soup):
        self._ensure_not_blocked(soup)
        seen = set()
        for url in links_matching(self, soup, (r"/propiedades/clasificado/",)):
            canonical = self._canonical_listing_url(url)
            if canonical and canonical not in seen:
                seen.add(canonical)
                yield canonical

    def discover(self):
        limited_run = (
            self.start_page > 1
            or self.max_pages is not None
            or self.max_listings is not None
        )
        if not limited_run:
            yield from self._segmented_discover()
            return

        yield from self._limited_discover()

    def _limited_discover(self):
        if self.start_page > self.max_public_pages:
            self.discovery_stats = {
                "declared_total": None,
                "pages_seen": 0,
                "urls_discovered": 0,
                "coverage_ratio": None,
                "limited_by_max_listings": False,
                "limited_by_max_pages": True,
            }
            return
        original_max_pages = self.max_pages
        remaining_public_pages = self.max_public_pages - self.start_page + 1
        self.max_pages = min(original_max_pages or remaining_public_pages, remaining_public_pages)
        try:
            yield from paginated_discover(
                self,
                self._page_url(1),
                self._page_url,
                self._listing_urls,
                fallback_max_pages=self.max_public_pages,
            )
        finally:
            self.max_pages = original_max_pages

    def _probe_price_segment(self, min_price, max_price, cache):
        key = (min_price, max_price)
        if key not in cache:
            soup = self.soup(self._price_url(min_price, max_price))
            text = soup.get_text(" ", strip=True)
            cache[key] = {
                "min_price": min_price,
                "max_price": max_price,
                "declared_total": declared_total_from_text(text),
                "first_page_urls": list(dict.fromkeys(self._listing_urls(soup))),
            }
        return cache[key]

    def _segment_capacity(self, first_page_urls):
        page_size = len(first_page_urls) or 30
        return max(page_size, 1) * self.max_public_pages

    def _target_segment_capacity(self, capacity):
        return max(1, int(capacity * self.segment_capacity_ratio))

    def _segment_fits(self, segment, capacity):
        total = segment.get("declared_total")
        if total is not None:
            return total <= capacity
        return len(segment.get("first_page_urls") or []) < capacity

    def _split_price(self, min_price, max_price):
        low = min_price or 0
        span = max_price - low
        if span <= self.price_split_step:
            return None
        midpoint = low + (span // 2)
        midpoint = round(midpoint / self.price_split_step) * self.price_split_step
        midpoint = int(max(low + self.price_split_step, min(max_price - self.price_split_step, midpoint)))
        return midpoint if low < midpoint < max_price else None

    def _find_tail_boundary(self, probe, capacity):
        boundary = self.initial_price_probe
        last_tail = None
        while boundary <= self.max_price_probe:
            tail = probe(boundary, None)
            last_tail = tail
            if self._segment_fits(tail, capacity):
                return boundary, tail, False
            boundary *= 2
        return boundary, last_tail, True

    def _split_price_range(self, min_price, max_price, probe, capacity, segments, incomplete):
        segment = probe(min_price, max_price)
        if self._segment_fits(segment, capacity):
            segments.append(segment)
            return

        midpoint = self._split_price(min_price, max_price)
        if midpoint is None:
            segment["incomplete"] = True
            segments.append(segment)
            incomplete.append(segment)
            return

        self._split_price_range(min_price, midpoint, probe, capacity, segments, incomplete)
        self._split_price_range(midpoint, max_price, probe, capacity, segments, incomplete)

    def _build_price_segments(self):
        cache = {}

        def probe(min_price, max_price):
            return self._probe_price_segment(min_price, max_price, cache)

        base_segment = probe(None, None)
        capacity = self._segment_capacity(base_segment["first_page_urls"])
        target_capacity = self._target_segment_capacity(capacity)
        boundary, tail_segment, tail_incomplete = self._find_tail_boundary(probe, target_capacity)

        segments = []
        incomplete = []
        self._split_price_range(None, boundary, probe, target_capacity, segments, incomplete)
        if tail_segment:
            if tail_incomplete:
                tail_segment["incomplete"] = True
                incomplete.append(tail_segment)
            segments.append(tail_segment)

        return base_segment, segments, incomplete, capacity, target_capacity

    def _segment_page_count(self, segment, capacity):
        total = segment.get("declared_total")
        page_size = max(len(segment.get("first_page_urls") or []), 1)
        if total is None:
            return self.max_public_pages
        return max(1, min(self.max_public_pages, ceil(total / page_size), ceil(total / max(capacity // self.max_public_pages, 1))))

    def _segmented_discover(self):
        base_seed_urls = set()
        base_seed_pages = 0
        base_declared_total = None
        for page in range(1, self.max_public_pages + 1):
            if self.should_cancel():
                self.discovery_stats = {
                    "cancelled": True,
                    "declared_total": base_declared_total,
                    "pages_seen": base_seed_pages,
                    "urls_discovered": 0,
                    "coverage_ratio": None,
                    "limited_by_max_listings": False,
                    "limited_by_max_pages": False,
                    "segmented": True,
                    "coverage_complete": False,
                }
                return
            soup = self.soup(self._page_url(page))
            base_seed_pages += 1
            if page == 1:
                base_declared_total = declared_total_from_text(soup.get_text(" ", strip=True))
            base_seed_urls.update(self._listing_urls(soup))

        base_segment, segments, incomplete_segments, capacity, target_capacity = self._build_price_segments()
        seen = set()
        pages_seen = base_seed_pages
        segment_stats = []
        coverage_complete = not incomplete_segments

        if self.should_cancel():
            self.discovery_stats = {
                "cancelled": True,
                "declared_total": base_declared_total or base_segment.get("declared_total"),
                "pages_seen": pages_seen,
                "urls_discovered": 0,
                "coverage_ratio": None,
                "limited_by_max_listings": False,
                "limited_by_max_pages": False,
                "segmented": True,
                "coverage_complete": False,
            }
            return

        for url in sorted(base_seed_urls):
            if self.should_cancel():
                coverage_complete = False
                break
            seen.add(url)
            yield url

        for segment in segments:
            if self.should_cancel():
                coverage_complete = False
                break

            segment_urls = set(segment.get("first_page_urls") or [])
            page_count = self._segment_page_count(segment, capacity)
            pages_seen += 1
            for page in range(2, page_count + 1):
                if self.should_cancel():
                    coverage_complete = False
                    break
                soup = self.soup(self._price_url(segment.get("min_price"), segment.get("max_price"), page=page))
                pages_seen += 1
                segment_urls.update(self._listing_urls(soup))

            declared = segment.get("declared_total")
            if declared is not None and len(segment_urls) < declared:
                coverage_complete = False
            if segment.get("incomplete"):
                coverage_complete = False

            segment_stats.append(
                {
                    "min_price": segment.get("min_price"),
                    "max_price": segment.get("max_price"),
                    "declared_total": declared,
                    "urls_discovered": len(segment_urls),
                    "pages_seen": page_count,
                    "incomplete": bool(segment.get("incomplete")) or (declared is not None and len(segment_urls) < declared),
                }
            )

            for url in sorted(segment_urls):
                if self.should_cancel():
                    coverage_complete = False
                    break
                if url in seen:
                    continue
                seen.add(url)
                yield url

        declared_totals = [
            total
            for total in (base_declared_total, base_segment.get("declared_total"))
            if total
        ]
        declared_total = max(declared_totals) if declared_totals else None
        coverage_ratio = (
            round((len(seen) / declared_total) * 100, 1)
            if declared_total
            else None
        )
        if declared_total and coverage_ratio < self.min_complete_coverage_ratio:
            coverage_complete = False

        self.discovery_stats = {
            "cancelled": self.should_cancel(),
            "declared_total": declared_total,
            "pages_seen": pages_seen,
            "urls_discovered": len(seen),
            "coverage_ratio": coverage_ratio,
            "limited_by_max_listings": False,
            "limited_by_max_pages": False,
            "segmented": True,
            "base_seed_pages": base_seed_pages,
            "base_seed_urls": len(base_seed_urls),
            "base_declared_total": base_declared_total,
            "price_declared_total": base_segment.get("declared_total"),
            "segments_seen": len(segment_stats),
            "segments": segment_stats,
            "segment_capacity": capacity,
            "target_segment_capacity": target_capacity,
            "coverage_complete": coverage_complete and not self.should_cancel(),
        }

    def parse(self, url):
        canonical = self._canonical_listing_url(url)
        if not canonical or is_listing_page_url(canonical):
            return None
        data = super().parse(canonical)
        if data:
            data["url"] = canonical
            data["operation"] = detect_operation(
                f"{data.get('title') or ''} {data.get('description') or ''}",
                canonical,
            )
        return data

    def enrich_common_data(self, data, text, soup):
        data = super().enrich_common_data(data, text, soup)
        data = parse_zonaprop_highlights(data, text)
        raw_data = data.setdefault("raw_data", {})
        address = _zonaprop_address_from_soup(soup)
        if address:
            data["address"] = address
            data["detected_address"] = address
            data["location_precision"] = classify_address_precision(address)
            raw_data["zonaprop_address_source"] = "map_heading"
        currency, amount, evidence = _zonaprop_price_from_soup(soup, text)
        if amount is not None:
            data["currency"] = currency
            data["price"] = amount
            raw_data["zonaprop_currency_inference"] = evidence
        folded = fold_text(" ".join([data.get("title") or "", data.get("description") or "", data.get("url") or "", text[:700]]))
        if re.search(r"\bph\b|veclph", folded, re.I):
            data["property_type"] = Property.Type.PH
        elif re.search(r"local\s+comercial|fondo\s+de\s+comercio|vecllc|veclfc", folded, re.I):
            data["property_type"] = Property.Type.OTHER
        return data
