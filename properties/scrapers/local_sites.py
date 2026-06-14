import re
from math import ceil
from urllib.parse import parse_qs, urlparse

from properties.models import Property
from .base import BaseScraper, SourceDefinition
from .paginated import (
    ajax_paginated_discover,
    declared_total_from_text,
    max_page_from_markup,
    paginated_discover,
)
from .parsing import basic_html_data, clean_text, text_value
from properties.services.location_enrichment import clean_detected_address
from properties.services.normalization import (
    classify_address_precision,
    infer_property_type,
    known_neighborhood_name,
    normalize_currency,
    parse_decimal,
    parse_int,
)


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


class FaellaScraper(BaseScraper):
    definition = SourceDefinition(
        slug="faella",
        name="Faella Propiedades",
        base_url="https://faellainmuebles.com.ar",
        search_url="https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham",
        crawl_delay=2,
        enabled=True,
        notes=(
            "Vidriera publica de ventas en Hurlingham; se parsean tarjetas propias "
            "y se conserva el permalink externo de MercadoLibre sin consultar MercadoLibre."
        ),
    )

    def _page_url(self, page):
        if page <= 1:
            return self.definition.search_url
        return f"{self.definition.search_url}&page={page}"

    def _meli_id(self, value):
        match = re.search(r"\bMLA-?(\d+)\b", value or "", re.I)
        return f"MLA{match.group(1)}" if match else ""

    def _synthetic_url(self, page, href):
        meli_id = self._meli_id(href)
        return f"{self._page_url(page)}#{meli_id}" if meli_id else ""

    def _listing_urls(self, soup, page):
        for card in soup.select(".card"):
            anchor = card.select_one("a.card-link[href]")
            if not anchor:
                continue
            text = clean_text(card.get_text(" ", strip=True))
            if re.search(r"\balquiler\b", text, re.I) and not re.search(r"\bventa\b", text, re.I):
                continue
            url = self._synthetic_url(page, anchor.get("href", ""))
            if url:
                yield url

    def _set_discovery_stats(
        self,
        declared_total,
        pages_seen,
        urls_discovered,
        limited_by_max_pages,
        limited_by_max_listings=False,
        cancelled=False,
        limited_run=False,
    ):
        self.discovery_stats = {
            "cancelled": cancelled,
            "declared_total": declared_total,
            "pages_seen": pages_seen,
            "urls_discovered": urls_discovered,
            "coverage_ratio": (
                round((urls_discovered / declared_total) * 100, 1)
                if declared_total and not limited_run
                else None
            ),
            "limited_by_max_listings": limited_by_max_listings,
            "limited_by_max_pages": limited_by_max_pages,
        }

    def discover(self):
        seen = set()
        start_page = max(self.start_page or 1, 1)
        limited_by_max_pages = self.max_pages is not None
        limited_run = start_page > 1 or limited_by_max_pages or self.max_listings is not None

        if self.should_cancel():
            self._set_discovery_stats(None, 0, 0, limited_by_max_pages, cancelled=True, limited_run=limited_run)
            return

        first_soup = self.soup(self._page_url(start_page))
        first_urls = list(dict.fromkeys(self._listing_urls(first_soup, start_page)))
        declared_total = declared_total_from_text(first_soup.get_text(" ", strip=True))

        if self.max_pages is not None:
            max_page = start_page + self.max_pages - 1
        else:
            markup_max_page = max_page_from_markup(str(first_soup))
            declared_max_page = ceil(declared_total / len(first_urls)) if declared_total and first_urls else None
            max_page = max(page for page in (markup_max_page, declared_max_page, start_page) if page)

        pages_seen = 0
        empty_pages = 0
        for page in range(start_page, max_page + 1):
            if self.should_cancel():
                self._set_discovery_stats(
                    declared_total,
                    pages_seen,
                    len(seen),
                    limited_by_max_pages,
                    cancelled=True,
                    limited_run=limited_run,
                )
                return
            soup = first_soup if page == start_page else self.soup(self._page_url(page))
            pages_seen += 1
            page_new = 0
            urls = first_urls if page == start_page else self._listing_urls(soup, page)
            for url in urls:
                if url in seen:
                    continue
                seen.add(url)
                page_new += 1
                yield url
                if self.max_listings is not None and len(seen) >= self.max_listings:
                    self._set_discovery_stats(
                        declared_total,
                        pages_seen,
                        len(seen),
                        limited_by_max_pages,
                        limited_by_max_listings=True,
                        limited_run=True,
                    )
                    return
            if page_new:
                empty_pages = 0
            else:
                empty_pages += 1
                if self.max_pages is None and empty_pages >= 2:
                    break

        self._set_discovery_stats(
            declared_total,
            pages_seen,
            len(seen),
            limited_by_max_pages,
            limited_run=limited_run,
        )

    def _card_for_url(self, url):
        parsed = urlparse(url)
        meli_id = self._meli_id(parsed.fragment or url)
        if not meli_id:
            raise ValueError(f"URL Faella sin ID MLA: {url}")
        try:
            page = int((parse_qs(parsed.query).get("page") or ["1"])[0] or 1)
        except ValueError:
            page = 1
        soup = self.soup(self._page_url(page))
        for card in soup.select(".card"):
            anchor = card.select_one("a.card-link[href]")
            if anchor and self._meli_id(anchor.get("href", "")) == meli_id:
                return page, card, anchor
        raise ValueError(f"No se encontro tarjeta Faella para {meli_id}")

    def _locality_from_text(self, text):
        if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I):
            return "William C. Morris"
        if re.search(r"(?:villa\s+)?(?:santos\s+)?tesei|\bciudad\s+tesei\b", text, re.I):
            return "Villa Tesei"
        return "Hurlingham"

    def parse(self, url):
        page, card, anchor = self._card_for_url(url)
        href = anchor.get("href", "")
        title_node = card.select_one(".card-title")
        price_node = card.select_one(".card-price")
        location_node = card.select_one(".card-location")
        title = clean_text(title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True))
        price_text = clean_text(price_node.get_text(" ", strip=True) if price_node else "")
        location_text = clean_text(location_node.get_text(" ", strip=True) if location_node else "")
        feature_texts = [clean_text(item.get_text(" ", strip=True)) for item in card.select(".feature")]

        currency_match = re.search(r"(USD|U\$S|US\$|ARS|\$)\s*([\d.,]+)", price_text, re.I)
        total_area = None
        bedrooms = None
        bathrooms = None
        for feature in feature_texts:
            if total_area is None and re.search(r"\bm", feature, re.I):
                total_area = text_value(feature, [r"([\d.,]+)\s*m"], parse_decimal)
            if bedrooms is None and re.search(r"\bdorm", feature, re.I):
                bedrooms = text_value(feature, [r"(\d+)\s*dorm"], parse_int)
            if bathrooms is None and re.search(r"\bba", feature, re.I):
                bathrooms = text_value(feature, [r"(\d+(?:[.,]\d+)?)\s*ba"], parse_decimal)

        image_urls = []
        for image in card.select("img[src]"):
            src = image.get("src", "")
            if src and not src.startswith("data:"):
                image_urls.append(self.absolute(src))

        neighborhood = known_neighborhood_name(title)
        locality = self._locality_from_text(f"{title} {location_text}")
        if neighborhood == locality or neighborhood == "Hurlingham":
            neighborhood = ""

        return {
            "external_id": self._meli_id(href),
            "url": href,
            "title": title or "Propiedad Faella",
            "description": "",
            "agency": self.definition.name,
            "property_type": infer_property_type(title),
            "operation": "sale",
            "locality": locality,
            "neighborhood": neighborhood,
            "currency": normalize_currency(currency_match.group(1)) if currency_match else "",
            "price": parse_decimal(currency_match.group(2)) if currency_match else None,
            "rooms": text_value(title, [r"(\d+)\s*amb"], parse_int),
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "total_area": total_area,
            "features": [],
            "images": list(dict.fromkeys(image_urls))[:30],
            "status": Property.Status.ACTIVE,
            "location_precision": classify_address_precision(""),
            "raw_data": {
                "faella_page": self._page_url(page),
                "faella_synthetic_url": url,
                "mercadolibre_permalink": href,
                "location_text": location_text,
                "feature_texts": feature_texts,
                "price_text": price_text,
            },
        }
