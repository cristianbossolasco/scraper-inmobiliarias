import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


USER_AGENT = "HurlinghamPropertyResearch/1.0 (local personal research)"
SOURCE_THROTTLES = {}
SOURCE_THROTTLES_LOCK = threading.Lock()
ROBOTS_CACHE = {}
ROBOTS_CACHE_LOCK = threading.Lock()


@dataclass
class SourceDefinition:
    slug: str
    name: str
    base_url: str
    search_url: str
    crawl_delay: int = 2
    enabled: bool = True
    notes: str = ""


class BaseScraper(ABC):
    definition: SourceDefinition

    def __init__(
        self,
        max_pages=None,
        session=None,
        request_timeout=None,
        max_listings=None,
        start_page=None,
        should_cancel=None,
    ):
        self.max_pages = max_pages
        self.max_listings = max_listings
        self.start_page = start_page or 1
        self.should_cancel = should_cancel or (lambda: False)
        self.session = session or requests.Session()
        self.request_timeout = request_timeout or 25
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
            }
        )
        self._last_request_at = 0.0
        self._robots = None

    def throttle(self):
        slug = self.definition.slug
        with SOURCE_THROTTLES_LOCK:
            state = SOURCE_THROTTLES.setdefault(slug, {"lock": threading.Lock(), "last_request_at": 0.0})
        with state["lock"]:
            elapsed = time.monotonic() - state["last_request_at"]
            delay = self.definition.crawl_delay
            if elapsed < delay:
                time.sleep(delay - elapsed)
            state["last_request_at"] = time.monotonic()

    def robots(self):
        if self._robots is not None:
            return self._robots
        parsed = urlparse(self.definition.base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        with ROBOTS_CACHE_LOCK:
            cached = ROBOTS_CACHE.get(robots_url)
        if cached is not None:
            self._robots = cached
            return cached
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = self.session.get(robots_url, timeout=15)
            if response.ok:
                parser.parse(response.text.splitlines())
            else:
                parser.parse([])
        except requests.RequestException:
            parser.parse([])
        with ROBOTS_CACHE_LOCK:
            ROBOTS_CACHE[robots_url] = parser
        self._robots = parser
        return parser

    def allowed(self, url):
        return self.robots().can_fetch(USER_AGENT, url)

    def get(self, url):
        if not self.allowed(url):
            raise PermissionError(f"robots.txt no permite acceder a {url}")
        self.throttle()
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=self.request_timeout)
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise last_error

    def soup(self, url):
        return BeautifulSoup(self.get(url).text, "lxml")

    @abstractmethod
    def discover(self):
        raise NotImplementedError

    @abstractmethod
    def parse(self, url):
        raise NotImplementedError

    def scrape(self):
        for url in self.discover():
            data = self.parse(url)
            if data is not None:
                yield data

    def absolute(self, url):
        return urljoin(self.definition.base_url, url)
