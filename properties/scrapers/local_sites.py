import re

from .base import BaseScraper, SourceDefinition
from .paginated import ajax_paginated_discover, paginated_discover
from .parsing import basic_html_data, clean_text, text_value
from properties.services.location_enrichment import clean_detected_address
from properties.services.normalization import classify_address_precision


class LinkDetailScraper(BaseScraper):
    link_pattern = ""

    def discover(self):
        soup = self.soup(self.definition.search_url)
        seen = set()
        for anchor in soup.select(self.link_pattern):
            url = self.absolute(anchor["href"])
            if url not in seen:
                seen.add(url)
                yield url

    def parse(self, url):
        data = basic_html_data(self.soup(url), url)
        data["locality"] = "Hurlingham"
        data["agency"] = self.definition.name
        data["operation"] = "sale"
        return data


class BecerraScraper(LinkDetailScraper):
    definition = SourceDefinition(
        slug="becerra",
        name="Becerra Propiedades",
        base_url="https://becerrapropiedades.com",
        search_url="https://becerrapropiedades.com/buscador?ubicaciones=Hurlingham&operaciones=Venta&moneda=USD",
        crawl_delay=3,
        enabled=False,
        notes="Busqueda publica con paginacion page=N para ventas en Hurlingham.",
    )
    link_pattern = 'a[href*="/ficha/"]'

    def _page_url(self, page):
        if page == 1:
            return self.definition.search_url
        return f"{self.definition.search_url}&page={page}"

    def _listing_urls(self, soup):
        for anchor in soup.select(self.link_pattern):
            yield self.absolute(anchor["href"])

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
        data = basic_html_data(soup, url)
        text = clean_text(soup.get_text(" ", strip=True))
        address = self._address_from_text(data.get("title") or "", text)
        if address:
            data["address"] = address[:250]
        data["locality"] = "Hurlingham"
        data["agency"] = self.definition.name
        data["operation"] = "sale"
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data

    def _address_from_text(self, title, text):
        candidates = []
        title = clean_text(title)
        if title:
            after_title = text.split(title, 1)[-1]
            candidates.append(after_title[:240])
        candidates.append(text[:1200])
        for candidate in candidates:
            value = text_value(
                candidate,
                [
                    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{3,}?\s+(?:al\s+)?\d{2,5})\s+(?:Hurlingham|Villa\s+Tesei|William\s+C\.?\s+Morris)\b",
                    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{3,}?\s+(?:al\s+)?\d{2,5})\s*\|",
                ],
            )
            if value:
                address = clean_detected_address(value)
                if address:
                    return address
        return ""


class AliagaScraper(LinkDetailScraper):
    definition = SourceDefinition(
        slug="aliaga",
        name="Aliaga Propiedades",
        base_url="https://www.aliagapropiedades.com",
        search_url="https://www.aliagapropiedades.com/Buscar?operation=1&locations=25973&o=2,2&1=1",
        crawl_delay=3,
        enabled=False,
        notes="Motor Tokko publico para ventas en Hurlingham; primera pagina HTML y resto por AJAX p=N.",
    )
    link_pattern = 'a[href*="/p/"]'

    tokko_query = (
        "q=&currency=ANY&minprice=&maxprice=&minsurface=&maxsurface="
        "&minrooms=&maxrooms=&minbedrooms=&maxbedrooms=&operation=1"
        "&locations=25973&location_type=&ptypes=&o=2,2&watermark="
    )

    def _ajax_url(self, page):
        return f"{self.definition.base_url}/Buscar?{self.tokko_query}&p={page}"

    def _listing_urls(self, soup):
        for anchor in soup.select(self.link_pattern):
            yield self.absolute(anchor["href"])

    def discover(self):
        yield from ajax_paginated_discover(
            self,
            self.definition.search_url,
            self._ajax_url,
            self._listing_urls,
            fallback_max_pages=10,
        )
