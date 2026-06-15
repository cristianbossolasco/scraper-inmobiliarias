from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from properties.models import Property
from properties.services.location_intelligence import (
    load_location_zones,
    location_intelligence_values,
    score_property_location_intelligence,
)


UPDATE_FIELDS = [
    "overall_score",
    "level",
    "partido_name",
    "locality_name",
    "zone_name",
    "match_method",
    "confidence",
    "transport_score",
    "education_score",
    "health_score",
    "flood_penalty_score",
    "urban_informality_score",
    "environmental_penalty_score",
    "development_potential_score",
    "in_flood_risk_zone",
    "nearest_renabap_m",
    "nearest_sube_point_m",
    "nearest_school_m",
    "nearest_health_center_m",
    "components",
    "risks",
    "evidence",
    "source_signature",
    "scored_at",
]


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
        queryset = list(queryset)

        total = len(queryset)
        matched = 0
        changed = 0
        reported = 0
        to_create = []
        to_update = []
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
            record = self._record_from_score(property_obj, score, old_record)
            new_values = location_intelligence_values(record)
            if old_values != new_values:
                changed += 1
                if record.pk:
                    to_update.append(record)
                else:
                    to_create.append(record)
                if reported < 40:
                    reported += 1
                    self.stdout.write(
                        self._safe_line(
                            f"  id={property_obj.pk} score={new_values.get('overall_score')} "
                            f"nivel={new_values.get('level')} zona={new_values.get('zone_name')} "
                            f"match={record.match_method}"
                        )
                    )

        if changed > reported:
            self.stdout.write(f"  ... {changed - reported} cambios adicionales omitidos")
        if not options["dry_run"]:
            self._persist_records(to_create, to_update)
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{matched}/{total} propiedades con inteligencia territorial; "
                f"{changed} cambios{suffix}"
            )
        )

    def _safe_line(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")

    def _record_from_score(self, property_obj, score, old_record):
        from django.utils import timezone
        from properties.models import PropertyLocationIntelligence

        record = old_record or PropertyLocationIntelligence(property=property_obj)
        values = {
            "overall_score": score.overall_score,
            "level": score.level or "",
            "partido_name": score.partido_name or property_obj.inferred_partido or "",
            "locality_name": score.locality_name or property_obj.inferred_locality or "",
            "zone_name": score.zone_name or property_obj.inferred_zone or "",
            "match_method": score.match_method or "none",
            "confidence": score.confidence or "",
            "transport_score": score.transport_score,
            "education_score": score.education_score,
            "health_score": score.health_score,
            "flood_penalty_score": score.flood_penalty_score,
            "urban_informality_score": score.urban_informality_score,
            "environmental_penalty_score": score.environmental_penalty_score,
            "development_potential_score": score.development_potential_score,
            "in_flood_risk_zone": score.in_flood_risk_zone,
            "nearest_renabap_m": score.nearest_renabap_m,
            "nearest_sube_point_m": score.nearest_sube_point_m,
            "nearest_school_m": score.nearest_school_m,
            "nearest_health_center_m": score.nearest_health_center_m,
            "components": score.components or {},
            "risks": score.risks or {},
            "evidence": score.evidence or {},
            "source_signature": score.source_signature or "",
            "scored_at": timezone.now(),
        }
        for field, value in values.items():
            setattr(record, field, value)
        return record

    def _persist_records(self, to_create, to_update):
        from properties.models import PropertyLocationIntelligence

        for start in range(0, len(to_update), 250):
            PropertyLocationIntelligence.objects.bulk_update(
                to_update[start : start + 250],
                UPDATE_FIELDS,
                batch_size=250,
            )
        for start in range(0, len(to_create), 250):
            PropertyLocationIntelligence.objects.bulk_create(
                to_create[start : start + 250],
                batch_size=250,
            )
