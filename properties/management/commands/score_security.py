from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from properties.models import Property
from properties.services.security_scoring import (
    SECURITY_UPDATE_FIELDS,
    apply_security_score,
    load_security_points,
    load_security_zones,
    score_property_security,
)


class Command(BaseCommand):
    help = "Calcula cobertura/riesgo de seguridad por propiedad usando GeoJSON locales."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--only-missing", action="store_true")
        parser.add_argument("--geojson")
        parser.add_argument("--points-geojson")

    def handle(self, *args, **options):
        zones_dataset = load_security_zones(options.get("geojson"))
        points_dataset = load_security_points(options.get("points_geojson"))
        if not zones_dataset["configured"]:
            raise CommandError("No se encontraron zonas de seguridad GeoJSON.")

        queryset = Property.objects.select_related("location").filter(location__isnull=False)
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        if options["only_missing"]:
            queryset = queryset.filter(security_coverage_score__isnull=True)

        total = queryset.count()
        matched = 0
        changed = 0
        reported = 0
        now = timezone.now()
        self.stdout.write(
            f"Propiedades con ubicacion: {total}; zonas={len(zones_dataset['features'])}; "
            f"puntos={len(points_dataset['features'])}"
        )

        for property_obj in queryset.order_by("pk").iterator():
            score = score_property_security(
                property_obj,
                zones=zones_dataset["features"],
                points=points_dataset["features"],
            )
            if score.matched:
                matched += 1
            old_values = {
                "coverage": property_obj.security_coverage_score,
                "risk": property_obj.security_risk_score,
                "level": property_obj.security_level,
                "zone": property_obj.security_zone_label,
            }
            apply_security_score(property_obj, score, commit=False)
            property_obj.security_scored_at = now
            new_values = {
                "coverage": property_obj.security_coverage_score,
                "risk": property_obj.security_risk_score,
                "level": property_obj.security_level,
                "zone": property_obj.security_zone_label,
            }
            if old_values != new_values:
                changed += 1
                if reported < 40:
                    reported += 1
                    self.stdout.write(
                        self._safe_line(
                            f"  id={property_obj.pk} cobertura={new_values['coverage']} "
                            f"riesgo={new_values['risk']} nivel={new_values['level']} "
                            f"zona={new_values['zone']}"
                        )
                    )
            if not options["dry_run"]:
                property_obj.save(update_fields=SECURITY_UPDATE_FIELDS)

        if changed > reported:
            self.stdout.write(f"  ... {changed - reported} cambios adicionales omitidos")
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{matched}/{total} propiedades con score; {changed} cambios{suffix}"
            )
        )

    def _safe_line(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
