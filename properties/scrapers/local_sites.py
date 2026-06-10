from .base import BaseScraper, SourceDefinition
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
        return data


class BecerraScraper(LinkDetailScraper):
    definition = SourceDefinition(
        slug="becerra",
        name="Becerra Propiedades",
        base_url="https://becerrapropiedades.com",
        search_url="https://becerrapropiedades.com",
        crawl_delay=3,
        enabled=False,
        notes="Ficha limpia, pero el servidor devuelve errores intermitentes a clientes automatizados.",
    )
    link_pattern = 'a[href*="/ficha/"]'


class AliagaScraper(LinkDetailScraper):
    definition = SourceDefinition(
        slug="aliaga",
        name="Aliaga Propiedades",
        base_url="https://www.aliagapropiedades.com",
        search_url="https://www.aliagapropiedades.com/propiedades",
        crawl_delay=3,
        enabled=False,
        notes="Fichas estructuradas; falta confirmar un listado estable para Hurlingham.",
    )
    link_pattern = 'a[href*="/p/"]'
