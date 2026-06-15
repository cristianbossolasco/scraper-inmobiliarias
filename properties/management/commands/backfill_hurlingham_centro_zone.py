from django.core.management.base import BaseCommand

from properties.services.hurlingham_centro_backfill import backfill_hurlingham_centro_zone


class Command(BaseCommand):
    help = "Unifica Hurlingham Centro y Barrio Ingles en propiedades e inteligencia territorial."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--property-id", action="append", type=int)

    def handle(self, *args, **options):
        result = backfill_hurlingham_centro_zone(
            dry_run=options["dry_run"],
            property_ids=options.get("property_id"),
        )
        counts = result["counts"]
        suffix = " (dry-run)" if result["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{counts.get('properties', 0)} propiedades y "
                f"{counts.get('location_intelligence', 0)} registros de inteligencia "
                f"actualizados a {result['canonical_name']}{suffix}"
            )
        )
