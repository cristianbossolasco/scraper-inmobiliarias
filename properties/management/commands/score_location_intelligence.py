from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from properties.models import Property
from properties.services.location_intelligence import (
    apply_location_intelligence_score,
    load_location_zones,
    location_intelligence_values,
    score_property_location_intelligence,
)


class Command(BaseCommand):
    help = "Calcula inteligencia territorial por propiedad usando GeoJSON locales."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--only-missing", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--geojson")

    def handle(self, *args, **options):
        dataset = load_location_zones(options.get("geojson"))
        if not dataset["configured"]:
            raise CommandError("No se encontro GeoJSON integrado de inteligencia territorial.")

        queryset = Property.objects.select_related("location", "location_intelligence")
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        if options["only_missing"]:
            queryset = queryset.filter(location_intelligence__isnull=True)
        if not options["force"]:
            queryset = queryset.filter(
                Q(location__isnull=False)
                | Q(inferred_neighborhood__gt="")
                | Q(detected_neighborhood__gt="")
                | Q(neighborhood__gt="")
            )
        queryset = queryset.distinct().order_by("pk")
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        total = queryset.count() if not options["limit"] else len(queryset)
        matched = 0
        changed = 0
        reported = 0
        self.stdout.write(
            f"Propiedades evaluadas: {total}; zonas={len(dataset['features'])}; "
            f"fuente={dataset['path']}"
        )

        for property_obj in queryset:
            score = score_property_location_intelligence(
                property_obj,
                zones=dataset["features"],
                source_signature=dataset["signature"],
            )
            if score.matched:
                matched += 1
            old_record = getattr(property_obj, "location_intelligence", None)
            old_values = location_intelligence_values(old_record)
            record = apply_location_intelligence_score(property_obj, score, commit=False)
            new_values = location_intelligence_values(record)
            if old_values != new_values:
                changed += 1
                if reported < 40:
                    reported += 1
                    self.stdout.write(
                        self._safe_line(
                            f"  id={property_obj.pk} score={new_values.get('overall_score')} "
                            f"nivel={new_values.get('level')} zona={new_values.get('zone_name')} "
                            f"match={record.match_method}"
                        )
                    )
            if not options["dry_run"]:
                apply_location_intelligence_score(property_obj, score, commit=True)

        if changed > reported:
            self.stdout.write(f"  ... {changed - reported} cambios adicionales omitidos")
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{matched}/{total} propiedades con inteligencia territorial; "
                f"{changed} cambios{suffix}"
            )
        )

    def _safe_line(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
