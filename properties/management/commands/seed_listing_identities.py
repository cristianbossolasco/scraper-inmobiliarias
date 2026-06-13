from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from properties.models import ListingIdentity, Source
from properties.scrapers.parsing import external_id_from_url
from properties.scrapers.registry import get_adapter


class Command(BaseCommand):
    help = "Siembra memoria de publicaciones vistas sin crear propiedades."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica los cambios. Sin este flag solo informa el dry-run.",
        )
        parser.add_argument("--max-pages", type=int)
        parser.add_argument("--max-listings", type=int)
        parser.add_argument("--timeout", type=int, default=25)

    def handle(self, *args, **options):
        source = Source.objects.filter(slug=options["source"]).first()
        if not source:
            raise CommandError(f"No existe la fuente {options['source']}.")

        adapter = get_adapter(
            source.slug,
            max_pages=options["max_pages"],
            max_listings=options["max_listings"],
            request_timeout=options["timeout"],
        )
        urls = list(dict.fromkeys(adapter.discover()))
        identities = [
            (external_id_from_url(url), url)
            for url in urls
            if external_id_from_url(url)
        ]
        existing = set(
            ListingIdentity.objects.filter(
                source=source,
                external_id__in=[external_id for external_id, _url in identities],
            ).values_list("external_id", flat=True)
        )
        to_create = [
            (external_id, url)
            for external_id, url in identities
            if external_id not in existing
        ]
        to_update = [
            (external_id, url)
            for external_id, url in identities
            if external_id in existing
        ]

        self.stdout.write(
            f"{'APPLY' if options['apply'] else 'DRY-RUN'} seed_listing_identities "
            f"source={source.slug}: descubiertas={len(identities)} "
            f"nuevas={len(to_create)} existentes={len(to_update)}"
        )
        stats = getattr(adapter, "discovery_stats", {}) or {}
        if stats:
            self.stdout.write(
                "Discovery: "
                f"declarado={stats.get('declared_total')} "
                f"urls={stats.get('urls_discovered', len(identities))} "
                f"cobertura={stats.get('coverage_ratio')}"
            )

        if not options["apply"]:
            self.stdout.write(self.style.SUCCESS("Dry-run finalizado sin cambios."))
            return

        now = timezone.now()
        with transaction.atomic():
            ListingIdentity.objects.bulk_create(
                [
                    ListingIdentity(
                        source=source,
                        external_id=external_id,
                        url=url,
                        first_seen_at=now,
                        last_seen_at=now,
                        last_seen_reason="seed_discovery",
                    )
                    for external_id, url in to_create
                ],
                ignore_conflicts=True,
            )
            for external_id, url in to_update:
                ListingIdentity.objects.filter(
                    source=source,
                    external_id=external_id,
                ).update(
                    url=url,
                    last_seen_at=now,
                    last_seen_reason="seed_discovery",
                )
        self.stdout.write(self.style.SUCCESS("Identidades sembradas."))
