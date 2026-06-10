import re

from properties.models import Property
from properties.services.normalization import (
    classify_address_precision,
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
)

from .base import BaseScraper, SourceDefinition
from .parsing import basic_html_data, first_json_ld, text_value


RESIDENTIAL_PATHS = ("/casas/", "/departamentos/", "/terrenos/", "/ph/", "/duplex/")
TARGET_ZONES = ("hurlingham", "villa-tesei", "william-morris", "william-morris-hurlingham")


class MercadoPropScraper(BaseScraper):
    definition = SourceDefinition(
        slug="mercadoprop",
        name="MercadoProp",
        base_url="https://www.mercadoprop.net",
        search_url="https://www.mercadoprop.net/sitemap_ar.xml",
        crawl_delay=2,
        enabled=True,
        notes="Sitemap público con fichas JSON-LD y coordenadas cuando están disponibles.",
    )

    def discover(self):
        response = self.get(self.definition.search_url)
        links = re.findall(r"<loc>(.*?)</loc>", response.text)
        seen = set()
        yielded = 0
        for url in links:
            lowered = url.lower()
            if "/venta-" not in lowered:
                continue
            if not any(path in lowered for path in RESIDENTIAL_PATHS):
                continue
            if not any(zone in lowered for zone in TARGET_ZONES):
                continue
            if url in seen:
                continue
            seen.add(url)
            yield url
            yielded += 1
            if self.max_pages and yielded >= self.max_pages:
                break

    def parse(self, url):
        soup = self.soup(url)
        text = soup.get_text(" ", strip=True)
        data = basic_html_data(soup, url)
        payload = first_json_ld(soup, "RealEstateListing")
        residence = (payload or {}).get("mainEntity") or {}
        offer = (payload or {}).get("offers") or {}
        provider = (payload or {}).get("provider") or {}
        address = residence.get("address") or {}
        geo = residence.get("geo") or (payload or {}).get("geo") or {}

        data.update(
            {
                "title": (payload or {}).get("name") or residence.get("name") or data["title"],
                "description": (payload or {}).get("description") or data["description"],
                "address": address.get("streetAddress") or data.get("address") or "",
                "locality": address.get("addressLocality") or address.get("addressRegion") or "Hurlingham",
                "agency": provider.get("name") or "MercadoProp",
                "agency_url": provider.get("url") or "",
                "currency": offer.get("priceCurrency") or data.get("currency"),
                "price": offer.get("price") or data.get("price"),
                "property_type": infer_property_type(
                    residence.get("@type", ""), residence.get("name", ""), url
                ),
                "raw_data": payload or {},
            }
        )
        image = (payload or {}).get("image") or provider.get("image")
        if isinstance(image, str):
            data["images"] = [image]
        elif isinstance(image, list):
            data["images"] = [item for item in image if isinstance(item, str)]

        data["rooms"] = text_value(text, [r"(\d+)\s*ambientes?"], parse_int)
        data["bedrooms"] = text_value(text, [r"(\d+)\s*dormitorios?", r"(\d+)\s*habitaciones?"], parse_int)
        data["bathrooms"] = text_value(text, [r"(\d+(?:[.,]\d+)?)\s*ba(?:ñ|n)os?"], parse_decimal)
        data["covered_area"] = text_value(text, [r"([\d.,]+)\s*m2?\s*cubiertos"], parse_decimal)
        data["total_area"] = text_value(text, [r"([\d.,]+)\s*m2?\s*totales"], parse_decimal)
        data["land_area"] = text_value(text, [r"([\d.,]+)\s*m2?\s*terreno"], parse_decimal)

        latitude = geo.get("latitude") or text_value(str(payload), [r'"latitude"\s*:\s*"?(-?\d+\.\d+)"?'])
        longitude = geo.get("longitude") or text_value(str(payload), [r'"longitude"\s*:\s*"?(-?\d+\.\d+)"?'])
        if latitude and longitude:
            data["latitude"] = float(latitude)
            data["longitude"] = float(longitude)
            data["location_precision"] = classify_address_precision(data.get("address"))
        data["currency"] = normalize_currency(data.get("currency") or "")
        data["price"] = parse_decimal(data.get("price"))
        data["status"] = Property.Status.ACTIVE
        return data
