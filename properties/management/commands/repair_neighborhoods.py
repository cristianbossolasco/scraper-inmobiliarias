from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.normalization import normalize_neighborhood_name


class Command(BaseCommand):
    help = "Normaliza barrios/zonas existentes y elimina textos contaminados."

    def add_arguments(self, parser):
        parser.add_argument("--source")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        queryset = Property.objects.all()
        if options["source"]:
            queryset = queryset.filter(listings__source__slug=options["source"])
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        queryset = queryset.distinct().order_by("id")

        changed = 0
        for property_obj in queryset:
            updates = {}
            for field in ("neighborhood", "detected_neighborhood"):
                current = getattr(property_obj, field) or ""
                normalized = normalize_neighborhood_name(current)
                if current != normalized:
                    updates[field] = normalized
            if not updates:
                continue
            changed += 1
            rendered = "; ".join(
                f"{field}: {getattr(property_obj, field)!r} -> {value!r}"
                for field, value in updates.items()
            )
            self.stdout.write(self._safe(f"id={property_obj.pk} {rendered}"))
            if not options["dry_run"]:
                for field, value in updates.items():
                    setattr(property_obj, field, value)
                property_obj.save(update_fields=list(updates))

        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{changed} propiedades con zona corregida{suffix}"))

    def _safe(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
