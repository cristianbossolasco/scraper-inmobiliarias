import re

from properties.models import Property
from properties.services.normalization import (
    classify_address_precision,
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
    repair_mojibake_text,
)

from .base import BaseScraper, SourceDefinition
from .parsing import (
    basic_html_data,
    evidence_set,
    first_json_ld,
    first_present,
    text_value,
    value_after_label,
    value_before_label,
)
from .paginated import paginated_discover


class ArgenpropScraper(BaseScraper):
    definition = SourceDefinition(
        slug="argenprop",
        name="Argenprop",
        base_url="https://www.argenprop.com",
        search_url="https://www.argenprop.com/casas/venta/hurlingham",
        crawl_delay=3,
        enabled=True,
        notes="Portal con alto volumen y riesgo de bloqueo CDN. Usar 1 worker, tandas chicas y cooldown ante 403.",
    )

    fallback_max_pages = 80

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return f"{self.definition.search_url}?pagina-{page}"

    def _listing_urls_from_soup(self, soup):
        for anchor in soup.select('a[href*="--"]'):
            href = anchor.get("href", "")
            if not re.search(r"/(?:casa|departamento|ph|duplex|terreno)-.*--\d+", href):
                continue
            yield self.absolute(href.split("?")[0])

    def discover(self):
        yield from paginated_discover(
            self,
            self._page_url(1),
            self._page_url,
            self._listing_urls_from_soup,
            fallback_max_pages=self.fallback_max_pages,
        )

    def parse(self, url):
        soup = self.soup(url)
        text = repair_mojibake_text(soup.get_text(" ", strip=True))
        data = basic_html_data(soup, url)
        payload = first_json_ld(soup, {"House", "Apartment", "SingleFamilyResidence", "Product"})
        if payload:
            address = payload.get("address") or {}
            data.update(
                {
                    "title": payload.get("name") or data["title"],
                    "description": payload.get("description") or data["description"],
                    "address": address.get("streetAddress") or data.get("address") or "",
                    "locality": address.get("addressRegion")
                    or address.get("addressLocality")
                    or "Hurlingham",
                    "rooms": payload.get("numberOfRooms") or data.get("rooms"),
                    "bedrooms": payload.get("numberOfBedrooms") or data.get("bedrooms"),
                    "property_type": infer_property_type(
                        payload.get("@type", ""), payload.get("name", ""), text[:600]
                    ),
                }
            )
            image = payload.get("image")
            if isinstance(image, str):
                data["images"] = [image]
            elif isinstance(image, list):
                data["images"] = [item for item in image if isinstance(item, str)]

        price = re.search(r"\b(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", text, re.I)
        if price:
            data["currency"] = normalize_currency(price.group(1))
            data["price"] = parse_decimal(price.group(2))

        metrics = {
            "rooms": first_present(
                value_before_label(text, [r"Ambientes?"], parse_int, unit_pattern=""),
                value_after_label(text, [r"Ambientes?"], parse_int, unit_pattern=""),
                text_value(text, [r"(\d+)\s*ambientes"], parse_int),
            ),
            "bedrooms": first_present(
                value_before_label(text, [r"Dormitorios?", r"Habitaciones?"], parse_int, unit_pattern=""),
                value_after_label(text, [r"Dormitorios?", r"Habitaciones?"], parse_int, unit_pattern=""),
                text_value(text, [r"(\d+)\s*dormitorios?", r"(\d+)\s*habitaciones?"], parse_int),
            ),
            "bathrooms": first_present(
                value_before_label(text, [r"Ba(?:ñ|n)os?"], parse_decimal, unit_pattern=""),
                value_after_label(text, [r"Ba(?:ñ|n)os?"], parse_decimal, unit_pattern=""),
                text_value(text, [r"(\d+(?:[.,]\d+)?)\s*ba(?:ñ|n)os?"], parse_decimal),
            ),
            "garages": first_present(
                value_before_label(text, [r"Cocheras?", r"Garages?"], parse_int, unit_pattern=""),
                value_after_label(text, [r"Cocheras?", r"Garages?"], parse_int, unit_pattern=""),
            ),
            "covered_area": first_present(
                value_before_label(text, [r"Cubierta", r"Cubiertos"], parse_decimal),
                value_after_label(text, [r"Sup\.?\s*Cubierta", r"Superficie\s+Cubierta", r"Cubierta"], parse_decimal),
            ),
            "total_area": first_present(
                value_before_label(text, [r"Totales?", r"Total"], parse_decimal),
                value_after_label(text, [r"Sup\.?\s*Total", r"Superficie\s+Total", r"Total"], parse_decimal),
            ),
            "land_area": first_present(
                value_before_label(text, [r"Terreno"], parse_decimal),
                value_after_label(text, [r"Sup\.?\s*Terreno", r"Superficie\s+Terreno", r"Terreno"], parse_decimal),
            ),
        }
        if metrics["total_area"] and not metrics["land_area"]:
            metrics["land_area"] = metrics["total_area"]
        for field, value in metrics.items():
            evidence_set(data, field, value, "argenprop_metrics")

        agency = None
        for candidate in re.findall(r"Contact(?:a|á)\s+al\s+anunciante\s+(.+?)\s+Ver\s+tel", text, re.I):
            if "ubicaci" not in candidate.lower() and len(candidate) < 90:
                agency = candidate.strip()
                break
        agency = agency or text_value(
            text,
            [
                r"Publicado por\s+(.+?)(?:\s+C[oó]digo|\s+Ver tel[eé]fono|\s+Contactar)",
                r"Anunciante\s+(.+?)(?:\s+Ubicaci[oó]n|\s+C[oó]digo)",
            ],
        )
        if agency and "ubicaci" in agency.lower():
            agency = None
        data["agency"] = agency or "Argenprop"
        data["location_precision"] = classify_address_precision(data.get("address"))
        data["status"] = Property.Status.ACTIVE
        return data
