import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from properties.models import Listing, Property, Source
from properties.services.argenprop_browser import BrowserBlocked


class ArgenpropBrowserCommandTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(slug='argenprop', name='Argenprop')
        self.url = 'https://www.argenprop.com/casa-en-venta--123'
        self.data = {'external_id': '123', 'url': self.url, 'title': 'Casa', 'address': 'Otra 100', 'locality': 'Hurlingham', 'price': 100000, 'currency': 'USD', 'status': 'active'}

    def run_mock(self, path, apply=False, blocked=False):
        with patch('properties.management.commands.scrape_argenprop_browser.ArgenpropBrowser'), patch('properties.management.commands.scrape_argenprop_browser.BrowserArgenpropScraper') as factory:
            scraper = factory.return_value
            scraper.discover.return_value = iter([self.url])
            scraper.soup.return_value = BeautifulSoup('<h1>Casa</h1>Código de aviso: 123', 'lxml')
            scraper.parse_soup.return_value = self.data.copy()
            if blocked:
                scraper.soup.side_effect = BrowserBlocked('403')
            call_command('scrape_argenprop_browser', state=str(path), apply=apply)
            return scraper

    def test_dry_run_does_not_write_database(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_mock(Path(folder) / 'state.json')
        self.assertFalse(Listing.objects.exists())

    def test_resume_does_not_repeat_completed_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            self.run_mock(path)
            scraper = self.run_mock(path)
            scraper.soup.assert_not_called()
            scraper.discover.assert_not_called()

    def test_apply_preserves_manual_address_and_includes_historical_urls(self):
        prop = Property.objects.create(fingerprint='manual', title='Casa', address='Manual 500', manual_overrides={'address': True})
        Listing.objects.create(source=self.source, property=prop, external_id='123', url=self.url)
        with tempfile.TemporaryDirectory() as folder:
            self.run_mock(Path(folder) / 'state.json', apply=True)
        prop.refresh_from_db()
        self.assertEqual(prop.address, 'Manual 500')
        self.assertEqual(prop.price, 100000)

    def test_block_does_not_complete_pending_url(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            with self.assertRaises(CommandError):
                self.run_mock(path, blocked=True)
            self.assertEqual(json.loads(path.read_text())['results'], {})
            self.assertFalse(path.with_suffix('.lock').exists())
