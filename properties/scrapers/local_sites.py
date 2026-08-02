import re
from urllib.parse import urlencode, urlparse

from properties.models import Listing, Property
from .base import BaseScraper, SourceDefinition
from .paginated import (
    ajax_paginated_discover,
    paginated_discover,
)
from .parsing import basic_html_data, clean_text, first_map_coordinate, text_value
from properties.services.location_enrichment import clean_detected_address
from properties.services.normalization import (
    canonical_address_alias,
    classify_address_precision,
    infer_property_type,
    known_neighborhood_name,
    normalize_address,
    parse_decimal,
    parse_int,
)


ADDRESS_NOISE_RE = re.compile(
    r"\b(?:tel|inicio|destacados|emprendimientos|servicios|quienes\s+somos|contacto|ver\s+tel|whatsapp)\b",
    re.I,
)
BECERRA_RETIRED_RE = re.compile(
    r"whoops!\s+we\s+seem\s+to\s+have\s+hit\s+a\s+snag|propiedad\s+(?:retirada|no\s+disponible)",
    re.I,
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
        enabled=True,
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
        if BECERRA_RETIRED_RE.search(text):
            data["status"] = Property.Status.REMOVED
            data["source_status"] = "removed"
            data.setdefault("raw_data", {})["becerra_retired_text"] = True
            data["agency"] = self.definition.name
            data["operation"] = "sale"
            return data
        address = self._address_from_soup(soup) or self._address_from_text(data.get("title") or "", text)
        if address:
            data["address"] = address[:250]
            data["detected_address"] = address[:250]
        data["locality"] = "Hurlingham"
        neighborhood = known_neighborhood_name(text)
        if neighborhood and neighborhood != data["locality"]:
            data["neighborhood"] = neighborhood
        coordinate = first_map_coordinate(str(soup))
        if coordinate:
            data["latitude"] = coordinate["latitude"]
            data["longitude"] = coordinate["longitude"]
            data.setdefault("raw_data", {})["becerra_map_coordinate"] = coordinate
        data["agency"] = self.definition.name
        data["operation"] = "sale"
        data["location_precision"] = classify_address_precision(data.get("address"))
        return data

    def _clean_address_candidate(self, value):
        address = clean_detected_address(value or "")
        if not address or ADDRESS_NOISE_RE.search(address):
            return ""
        if len(address) > 90:
            return ""
        return canonical_address_alias(address)

    def _address_from_soup(self, soup):
        for node in soup.select("h3, .item-address, .property-address, .address"):
            address = self._clean_address_candidate(node.get_text(" ", strip=True))
            if address and re.search(r"\d{2,5}\b", address):
                return address
        return ""

    def _address_from_text(self, title, text):
        candidates = []
        title = clean_text(title)
        if title:
            after_title = text.split(title, 1)[-1]
            candidates.append(after_title[:240])
        candidates.append(text[:1200])
        for candidate in candidates:
            direct = text_value(
                candidate,
                [
                    r"([^|]{3,}?\s+(?:al\s+)?\d{2,5})\s+G\.?B\.?A\.?",
                    r"([^|]{3,}?\s+(?:al\s+)?\d{2,5})\s*\|",
                ],
            )
            if direct:
                if title and direct.startswith(title):
                    direct = direct[len(title):]
                address = self._clean_address_candidate(direct)
                if address:
                    return address
            value = text_value(
                candidate,
                [
                    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{3,}?\s+(?:al\s+)?\d{2,5})\s+(?:Hurlingham|Villa\s+Tesei|William\s+C\.?\s+Morris)\b",
                    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{3,}?\s+(?:al\s+)?\d{2,5})\s*\|",
                ],
            )
            if value:
                address = self._clean_address_candidate(value)
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
        enabled=True,
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
    api_base_url = "https://crm.faellainmuebles.com.ar"
    list_limit = 12
    city_filter = "Hurlingham"
    operation_filter = "sale"
    category_filters = ("MLA401685", "MLA401686", "MLA1473")
    category_type_map = {
        "MLA401685": Property.Type.HOUSE,
        "MLA401686": Property.Type.APARTMENT,
        "MLA1473": Property.Type.APARTMENT,
    }
    definition = SourceDefinition(
        slug="faella",
        name="Faella Propiedades",
        base_url="https://faellainmuebles.com.ar",
        search_url="https://faellainmuebles.com.ar/propiedades",
        crawl_delay=2,
        enabled=True,
        notes=(
            "Frontend Next.js con API publica del CRM; se descubren ventas en Hurlingham "
            "filtradas a casas y departamentos."
        ),
    )

    def _api_url(self, path, params=None):
        url = f"{self.api_base_url}{path}"
        if params:
            query = urlencode(params, doseq=True)
            return f"{url}?{query}"
        return url

    def _list_params(self, page):
        return {
            "page": page,
            "limit": self.list_limit,
            "city": self.city_filter,
            "operation": self.operation_filter,
            "categoryId": list(self.category_filters),
        }

    def _list_url(self, page):
        return self._api_url("/api/public/properties", self._list_params(page))

    def _detail_url(self, external_id):
        return self._api_url(f"/api/public/properties/{external_id}")

    def _public_url(self, external_id):
        return f"{self.definition.search_url}/{external_id}"

    def json_payload(self, url):
        return self.get(url).json()

    def _detail_payload(self, external_id):
        if not hasattr(self, "_detail_payload_cache"):
            self._detail_payload_cache = {}
        external_id = str(external_id)
        if external_id not in self._detail_payload_cache:
            payload = self.json_payload(self._detail_url(external_id)).get("property") or {}
            self._detail_payload_cache[external_id] = payload
        return self._detail_payload_cache[external_id]

    def _external_id_from_url(self, url):
        parsed = urlparse(url)
        external_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not external_id or external_id == "propiedades":
            raise ValueError(f"URL Faella sin ID CRM: {url}")
        return external_id

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

        first_payload = self.json_payload(self._list_url(start_page))
        pagination = first_payload.get("pagination") or {}
        declared_total = pagination.get("total")
        total_pages = pagination.get("totalPages") or start_page
        max_page = start_page + self.max_pages - 1 if self.max_pages is not None else total_pages
        pages_seen = 0
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
            payload = first_payload if page == start_page else self.json_payload(self._list_url(page))
            pages_seen += 1
            page_new = 0
            for item in payload.get("data") or []:
                if item.get("operation") != self.operation_filter:
                    continue
                if item.get("city") != self.city_filter:
                    continue
                if item.get("categoryId") not in self.category_filters:
                    continue
                external_id = str(item.get("id") or "")
                if not external_id:
                    continue
                url = self._public_url(external_id)
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
            if not page_new and self.max_pages is None and page >= total_pages:
                break

        self._set_discovery_stats(
            declared_total,
            pages_seen,
            len(seen),
            limited_by_max_pages,
            limited_run=limited_run,
        )

    def _locality_from_text(self, text):
        if re.search(r"william\s+(?:c\.\s*)?morris", text, re.I):
            return "William C. Morris"
        if re.search(r"(?:villa\s+)?(?:santos\s+)?tesei|\bciudad\s+tesei\b", text, re.I):
            return "Villa Tesei"
        return "Hurlingham"

    def _address_from_location(self, location):
        if not isinstance(location, dict):
            return ""
        text = ", ".join(
            clean_text(location.get(key) or "")
            for key in ("address", "city", "state")
            if location.get(key)
        )
        candidate = clean_detected_address(location.get("address") or "")
        if not candidate or not re.search(r"\d{2,5}\b", candidate):
            return ""
        if not re.search(r"\b(Hurlingham|Villa\s+Tesei|William\s+(?:C\.?\s*)?Morris)\b", text or "", re.I):
            return ""
        address = canonical_address_alias(candidate)
        return address[:250] if address and not ADDRESS_NOISE_RE.search(address) else ""

    def _property_type(self, payload):
        return self.category_type_map.get(payload.get("categoryId")) or infer_property_type(
            payload.get("title"), payload.get("description")
        )

    def _legacy_listing_for_payload(self, payload, crm_external_id):
        address = self._address_from_location(payload.get("location") or {})
        normalized_address = normalize_address(address) if address else ""
        price = parse_decimal(payload.get("price"))
        property_type = self._property_type(payload)
        if not normalized_address or price is None or not property_type:
            return None
        candidates = (
            Listing.objects.select_related("property")
            .filter(
                source__slug=self.definition.slug,
                active=True,
                property__property_type=property_type,
                property__price=price,
            )
            .exclude(external_id=str(crm_external_id))
        )
        currency = payload.get("currency") or ""
        if currency:
            candidates = candidates.filter(property__currency=currency)
        locality = self._locality_from_text(
            " ".join(
                str(value or "")
                for value in (
                    payload.get("title"),
                    address,
                    payload.get("city"),
                )
            )
        )
        if locality:
            candidates = candidates.filter(property__locality=locality)
        matches = [
            listing
            for listing in candidates
            if normalize_address(listing.property.address or listing.property.detected_address)
            == normalized_address
        ]
        return matches[0] if len(matches) == 1 else None

    def discovery_external_id_from_url(self, url):
        crm_external_id = self._external_id_from_url(url)
        payload = self._detail_payload(crm_external_id)
        legacy_listing = self._legacy_listing_for_payload(payload, crm_external_id)
        return legacy_listing.external_id if legacy_listing else crm_external_id

    def parse(self, url):
        external_id = self._external_id_from_url(url)
        detail_endpoint = self._detail_url(external_id)
        payload = self._detail_payload(external_id)
        if not payload:
            raise ValueError(f"No se encontro propiedad Faella {external_id}")
        if payload.get("operation") and payload.get("operation") != self.operation_filter:
            return None
        if payload.get("city") and payload.get("city") != self.city_filter:
            return None
        if payload.get("categoryId") and payload.get("categoryId") not in self.category_filters:
            return None
        title = clean_text(payload.get("title") or "")
        description = clean_text(payload.get("description") or "")
        location = payload.get("location") or {}
        address = self._address_from_location(location)
        location_text = ", ".join(
            clean_text(location.get(key) or "")
            for key in ("address", "city", "state")
            if location.get(key)
        )
        images = []
        for photo in sorted(payload.get("photos") or [], key=lambda item: item.get("order", 0)):
            image_url = photo.get("url")
            if image_url:
                images.append(image_url)
        if payload.get("thumbnail"):
            images.insert(0, payload["thumbnail"])

        neighborhood = known_neighborhood_name(title)
        locality = self._locality_from_text(f"{title} {location_text} {payload.get('city') or ''}")
        if neighborhood == locality or neighborhood == "Hurlingham":
            neighborhood = ""

        data = {
            "external_id": external_id,
            "url": self._public_url(external_id),
            "title": title or "Propiedad Faella",
            "description": description,
            "address": address,
            "detected_address": address,
            "agency": self.definition.name,
            "property_type": self._property_type(payload),
            "operation": payload.get("operation") or self.operation_filter,
            "locality": locality,
            "neighborhood": neighborhood,
            "currency": payload.get("currency") or "",
            "price": parse_decimal(payload.get("price")),
            "rooms": text_value(title, [r"(\d+)\s*amb"], parse_int),
            "bedrooms": parse_int(payload.get("bedrooms")),
            "bathrooms": parse_decimal(payload.get("bathrooms")),
            "total_area": parse_decimal(payload.get("surfaceTotal")),
            "features": payload.get("amenities") or [],
            "images": list(dict.fromkeys(images))[:30],
            "status": Property.Status.ACTIVE,
            "location_precision": classify_address_precision(address),
            "raw_data": {
                "faella_detail_endpoint": detail_endpoint,
                "faella_public_url": self._public_url(external_id),
                "crm_external_id": payload.get("id") or external_id,
                "faella_filters": self._list_params(1),
                "category_id": payload.get("categoryId"),
                "property_type": payload.get("propertyType"),
                "location_text": location_text,
                "payload": payload,
            },
        }
        return data
