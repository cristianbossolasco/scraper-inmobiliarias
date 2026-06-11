import re
from urllib.parse import urlparse

from properties.models import Property
from properties.services.normalization import classify_address_precision, infer_property_type, parse_decimal, parse_int

from .base import BaseScraper, SourceDefinition
from .paginated import declared_total_from_text, paginated_discover
from .parsing import basic_html_data, first_json_ld, text_value


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
        data["location_precision"] = classify_address_precision(data.get("address"))
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
        latitudes = re.findall(r"-34\.\d{4,}", str(soup))
        longitudes = re.findall(r"-58\.\d{4,}", str(soup))
        if latitudes and longitudes:
            data["latitude"] = float(latitudes[0])
            data["longitude"] = float(longitudes[0])
            data["location_precision"] = classify_address_precision(data.get("address"))
        data["status"] = Property.Status.ACTIVE
        return data
