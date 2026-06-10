import re

from .base import BaseScraper, SourceDefinition
from .parsing import basic_html_data, evidence_set, parse_surface_pair, text_value, value_after_label
from properties.services.normalization import (
    classify_address_precision,
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
)


class MapapropScraper(BaseScraper):
    definition = SourceDefinition(
        slug="mapaprop",
        name="Mapaprop",
        base_url="https://www.mapaprop.com",
        search_url=(
            "https://www.mapaprop.com/en/search/"
            "inmuebles-venta-en_hurlingham_hurlingham_buenos_aires_1_2_196_46"
        ),
        crawl_delay=2,
        notes="HTML indexable y fichas estructuradas. No se utiliza su API privada.",
    )

    def discover(self):
        seen = set()
        page_url = self.definition.search_url
        page = 0
        while page_url and (self.max_pages is None or page < self.max_pages):
            page += 1
            soup = self.soup(page_url)
            for anchor in soup.select('a[href*="/property/"]'):
                url = self.absolute(anchor["href"])
                if url not in seen:
                    seen.add(url)
                    yield url
            next_link = next(
                (
                    anchor
                    for anchor in soup.select("a[href]")
                    if anchor.get_text(" ", strip=True).lower() in {"next", "siguiente"}
                ),
                None,
            )
            page_url = self.absolute(next_link["href"]) if next_link else None

    def parse(self, url):
        soup = self.soup(url)
        data = basic_html_data(soup, url)
        text = soup.get_text(" ", strip=True)
        header = soup.select_one("h1")
        address_node = header.find_next("p") if header else None
        if address_node:
            address_text = address_node.get_text(" ", strip=True)
            data["address"] = address_text.split(",")[0]
            parts = [part.strip() for part in address_text.split(",")]
            data["locality"] = parts[1] if len(parts) > 1 else "Hurlingham"
        structured_address = text_value(
            text,
            [
                r"Address:\s*(.+?)(?:\s+Location:|\s+Type:|\s+Status:|\s+Code:|\s+Price:)"
            ],
        )
        if structured_address:
            data["address"] = structured_address.split(",")[0].strip()
        data["agency"] = text_value(
            text,
            [
                r"Marketed by::?\s*(.+?)(?:\s+(?:Operation|Address|Location|Code|Price):|\s+\d+\s+(?:Rooms|Bathrooms)|Description)"
            ],
        ) or ""
        data["rooms"] = text_value(text, [r"(\d+)\s*Ambiences"], parse_int)
        data["bedrooms"] = text_value(text, [r"(\d+)\s*Rooms"], parse_int)
        data["bathrooms"] = text_value(text, [r"(\d+)\s*Bathrooms"], parse_int)
        data["garages"] = text_value(text, [r"(\d+)\s*Garages"], parse_int) or data.get("garages")
        total, covered = parse_surface_pair(text)
        if total is not None:
            evidence_set(data, "total_area", total, "mapaprop_surface_pair")
            evidence_set(data, "land_area", total, "mapaprop_surface_pair")
        if covered is not None:
            evidence_set(data, "covered_area", covered, "mapaprop_surface_pair")
        highlight_type = text_value(
            text,
            [r"Property\s+type\s*:?\s*([A-Za-zÃ¡Ã©Ã­Ã³ÃºÃ±ÁÉÍÓÚÑ ]+?)(?:\s+Total\s+surface|\s+Years\s+old|\s+Rooms|\s+Full|\s+Garages|\s+Building|$)"],
        )
        if highlight_type:
            data["property_type"] = infer_property_type(highlight_type, data.get("title"))
            data.setdefault("raw_data", {})["mapaprop_property_type"] = highlight_type.strip()
        highlight_total = value_after_label(text, [r"Total\s+surface"])
        if highlight_total is not None:
            evidence_set(data, "total_area", highlight_total, "mapaprop_highlights")
            evidence_set(data, "land_area", highlight_total, "mapaprop_highlights")
        evidence_set(data, "age_years", value_after_label(text, [r"Years\s+old"], parse_int, unit_pattern=""), "mapaprop_highlights")
        evidence_set(data, "building_floors", value_after_label(text, [r"Building\s+floors"], parse_int, unit_pattern=""), "mapaprop_highlights")
        evidence_set(data, "garages", value_after_label(text, [r"Garages?"], parse_int, unit_pattern="") or data.get("garages"), "mapaprop_highlights")
        price = re.search(r"\b(USD|U\$S|US\$|ARS)\s*([\d.,]+)", text, re.I)
        if price:
            data["currency"] = normalize_currency(price.group(1))
            data["price"] = parse_decimal(price.group(2))
        raw_html = str(soup)
        latitudes = re.findall(r"-34\.\d{4,}", raw_html)
        longitudes = re.findall(r"-58\.\d{4,}", raw_html)
        if latitudes and longitudes:
            data["latitude"] = float(latitudes[0])
            data["longitude"] = float(longitudes[0])
            data["location_precision"] = classify_address_precision(data.get("address"))
        description_heading = next(
            (node for node in soup.select("h2") if node.get_text(strip=True) == "Description"),
            None,
        )
        if description_heading:
            description = description_heading.find_next("p")
            if description:
                data["description"] = description.get_text(" ", strip=True)
        non_residential = (
            "oficina",
            "galpón",
            "galpon",
            "local comercial",
            "depósito",
            "deposito",
            "fondo de comercio",
            "hotel",
        )
        if any(term in data["title"].lower() for term in non_residential):
            return None
        return data
