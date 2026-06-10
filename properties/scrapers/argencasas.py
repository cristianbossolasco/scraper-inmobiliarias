import re

from .base import BaseScraper, SourceDefinition
from .parsing import basic_html_data, json_ld_objects


class ArgencasasScraper(BaseScraper):
    definition = SourceDefinition(
        slug="argencasas",
        name="Argencasas / SIBA",
        base_url="https://www.argencasas.com",
        search_url="https://www.argencasas.com/inmuebles-venta-hurlingham-partido",
        crawl_delay=2,
        notes="Portal SIBA con JSON-LD estructurado y demora solicitada de 2 segundos.",
    )

    def discover(self):
        seen = set()
        page_url = self.definition.search_url
        page = 0
        while page_url and (self.max_pages is None or page < self.max_pages):
            page += 1
            soup = self.soup(page_url)
            for anchor in soup.select('a[href*="/propiedad-"]'):
                url = self.absolute(anchor["href"])
                if url not in seen:
                    seen.add(url)
                    yield url
            next_link = soup.select_one('a[rel="next"]')
            page_url = self.absolute(next_link["href"]) if next_link else None

    def parse(self, url):
        soup = self.soup(url)
        data = basic_html_data(soup, url)
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
        return data
