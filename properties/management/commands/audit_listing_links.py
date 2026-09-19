import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from properties.models import Listing, Property
from properties.scrapers.registry import get_adapter
from properties.services.ingestion import manual_override_fields
from properties.services.scraping import is_listing_gone_error


@transaction.atomic
def retire_listing(listing_id, status):
    listing = Listing.objects.select_related('property').get(pk=listing_id)
    listing.active = False
    listing.source_status = status
    listing.missing_runs = max(2, listing.missing_runs)
    listing.save(update_fields=['active', 'source_status', 'missing_runs'])
    prop = listing.property
    if not prop.listings.filter(active=True).exists() and 'status' not in manual_override_fields(prop):
        prop.status = status
        prop.save(update_fields=['status'])


class Command(BaseCommand):
    help = 'Verifica todas las URLs guardadas, incluidas publicaciones inactivas. Solo aplica bajas confirmadas.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int)
        parser.add_argument('--report')

    def handle(self, *args, **options):
        report = Path(options['report'] or f"logs/link-audit-{timezone.now():%Y%m%d-%H%M%S}.jsonl")
        report.parent.mkdir(parents=True, exist_ok=True)
        lock = Path('.link-audit.lock')
        try:
            lock_file = lock.open('x')
        except FileExistsError as exc:
            raise CommandError('Ya existe un barrido de enlaces; revisar .link-audit.lock.') from exc
        counts = Counter()
        try:
            ids = list(Listing.objects.order_by('source_id', 'id').values_list('pk', flat=True))
            if options['limit'] is not None:
                ids = ids[:options['limit']]
            self.stdout.write(f"{'APPLY' if options['apply'] else 'DRY-RUN'}: {len(ids)} enlaces; informe {report}")
            adapters = {}
            with report.open('w', encoding='utf-8') as output:
                for index, pk in enumerate(ids, 1):
                    listing = Listing.objects.select_related('source').get(pk=pk)
                    row = dict(id=pk, url=listing.url, source=listing.source.slug, checked_at=timezone.now().isoformat())
                    status = None
                    try:
                        slug = listing.source.slug
                        if slug not in adapters:
                            adapters[slug] = get_adapter(slug, request_timeout=25)
                        data = adapters[slug].parse(listing.url)
                        status = (data or {}).get('status')
                        if status in {Property.Status.REMOVED, Property.Status.SUSPENDED, Property.Status.SOLD}:
                            row['result'] = 'unavailable'
                        elif data and status == Property.Status.ACTIVE:
                            row['result'] = 'active'
                        else:
                            row['result'] = 'uncertain'
                    except Exception as exc:
                        row['detail'] = str(exc)
                        if is_listing_gone_error(exc):
                            status = Property.Status.REMOVED
                            row['result'] = 'unavailable'
                        else:
                            row['result'] = 'error'
                    row['applied'] = False
                    if row['result'] == 'unavailable' and options['apply']:
                        retire_listing(pk, status)
                        row['applied'] = True
                    counts[row['result']] += 1
                    output.write(json.dumps(row, ensure_ascii=False) + '\n')
                    output.flush()
                    self.stdout.write(f"{index}/{len(ids)} #{pk}: {row['result']}")
            self.stdout.write(json.dumps(dict(counts), ensure_ascii=False))
        finally:
            lock_file.close()
            lock.unlink(missing_ok=True)
