import re
from math import ceil


def declared_total_from_text(text):
    patterns = (
        r"\bFound\s+([\d.]+)\s+results\b",
        r"\bShowing\s+\d+\s+to\s+\d+\s+properties\s+of\s+([\d.]+)\s+found\b",
        r"\b[\d.]+\s+a\s+[\d.]+\s+de\s+([\d.]+)\s+(?:propiedades|inmuebles)\b",
        r"\b([\d.]+)\s+(?:casas|propiedades|inmuebles)\s+en\s+venta\b",
        r"\b([\d.]+)\s+Results?\s+Found\b",
        r"\b([\d.]+)\s+Resultados?\s+(?:de\s+b[uú]squeda|encontrados?)\b",
        r"\b(?:Se\s+encontraron|Encontramos)\s+([\d.]+)\s+resultados\b",
        r"\bMostrando\s+[\d.]+\s+a\s+[\d.]+\s+propiedades\s+de\s+([\d.]+)\s+encontradas\b",
        r"\b(?:Se\s+encontraron|Encontramos)\s+([\d.]+)\s+propiedades\b",
        r"\b([\d.]+)\s+propiedades\s+encontradas?\b",
        r"\b([\d.]+)\s+Propiedades\s+e\s+inmuebles\b",
        r"\bEn\s+.+?\s+tenemos\s+([\d.]+)\s+Propiedades\b",
        r"\bArgentina\s*\(([\d.]+)\)\s+Tipo\s+de\s+operaci[oó]n\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            return int(match.group(1).replace(".", ""))
    return None


def max_page_from_markup(markup):
    pages = [
        int(match.group(1))
        for match in re.finditer(
            r"(?:pagina-|/page/|(?:[?&]|&amp;)page=)(\d+)",
            markup or "",
            re.I,
        )
    ]
    return max(pages) if pages else None


def paginated_discover(scraper, first_url, page_url, listing_urls, fallback_max_pages=80, empty_page_stop=3):
    seen = set()
    start_page = max(getattr(scraper, "start_page", None) or 1, 1)
    limited_by_pages = scraper.max_pages is not None
    limited_by_listings = scraper.max_listings is not None
    limited_run = start_page > 1 or limited_by_pages or limited_by_listings

    if scraper.should_cancel():
        scraper.discovery_stats = {
            "cancelled": True,
            "declared_total": None,
            "pages_seen": 0,
            "urls_discovered": 0,
            "coverage_ratio": None,
            "limited_by_max_listings": False,
            "limited_by_max_pages": limited_by_pages,
        }
        return

    first_soup = None
    first_page_urls = None
    declared_total = None
    if not limited_run or start_page == 1:
        first_soup = scraper.soup(first_url)
    if first_soup is not None and start_page == 1:
        first_text = first_soup.get_text(" ", strip=True)
        declared_total = declared_total_from_text(first_text)
        first_page_urls = list(dict.fromkeys(listing_urls(first_soup)))

    if not limited_run:
        max_page = max_page_from_markup(str(first_soup)) or fallback_max_pages
        if declared_total and first_page_urls:
            max_page = max(max_page, ceil(declared_total / len(first_page_urls)))
    elif scraper.max_pages is not None:
        max_page = start_page + scraper.max_pages - 1
    else:
        max_page = fallback_max_pages

    if scraper.max_pages is not None:
        max_page = min(max_page, start_page + scraper.max_pages - 1)

    empty_pages = 0
    pages_seen = 0
    for page in range(start_page, max_page + 1):
        if scraper.should_cancel():
            break
        try:
            soup = first_soup if page == 1 and first_soup is not None else scraper.soup(page_url(page))
        except Exception:
            scraper.discovery_stats = {
                "cancelled": scraper.should_cancel(),
                "declared_total": declared_total,
                "pages_seen": pages_seen,
                "urls_discovered": len(seen),
                "coverage_ratio": None,
                "limited_by_max_listings": False,
                "limited_by_max_pages": limited_by_pages,
            }
            raise
        pages_seen += 1
        page_new = 0
        urls = first_page_urls if page == 1 and first_page_urls is not None else listing_urls(soup)
        for url in urls:
            if scraper.should_cancel():
                scraper.discovery_stats = {
                    "cancelled": True,
                    "declared_total": declared_total,
                    "pages_seen": pages_seen,
                    "urls_discovered": len(seen),
                    "coverage_ratio": None,
                    "limited_by_max_listings": False,
                    "limited_by_max_pages": limited_by_pages,
                }
                return
            if url in seen:
                continue
            seen.add(url)
            page_new += 1
            yield url
            if scraper.max_listings is not None and len(seen) >= scraper.max_listings:
                scraper.discovery_stats = {
                    "declared_total": declared_total,
                    "pages_seen": pages_seen,
                    "urls_discovered": len(seen),
                    "coverage_ratio": None,
                    "limited_by_max_listings": True,
                    "limited_by_max_pages": limited_by_pages,
                }
                return
        if page_new == 0:
            empty_pages += 1
            if scraper.max_pages is None and empty_pages >= empty_page_stop:
                break
        else:
            empty_pages = 0
    scraper.discovery_stats = {
        "cancelled": scraper.should_cancel(),
        "declared_total": declared_total,
        "pages_seen": pages_seen,
        "urls_discovered": len(seen),
        "coverage_ratio": (
            round((len(seen) / declared_total) * 100, 1)
            if declared_total and not limited_run
            else None
        ),
        "limited_by_max_listings": False,
        "limited_by_max_pages": limited_by_pages,
    }


def ajax_paginated_discover(
    scraper,
    first_url,
    page_url,
    listing_urls,
    fallback_max_pages=80,
    empty_page_stop=1,
):
    seen = set()
    start_page = max(getattr(scraper, "start_page", None) or 1, 1)
    limited_by_pages = scraper.max_pages is not None
    limited_by_listings = scraper.max_listings is not None
    limited_run = start_page > 1 or limited_by_pages or limited_by_listings

    if scraper.should_cancel():
        scraper.discovery_stats = {
            "cancelled": True,
            "declared_total": None,
            "pages_seen": 0,
            "urls_discovered": 0,
            "coverage_ratio": None,
            "limited_by_max_listings": False,
            "limited_by_max_pages": limited_by_pages,
        }
        return

    declared_total = None
    first_soup = None
    first_page_urls = None
    if not limited_run or start_page == 1:
        first_soup = scraper.soup(first_url)
    if first_soup is not None and start_page == 1:
        first_text = first_soup.get_text(" ", strip=True)
        declared_total = declared_total_from_text(first_text)
        first_page_urls = list(dict.fromkeys(listing_urls(first_soup)))

    if not limited_run:
        if declared_total and first_page_urls:
            max_page = max(ceil(declared_total / len(first_page_urls)), 1)
        else:
            max_page = fallback_max_pages
    elif scraper.max_pages is not None:
        max_page = start_page + scraper.max_pages - 1
    else:
        max_page = fallback_max_pages

    if scraper.max_pages is not None:
        max_page = min(max_page, start_page + scraper.max_pages - 1)

    empty_pages = 0
    pages_seen = 0
    for page in range(start_page, max_page + 1):
        if scraper.should_cancel():
            break
        if page == 1:
            soup = first_soup if first_soup is not None else scraper.soup(first_url)
            urls = first_page_urls if first_page_urls is not None else list(listing_urls(soup))
        else:
            try:
                soup = scraper.soup(page_url(page))
            except Exception:
                scraper.discovery_stats = {
                    "cancelled": scraper.should_cancel(),
                    "declared_total": declared_total,
                    "pages_seen": pages_seen,
                    "urls_discovered": len(seen),
                    "coverage_ratio": None,
                    "limited_by_max_listings": False,
                    "limited_by_max_pages": limited_by_pages,
                }
                raise
            urls = list(listing_urls(soup))
        pages_seen += 1
        page_new = 0
        for url in urls:
            if scraper.should_cancel():
                scraper.discovery_stats = {
                    "cancelled": True,
                    "declared_total": declared_total,
                    "pages_seen": pages_seen,
                    "urls_discovered": len(seen),
                    "coverage_ratio": None,
                    "limited_by_max_listings": False,
                    "limited_by_max_pages": limited_by_pages,
                }
                return
            if url in seen:
                continue
            seen.add(url)
            page_new += 1
            yield url
            if scraper.max_listings is not None and len(seen) >= scraper.max_listings:
                scraper.discovery_stats = {
                    "declared_total": declared_total,
                    "pages_seen": pages_seen,
                    "urls_discovered": len(seen),
                    "coverage_ratio": None,
                    "limited_by_max_listings": True,
                    "limited_by_max_pages": limited_by_pages,
                }
                return
        if page_new == 0:
            empty_pages += 1
            if scraper.max_pages is None and empty_pages >= empty_page_stop:
                break
        else:
            empty_pages = 0

    scraper.discovery_stats = {
        "cancelled": scraper.should_cancel(),
        "declared_total": declared_total,
        "pages_seen": pages_seen,
        "urls_discovered": len(seen),
        "coverage_ratio": (
            round((len(seen) / declared_total) * 100, 1)
            if declared_total and not limited_run
            else None
        ),
        "limited_by_max_listings": False,
        "limited_by_max_pages": limited_by_pages,
    }
