import json
import os
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from properties.models import Property
from properties.services.normalization import (
    classify_address_precision,
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
)
from properties.services.location_enrichment import clean_detected_address

from .base import BaseScraper, SourceDefinition
from .parsing import (
    basic_html_data,
    evidence_set,
    first_json_ld,
    first_present,
    parse_labeled_fields,
    text_value,
    value_after_label,
)
from .paginated import paginated_discover


TARGET_ZONES = ("hurlingham", "villa tesei", "villa santos tesei", "william morris")


def clean_text(value):
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
        r"Superficie\s+total\s*:?\s*([\d.,]+)\s*m(?:2|²|Â²)?.*?"
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
    parsed = parse_int(value)
    if parsed is None or parsed > maximum:
        return None
    return parsed


def price_near_label(text):
    match = re.search(r"(?:Precio\s*:?\s*)?(U\$S|U\$D|US\$|USD|ARS|\$)\s*([\d.,]+)", text, re.I)
    if not match:
        return "", None
    return normalize_currency(match.group(1)), parse_decimal(match.group(2))


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


def enrich_from_common_text(data, text, default_locality="Hurlingham"):
    address = data.get("address") or text_value(
        text,
        [
            r"(?:Direccion|Dirección|Ubicacion|Ubicación)\s*:?\s*(.+?)(?:Venta|USD|U\$S|US\$|ARS|\$|Caracteristicas|Características|Descripcion|Descripción)",
            r"^(.+?,\s*(?:Hurlingham|Villa Tesei|William Morris)[^\.]*)",
        ],
    )
    if address:
        data["address"] = clean_detected_address(address)[:250]
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


class AnaliaFernandezScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="analia-fernandez",
        name="Analía Fernández Servicios Inmobiliarios",
        base_url="https://www.fernandezpropiedades.com.ar",
        search_url="https://www.fernandezpropiedades.com.ar/Venta",
        crawl_delay=3,
        enabled=False,
        notes="Fuente local con alto volumen. Arranca deshabilitada hasta validar corrida completa.",
    )
    detail_patterns = (r"/p/\d+-",)
    require_target_text = True

    def parse(self, url):
        data = super().parse(url)
        if not data:
            return None
        soup = self.soup(url)
        text = visible_text(soup)
        if not re.search(r"INFORMACI(?:O|Ó|Ã“)N\s+B(?:A|Á|Ã)SICA|SUPERFICIES\s+Y\s+MEDIDAS", text, re.I):
            return data
        labels = {
            "rooms": [r"Ambientes"],
            "bedrooms": [r"Dormitorios"],
            "bathrooms": [r"Ba(?:ñ|n|Ã±)os"],
            "toilets": [r"Toilettes?"],
            "garages": [r"Cocheras?"],
            "age_text": [r"Antig(?:ü|u|Ã¼)edad"],
            "condition": [r"Condici(?:o|ó|Ã³)n"],
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
                r"(?:Direcci(?:o|ó|Ã³)n)\s*:?\s*(.+?)(?:\s+Ubicaci(?:o|ó|Ã³)n|\s+Agua|\s+INFORMACI)",
                r"(?:Ubicaci(?:o|ó|Ã³)n)\s*:?\s*(.+?)(?:\s+Agua|\s+INFORMACI)",
            ],
        )
        if address:
            data["address"] = clean_detected_address(address)[:250]
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


class LopezCombaScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="lopez-comba",
        name="Lopez Comba Propiedades",
        base_url="https://www.lopezcomba.ar",
        search_url="https://www.lopezcomba.ar/Casas",
        crawl_delay=3,
        enabled=False,
        notes="Fuente local para Villa Tesei y William Morris. Arranca deshabilitada.",
    )
    detail_patterns = (r"/p/\d+-",)
    require_target_text = True


class RiquelmeScraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="riquelme",
        name="Riquelme Propiedades",
        base_url="https://www.riquelmepropiedades.com.ar",
        search_url="https://www.riquelmepropiedades.com.ar",
        crawl_delay=3,
        enabled=False,
        notes="Fuente local con fichas /propiedad/. Falta validar paginacion completa.",
    )
    detail_patterns = (r"/propiedad/",)


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

    def parse(self, url):
        soup = self.soup(url)
        data = super().parse(url)
        if not data:
            return None
        text = visible_text(soup)
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
        return data


class GuarnieriScraper(MultiSearchScraper):
    definition = SourceDefinition(
        slug="guarnieri",
        name="Guarnieri Propiedades",
        base_url="https://guarnieripropiedades.com.ar",
        search_url="https://guarnieripropiedades.com.ar/inmobiliaria/ciudad/villa-tesei",
        crawl_delay=3,
        enabled=False,
        notes="Fuente local con posible WordPress/JS. Incluye Villa Tesei, William Morris y Hurlingham.",
    )
    detail_patterns = (r"/inmobiliaria/propiedades/", r"/propiedad/")
    search_urls = (
        "https://guarnieripropiedades.com.ar/inmobiliaria/ciudad/hurlingham",
        "https://guarnieripropiedades.com.ar/inmobiliaria/ciudad/villa-tesei",
        "https://guarnieripropiedades.com.ar/inmobiliaria/ciudad/william-morris",
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
        metric_block_match = re.search(r"Precio\s*:.*?(?:CARACTER[ÃÍI]STICAS|UBICACI[ÃÓO]N|$)", detail_text, re.I)
        metric_block = metric_block_match.group(0) if metric_block_match else metrics_text
        data["rooms"] = label_before_number(detail_text, [r"Amb\.?", r"Ambientes"], parse_int) or number_before_label(detail_text, [r"Amb\.?", r"Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = label_before_number(detail_text, [r"Dormitorios"], parse_int) or number_before_label(detail_text, [r"Dormitorios"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = number_before_label(detail_text, [r"Baños", r"Banos"], parse_decimal) or label_before_number(detail_text, [r"Baños", r"Banos"], parse_decimal) or data.get("bathrooms")
        data["garages"] = number_before_label(detail_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int) or label_before_number(detail_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int) or data.get("garages")
        data["rooms"] = label_before_number(metrics_text, [r"Amb\.?", r"Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = label_before_number(metrics_text, [r"Dormitorios"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = label_before_number(metrics_text, [r"BaÃ±os", r"Banos"], parse_decimal) or data.get("bathrooms")
        data["garages"] = label_before_number(metrics_text, [r"Garage", r"Garaje", r"Cocheras"], parse_int) or data.get("garages")
        data["rooms"] = text_value(detail_text, [r"(\d+)\s*Amb\.?", r"(\d+)\s*Ambientes"], parse_int) or data.get("rooms")
        data["bedrooms"] = text_value(metric_block, [r"Dormitorios?\s*:?\s*(\d+)"], parse_int) or data.get("bedrooms")
        data["bathrooms"] = text_value(metric_block, [r"Ba(?:Ã±|ñ|n)o?s?\s*:?\s*(\d+(?:[.,]\d+)?)"], parse_decimal) or data.get("bathrooms")
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
            data["address"] = clean_text(address)[:250]
        city = text_value(detail_text, [r"Ciudad\s*:?\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .]+?)(?:Barrio\s*:|Propiedades|$)"])
        if city:
            data["locality"] = clean_text(city)
        neighborhood = text_value(detail_text, [r"Barrio\s*:?\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .]+?)(?:Propiedades|$)"])
        if neighborhood:
            data["neighborhood"] = clean_text(neighborhood)
        unit_offers = parse_multi_unit_offers(detail_text)
        if unit_offers:
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
                text_value(text, [r"(\d+(?:[.,]\d+)?)\s*ba(?:ñ|n|Ã±)os?"], parse_decimal),
                value_after_label(text, [r"Ba(?:ñ|n|Ã±)os?"], parse_decimal, unit_pattern=""),
            ),
            "garages": first_present(
                text_value(text, [r"(\d+)\s*cocheras?"], parse_int),
                value_after_label(text, [r"Cocheras?"], parse_int, unit_pattern=""),
            ),
            "covered_area": first_present(
                text_value(text, [r"([\d.,]+)\s*m(?:2|²|Â²)?\s*Cubierta"], parse_decimal),
                value_after_label(text, [r"Superficie\s+Cubierta", r"Sup\.?\s*Cubierta"], parse_decimal),
            ),
            "total_area": first_present(
                text_value(text, [r"([\d.,]+)\s*m(?:2|²|Â²)?\s*Totales"], parse_decimal),
                value_after_label(text, [r"Superficie\s+Total", r"Sup\.?\s*Total", r"Totales?"], parse_decimal),
            ),
            "land_area": first_present(
                text_value(text, [r"([\d.,]+)\s*m(?:2|²|Â²)?\s*Terreno"], parse_decimal),
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
        search_url="https://patagonprop.com/office/torriani-propiedades",
        crawl_delay=4,
        enabled=False,
        notes="Portal para oficinas chicas como Torriani y Adriana Dato. Fuente parcial.",
    )
    detail_patterns = (r"/property/", r"/propiedad/")

    def parse(self, url):
        data = super().parse(url)
        if not data:
            return None
        soup = self.soup(url)
        text = visible_text(soup)
        labels = {
            "operation": [r"Operaci(?:o|ó|Ã³)n"],
            "address": [r"Direcci(?:o|ó|Ã³)n"],
            "location": [r"Ubicaci(?:o|ó|Ã³)n"],
            "code": [r"C(?:o|ó|Ã³)digo"],
            "property_type_text": [r"Tipo"],
            "total_area": [r"Superficie\s+total", r"Superficie"],
            "rooms": [r"Ambientes?"],
            "bedrooms": [r"Habitaciones?", r"Dormitorios?"],
            "bathrooms": [r"Ba(?:ñ|n|Ã±)os?"],
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
        apply_detail_fields(data, fields, "patagonprop_detail")
        highlighted_area = text_value(text, [r"superficie\s+([\d.,]+)\s*m2"], parse_decimal)
        if highlighted_area is not None:
            evidence_set(data, "total_area", highlighted_area, "patagonprop_highlights")
            evidence_set(data, "land_area", highlighted_area, "patagonprop_highlights")
        if data.get("currency") == "ARS" and data.get("price") and data["price"] < 1000000:
            data["source_status"] = "price_age_review"
        data["raw_data"] = data.get("raw_data") or {}
        data["raw_data"]["patagonprop_fields"] = fields
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
            "address": [r"Direcci(?:o|ó|Ã³)n"],
            "neighborhood": [r"Barrio"],
            "locality": [r"Ciudad"],
            "garages": [r"Estacionamientos"],
            "total_area": [r"Superficie\s+total"],
            "covered_area": [r"Superficie\s+cubierta"],
            "uncovered_area": [r"Superficie\s+descubierta"],
            "age_text": [r"A(?:ñ|n|Ã±)o\s+de\s+construcci(?:o|ó|Ã³)n"],
            "front_width": [r"Frente"],
            "lot_depth": [r"Fondo"],
        }
        fields = parse_labeled_fields(page_text, labels)
        if fields.get("address"):
            data["address"] = clean_detected_address(fields["address"])[:250]
        if fields.get("locality"):
            data["locality"] = clean_text(fields["locality"])
        if fields.get("neighborhood"):
            data["neighborhood"] = clean_text(fields["neighborhood"])
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


class Century21Scraper(CommonDetailScraper):
    definition = SourceDefinition(
        slug="century21-hurlingham",
        name="Century 21 Hurlingham",
        base_url="https://century21.com.ar",
        search_url="https://century21.com.ar/v/resultados/tipo_casa/operacion_venta/en-pais_argentina/en-estado_gba-oeste/en-municipio_gba-oeste-hurlingham",
        crawl_delay=5,
        enabled=False,
        notes="Red/franquicia. Puede requerir JS/API publica; no evadir bloqueos.",
    )
    detail_patterns = (r"/propiedad/", r"/ficha/", r"/detalle/")


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
    definition = SourceDefinition(
        slug="zonaprop",
        name="Zonaprop",
        base_url="https://www.zonaprop.com.ar",
        search_url="https://www.zonaprop.com.ar/inmuebles-hurlingham.html",
        crawl_delay=6,
        enabled=False,
        notes="Portal de alto volumen y riesgo anti-bot. Usar solo si HTML publico es estable.",
    )
    detail_patterns = (r"/propiedades/", r"/inmuebles-")

    def discover(self):
        soup = self.soup(self.definition.search_url)
        for url in links_matching(self, soup, (r"/propiedades/clasificado/",), require_target_text=True):
            if "alquiler" not in url.lower():
                yield url

    def parse(self, url):
        if is_listing_page_url(url) or "alquiler" in url.lower() or "/alcl" in url.lower():
            return None
        data = super().parse(url)
        if data:
            data["operation"] = detect_operation(
                f"{data.get('title') or ''} {data.get('description') or ''}",
                url,
            )
        return data
