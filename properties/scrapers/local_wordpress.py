import html
import json
import re
from urllib.parse import urlparse

from properties.models import Property
from properties.services.normalization import classify_address_precision, infer_property_type, parse_decimal, parse_int

from .base import BaseScraper, SourceDefinition
from .paginated import declared_total_from_text, paginated_discover
from .parsing import basic_html_data, first_json_ld, text_value


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_wordpress_map_data(soup):
    raw_html = str(soup)
    candidates = []

    for tag in soup.select("[data-map]"):
        raw = tag.get("data-map")
        if raw:
            candidates.append(html.unescape(raw))

    for match in re.finditer(r"propertyMapData\s*=\s*(\{.*?\})\s*;", raw_html, re.S):
        candidates.append(match.group(1))

    for raw in candidates:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            continue
        latitude = _safe_float(payload.get("latitude") or payload.get("lat"))
        longitude = _safe_float(payload.get("longitude") or payload.get("lng") or payload.get("lang"))
        if latitude is not None and longitude is not None:
            return {
                "latitude": latitude,
                "longitude": longitude,
                "address": payload.get("address") or "",
            }

    latitudes = re.findall(r"-34\.\d{4,}", raw_html)
    longitudes = re.findall(r"-58\.\d{4,}", raw_html)
    if latitudes and longitudes:
        return {
            "latitude": float(latitudes[0]),
            "longitude": float(longitudes[0]),
            "address": "",
        }
    return {}


def is_miglierini_detail_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    return len(parts) == 2 and parts[0] == "propiedad" and parts[1]


def is_odriozola_detail_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    return len(parts) == 3 and parts[:2] == ["inmobiliaria", "propiedades"] and parts[2]


class MiglieriniScraper(BaseScraper):
    definition = SourceDefinition(
        slug="miglierini",
        name="Miglierini Propiedades",
        base_url="https://www.miglieriniprop.com",
        search_url="https://www.miglieriniprop.com/properties-search/?status=venta&location=miami",
        crawl_delay=5,
        enabled=False,
        notes="Sitio WordPress local. Deshabilitado hasta validación lenta por crawl-delay 10.",
    )

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return f"{self.definition.base_url}/properties-search/page/{page}/?status=venta&location=miami"

    def _listing_urls(self, soup):
        seen = set()
        for anchor in soup.select('a[href*="/propiedad/"]'):
            url = self.absolute(anchor["href"])
            if is_miglierini_detail_url(url) and url not in seen:
                seen.add(url)
                yield url

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls,
            fallback_max_pages=30,
        )

    def parse(self, url):
        soup = self.soup(url)
        text = soup.get_text(" ", strip=True)
        data = basic_html_data(soup, url)
        payload = first_json_ld(soup, {"RealEstateListing", "House"})
        if payload:
            data["description"] = payload.get("description") or data["description"]
        data["agency"] = "Miglierini Propiedades"
        data["locality"] = "Hurlingham"
        data["property_type"] = infer_property_type(data["title"], url, text[:600])
        data["operation"] = "sale"
        data["rooms"] = data.get("rooms") or text_value(text, [r"(\d+)\s*amb"], parse_int)
        data["covered_area"] = data.get("covered_area") or text_value(text, [r"([\d.,]+)\s*m2?\s*cub"], parse_decimal)
        data["land_area"] = data.get("land_area") or text_value(text, [r"([\d.,]+)\s*m2?\s*lote"], parse_decimal)
        map_data = _extract_wordpress_map_data(soup)
        if map_data:
            data["latitude"] = map_data["latitude"]
            data["longitude"] = map_data["longitude"]
        data["location_precision"] = "exact" if map_data else classify_address_precision(data.get("address"))
        data["status"] = Property.Status.ACTIVE
        return data


class OdriozolaScraper(BaseScraper):
    definition = SourceDefinition(
        slug="odriozola",
        name="Odriozola Propiedades",
        base_url="https://odriozolapropiedades.com.ar",
        search_url="https://odriozolapropiedades.com.ar/inmobiliaria/busqueda-avanzada?keyword=&location%5B%5D=hurlingham&currency=&min-price=&max-price=&label%5B%5D=",
        crawl_delay=3,
        enabled=False,
        notes="Sitio local con fichas y coordenadas embebidas. Deshabilitado hasta fixtures completos.",
    )

    def discover(self):
        soup = self.soup(self.definition.search_url)
        seen = set()
        declared_total = declared_total_from_text(soup.get_text(" ", strip=True))
        anchors = soup.select('.listing-view a[href*="/inmobiliaria/propiedades/"]') or soup.select(
            'a[href*="/inmobiliaria/propiedades/"]'
        )
        for anchor in anchors:
            url = self.absolute(anchor["href"])
            if is_odriozola_detail_url(url) and url not in seen:
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

    def parse(self, url):
        soup = self.soup(url)
        text = soup.get_text(" ", strip=True)
        if re.search(r"\balquiler\b", text, re.I) and not re.search(r"\bventa\b", text, re.I):
            return None
        data = basic_html_data(soup, url)
        data["agency"] = "Odriozola Propiedades"
        data["locality"] = "Hurlingham"
        data["property_type"] = infer_property_type(data["title"], text[:600])
        data["operation"] = "sale"
        map_data = _extract_wordpress_map_data(soup)
        if map_data:
            data["latitude"] = map_data["latitude"]
            data["longitude"] = map_data["longitude"]
            if not data.get("address") and map_data.get("address"):
                data["address"] = map_data["address"]
            if re.search(r"\bVilla\s+Tesei\b", data.get("address") or "", re.I):
                data["locality"] = "Villa Tesei"
            data["location_precision"] = "exact" if data.get("latitude") is not None else classify_address_precision(data.get("address"))
        data["status"] = Property.Status.ACTIVE
        return data
