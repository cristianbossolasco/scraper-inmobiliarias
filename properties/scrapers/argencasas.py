import re

from .base import BaseScraper, SourceDefinition
from .parsing import basic_html_data, json_ld_objects
from .paginated import paginated_discover
from properties.services.normalization import infer_property_type, parse_decimal, parse_int


ARGENCASAS_ZONES = (
    "Parque Johnston",
    "Parque Quirno",
    "Villa Alemania",
    "Barrio Ingles",
    "Zona Iglesia",
    "Villa Club",
    "Barrio Luna",
    "Km 18",
    "Hurlingham",
)


def argencasas_zone(text):
    folded = (text or "").lower()
    for zone in ARGENCASAS_ZONES:
        if zone.lower() in folded:
            return zone
    return ""


def metric_before_label(text, label, parser=parse_decimal):
    patterns = [
        rf"([\d.,]+)\s*(?:m²|mÂ²|m2|mts²|mts)?\s*\|\s*{label}\b",
        rf"([\d.,]+)\s*(?:m²|mÂ²|m2|mts²|mts)?\s+{label}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return parser(match.group(1))
    return None


class ArgencasasScraper(BaseScraper):
    definition = SourceDefinition(
        slug="argencasas",
        name="Argencasas / SIBA",
        base_url="https://www.argencasas.com",
        search_url="https://www.argencasas.com/venta-hurlingham-localidad",
        crawl_delay=2,
        notes="Portal SIBA con JSON-LD estructurado y demora solicitada de 2 segundos.",
    )
    fallback_max_pages = 40

    def _page_url(self, page):
        if page <= 1:
            return self.definition.search_url
        return f"{self.definition.search_url}?page={page}"

    def _listing_urls(self, soup):
        seen = set()
        for anchor in soup.select('a[href*="/propiedad-"]'):
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

    def parse(self, url):
        soup = self.soup(url)
        data = basic_html_data(soup, url)
        page_text = soup.get_text(" ", strip=True)
        for payload in json_ld_objects(soup):
            if payload.get("@type") != "RealEstateListing":
                continue
            residence = payload.get("mainEntity") or {}
            address = residence.get("address") or {}
            offer = payload.get("offers") or {}
            data.update(
                {
                    "title": payload.get("name") or data["title"],
                    "description": payload.get("description") or "",
                    "address": address.get("streetAddress") or "",
                    "locality": address.get("addressLocality") or "Hurlingham",
                    "price": offer.get("price") or data["price"],
                    "currency": offer.get("priceCurrency") or data["currency"],
                    "agency": (offer.get("seller") or {}).get("name") or "",
                }
            )
            image = payload.get("image")
            if image:
                data["images"] = [self.absolute(image)]
            break

        labels = [
            item.get_text(" ", strip=True)
            for item in soup.select("li")
            if item.get_text(" ", strip=True)
        ]
        joined = " | ".join(labels)
        data["property_type"] = infer_property_type(
            data.get("title"), data.get("description"), joined, page_text[:1200], url
        )
        zone = argencasas_zone(" ".join([data.get("title", ""), joined, page_text[:1600], url]))
        if zone:
            data["neighborhood"] = zone
            data.setdefault("raw_data", {})["argencasas_zone"] = zone
        pairs = re.findall(
            r"([\d.,]+(?:\s*x\s*[\d.,]+)?(?:\s*m²)?)\s*\|\s*([^|]+)", joined
        )
        for value, label in pairs:
            label = label.lower()
            if "ambiente" in label:
                data["rooms"] = int(re.search(r"\d+", value).group())
            elif "dormitorio" in label:
                data["bedrooms"] = int(re.search(r"\d+", value).group())
            elif "baño" in label:
                data["bathrooms"] = int(re.search(r"\d+", value).group())
        metric_text = soup.get_text(" | ", strip=True)
        metric_updates = {
            "rooms": metric_before_label(metric_text, "Ambientes", parse_int),
            "bedrooms": metric_before_label(metric_text, "Dormitorios", parse_int),
            "bathrooms": metric_before_label(metric_text, r"Ba(?:ños|nos)", parse_decimal),
            "covered_area": metric_before_label(metric_text, "Sup Cubierta", parse_decimal),
            "total_area": metric_before_label(metric_text, "Sup Total", parse_decimal),
            "uncovered_area": metric_before_label(metric_text, "Sup Libre", parse_decimal),
            "age_years": metric_before_label(metric_text, r"A(?:ños|nos)", parse_int),
        }
        evidence = {}
        for field, value in metric_updates.items():
            if value is not None:
                data[field] = value
                evidence[field] = str(value)
        if evidence:
            data.setdefault("raw_data", {})["argencasas_metrics"] = evidence
        return data
