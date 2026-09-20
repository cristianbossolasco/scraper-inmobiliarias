import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from properties.models import Listing, Source
from properties.services.argenprop_browser import ArgenpropBrowser, BrowserArgenpropScraper, BrowserBlocked, ListingGone
from properties.services.ingestion import ingest_listing
from properties.management.commands.audit_listing_links import retire_listing


def save_state(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class Command(BaseCommand):
    help = 'Actualiza Argenprop con navegador aislado y checkpoint por URL. Dry-run por defecto.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--state', required=True)
        parser.add_argument('--max-pages', type=int)
        parser.add_argument('--max-listings', type=int)

    def handle(self, *args, **options):
        path = Path(options['state'])
        path.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'apply': options['apply'], 'max_pages': options['max_pages'],
            'max_listings': options['max_listings'], 'started_at': timezone.now().isoformat(),
            'urls': [], 'results': {}, 'discovery_complete': False,
        }
        for key in ('apply', 'max_pages', 'max_listings'):
            if state[key] != options[key]:
                raise CommandError(f'El checkpoint usa otro valor de {key}. Use otro --state.')
        lock = path.with_suffix('.lock')
        try:
            handle = lock.open('x')
        except FileExistsError as exc:
            raise CommandError(f'Checkpoint en uso: {lock}') from exc
        try:
            source = Source.objects.get(slug='argenprop')
            with ArgenpropBrowser() as transport:
                scraper = BrowserArgenpropScraper(transport, max_pages=options['max_pages'], max_listings=options['max_listings'])
                if not state['discovery_complete']:
                    for url in scraper.discover():
                        if url not in state['urls']:
                            state['urls'].append(url)
                            save_state(path, state)
                        self.stdout.write(f"Descubiertas: {len(state['urls'])}")
                    state['discovery_complete'] = True
                    # Full runs also verify historical links absent from discovery.
                    if not options['max_pages'] and not options['max_listings']:
                        for url in Listing.objects.filter(source=source).values_list('url', flat=True):
                            if url not in state['urls']:
                                state['urls'].append(url)
                    save_state(path, state)
                for index, url in enumerate(state['urls'], 1):
                    if url in state['results']:
                        continue
                    self.stdout.write(f"Verificando {index}/{len(state['urls'])}: {url}")
                    try:
                        soup = scraper.soup(url)
                        # Refuse search redirects, empty pages, challenges and unrelated content.
                        if not soup.select_one('h1') or 'Código de aviso' not in soup.get_text(' ', strip=True):
                            result = {'status': 'uncertain'}
                        else:
                            data = scraper.parse_soup(soup, url)
                            result = {'status': 'active', 'title': data['title'], 'price': str(data.get('price'))}
                            if options['apply']:
                                listing, created = ingest_listing(source, data)
                                result.update(listing_id=listing.pk, created=created)
                    except ListingGone:
                        result = {'status': 'removed'}
                        if options['apply']:
                            for pk in Listing.objects.filter(source=source, url=url).values_list('pk', flat=True):
                                retire_listing(pk, 'removed')
                    except BrowserBlocked:
                        raise
                    except Exception as exc:
                        result = {'status': 'error', 'detail': str(exc)}
                    result['checked_at'] = timezone.now().isoformat()
                    state['results'][url] = result
                    save_state(path, state)
                    self.stdout.write(f"{index}/{len(state['urls'])}: {result['status']}")
                state['finished_at'] = timezone.now().isoformat()
                save_state(path, state)
        except BrowserBlocked as exc:
            raise CommandError(str(exc)) from exc
        finally:
            handle.close()
            lock.unlink(missing_ok=True)
