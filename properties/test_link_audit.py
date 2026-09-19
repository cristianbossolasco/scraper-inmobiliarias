import tempfile
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.test import TestCase

from properties.models import Listing, Property, Source


class LinkAuditTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(slug='sample', name='Sample')
        self.prop = Property.objects.create(fingerprint='audit', title='Casa', address='Manual 123', manual_overrides={'address': True})
        self.listing = Listing.objects.create(source=self.source, property=self.prop, external_id='1', url='https://example.com/1')

    def run_audit(self, data=None, error=None, apply=True):
        with tempfile.TemporaryDirectory() as folder, patch('properties.management.commands.audit_listing_links.get_adapter') as adapter:
            adapter.return_value.parse.return_value = data
            adapter.return_value.parse.side_effect = error
            with chdir(folder):
                call_command('audit_listing_links', apply=apply, report=str(Path(folder) / 'report.jsonl'))
        self.listing.refresh_from_db()
        self.prop.refresh_from_db()

    def test_dry_run_preserves_data(self):
        self.run_audit({'status': 'removed'}, apply=False)
        self.assertTrue(self.listing.active)

    def test_confirmed_gone_preserves_manual_address(self):
        response = requests.Response()
        response.status_code = 410
        self.run_audit(error=requests.HTTPError(response=response))
        self.assertFalse(self.listing.active)
        self.assertEqual(self.prop.status, 'removed')
        self.assertEqual(self.prop.address, 'Manual 123')

    def test_timeout_does_not_remove(self):
        self.run_audit(error=requests.Timeout('timeout'))
        self.assertTrue(self.listing.active)

    def test_other_active_listing_preserves_property(self):
        Listing.objects.create(source=self.source, property=self.prop, external_id='2', url='https://example.com/2')
        from properties.management.commands.audit_listing_links import retire_listing
        retire_listing(self.listing.pk, 'removed')
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.status, 'active')

    def test_unknown_page_does_not_remove(self):
        self.run_audit(None)
        self.assertTrue(self.listing.active)

    def test_manual_status_is_preserved(self):
        self.prop.manual_overrides = {'status': True}
        self.prop.save(update_fields=['manual_overrides'])
        self.run_audit({'status': 'removed'})
        self.assertFalse(self.listing.active)
        self.assertEqual(self.prop.status, 'active')
