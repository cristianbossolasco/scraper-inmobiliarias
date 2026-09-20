"""Isolated browser transport. No personal profiles, cookies, or challenge bypass."""
from concurrent.futures import ThreadPoolExecutor
import re
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from properties.scrapers.argenprop import ArgenpropScraper
from properties.scrapers.base import USER_AGENT


class BrowserBlocked(RuntimeError):
    pass


class ListingGone(RuntimeError):
    pass


class ArgenpropBrowser:
    def __init__(self):
        # Playwright stays in its own thread; Django ORM stays outside its loop.
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.browser = self.runtime = None

    def __enter__(self):
        try:
            self.pool.submit(self._start).result()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def _start(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True, channel='chromium')
        self.page = self.browser.new_page(locale='es-AR')
        response = self.page.goto('https://www.argenprop.com/robots.txt', wait_until='domcontentloaded', timeout=45000)
        if response is None or response.status not in (200, 404):
            status = response.status if response else 'sin respuesta'
            raise BrowserBlocked(f'robots.txt devolvio {status}; no se inicia el barrido.')
        self.robots = RobotFileParser()
        self.robots.parse(self.page.locator('body').inner_text().splitlines() if response.status == 200 else [])

    def read(self, url):
        return self.pool.submit(self._read, url).result()

    def _read(self, url):
        if urlparse(url).netloc != 'www.argenprop.com' or not self.robots.can_fetch(USER_AGENT, url):
            raise BrowserBlocked(f'URL no permitida: {url}')
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        if response is None:
            raise BrowserBlocked('Navegacion sin respuesta HTTP.')
        if response.status in (404, 410):
            raise ListingGone(f'HTTP {response.status}: {url}')
        if response.status >= 400:
            raise BrowserBlocked(f'HTTP {response.status}: {url}')
        expected = re.search(r'--(\d+)$', urlparse(url).path)
        actual = re.search(r'--(\d+)$', urlparse(self.page.url).path)
        if urlparse(self.page.url).netloc != 'www.argenprop.com' or (expected and (not actual or expected[1] != actual[1])):
            raise BrowserBlocked(f'Redireccion inesperada: {url} -> {self.page.url}')
        text = self.page.locator('body').inner_text().lower()
        if any(marker in text for marker in ('verify you are human', 'access denied', 'request blocked', 'verifica que eres humano')):
            raise BrowserBlocked('El sitio requiere verificacion; se detiene sin modificar estados.')
        return self.page.content()

    def __exit__(self, *args):
        try:
            self.pool.submit(self._close).result()
        finally:
            self.pool.shutdown()

    def _close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.runtime:
                self.runtime.stop()


class BrowserArgenpropScraper(ArgenpropScraper):
    def __init__(self, transport, **kwargs):
        super().__init__(**kwargs)
        self.transport = transport

    def soup(self, url):
        self.throttle()
        return BeautifulSoup(self.transport.read(url), 'lxml')
