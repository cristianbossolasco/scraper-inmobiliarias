import re

from properties.models import Property
from .base import BaseScraper, SourceDefinition
from .paginated import paginated_discover
from .parsing import basic_html_data, evidence_set, parse_surface_pair, text_value, value_after_label
from properties.services.normalization import (
    classify_address_precision,
    infer_property_type,
    normalize_currency,
    parse_decimal,
    parse_int,
)


STATUS_LABELS = {
    "reserved": Property.Status.RESERVED,
    "sold": Property.Status.SOLD,
    "suspended": Property.Status.SUSPENDED,
}


class MapapropScraper(BaseScraper):
    definition = SourceDefinition(
        slug="mapaprop",
        name="Mapaprop",
        base_url="https://www.mapaprop.com",
        search_url=(
            "https://www.mapaprop.com/en/search/"
            "inmuebles-venta-in_hurlingham_hurlingham_buenos_aires_1_2_196_46-from_0"
        ),
        crawl_delay=2,
        notes="HTML indexable y fichas estructuradas. No se utiliza su API privada.",
    )
    page_size = 12
    fallback_max_pages = 80

    def _page_url(self, page):
        offset = max(page - 1, 0) * self.page_size
        return re.sub(r"-from_\d+", f"-from_{offset}", self.definition.search_url)

    def _listing_urls(self, soup):
        seen = set()
        for anchor in soup.select('a[href*="/property/"]'):
            url = self.absolute(anchor["href"])
            if url in seen:
                continue
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

    def _status_from_text(self, text, title):
        title = r"\s+".join(re.escape(part) for part in (title or "").strip().split())
        for raw_status, property_status in STATUS_LABELS.items():
            if title and re.search(rf"\b{raw_status}\b\s+{title}", text, re.I):
                return raw_status, property_status
        status = text_value(
            text,
            [r"\bStatus\s*:?\s*(Reserved|Sold|Suspended)\b"],
        )
        if status:
            raw_status = status.lower()
            return raw_status, STATUS_LABELS.get(raw_status, Property.Status.ACTIVE)
        return "", Property.Status.ACTIVE

    def _price_from_text(self, text):
        match = re.search(r"\bPrice\s*:?\s*(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", text, re.I)
        if not match:
            match = re.search(r"\b(USD|U\$S|US\$|ARS)\s*([\d.,]+)", text, re.I)
        if not match:
            return "", None
        return normalize_currency(match.group(1)), parse_decimal(match.group(2))

    def _hide_suspicious_price(self, data, reason):
        raw_data = data.setdefault("raw_data", {})
        raw_data["mapaprop_public_price"] = {
            "currency": data.get("currency") or "",
            "price": str(data.get("price")) if data.get("price") is not None else "",
        }
        raw_data["mapaprop_price_hidden_reason"] = reason
        data["currency"] = ""
        data["price"] = None

    def _normalize_price_visibility(self, data):
        price = data.get("price")
        status = data.get("status") or Property.Status.ACTIVE
        currency = data.get("currency") or ""
        if price is None:
            data["currency"] = ""
            return
        if price == 1:
            self._hide_suspicious_price(data, "placeholder_price_1")
            return
        if currency == "ARS" and price < 1000:
            self._hide_suspicious_price(data, "ars_placeholder_price")
            return
        if status != Property.Status.ACTIVE and currency == "ARS":
            self._hide_suspicious_price(data, "non_active_ars_price")

    def parse(self, url):
        soup = self.soup(url)
        data = basic_html_data(soup, url)
        text = soup.get_text(" ", strip=True)
        header = soup.select_one("h1")
        raw_status, status = self._status_from_text(text, data.get("title"))
        data["status"] = status
        data["source_status"] = raw_status
        if raw_status:
            data.setdefault("raw_data", {})["mapaprop_status_badge"] = raw_status
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
            [
                r"Property\s+type\s*:?\s*(.+?)(?:\s+Total\s+surface|\s+Years\s+old|\s+Rooms|\s+Full|\s+Garages|\s+Building|$)"
            ],
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
        currency, price = self._price_from_text(text)
        if price is not None:
            data["currency"] = currency
            data["price"] = price
        self._normalize_price_visibility(data)
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
        return data
