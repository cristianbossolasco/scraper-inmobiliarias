from .base import BaseScraper, SourceDefinition
from .paginated import ajax_paginated_discover, paginated_discover
from .parsing import basic_html_data


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
