from django.core.management.base import BaseCommand

from properties.models import Agency, Listing
from properties.services.agency_normalization import normalize_agency_name


class Command(BaseCommand):
    help = "Normaliza inmobiliarias mal extraidas y reasigna publicaciones existentes."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        changes = 0
        for agency in list(Agency.objects.order_by("id")):
            normalized = normalize_agency_name(agency.name)
            listings = Listing.objects.filter(agency=agency)
            if not normalized:
                if listings.exists():
                    self.stdout.write(
                        f"{agency.id}: limpiar {listings.count()} publicaciones de {agency.name[:90]!r}"
                    )
                    changes += listings.count()
                    if not options["dry_run"]:
                        listings.update(agency=None)
                if not agency.listings.exists():
                    self.stdout.write(f"{agency.id}: eliminar agencia huerfana {agency.name[:90]!r}")
                    changes += 1
                    if not options["dry_run"]:
                        agency.delete()
                continue
            if normalized == agency.name:
                continue
            target = Agency.objects.filter(name=normalized).first()
            self.stdout.write(f"{agency.id}: {agency.name[:90]!r} -> {normalized!r}")
            changes += listings.count() or 1
            if options["dry_run"]:
                continue
            if target:
                listings.update(agency=target)
                agency.delete()
            else:
                agency.name = normalized
                agency.save(update_fields=["name"])
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{changes} cambios de agencia{suffix}"))
