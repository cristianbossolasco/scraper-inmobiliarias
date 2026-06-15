from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from properties.models import Property, PropertyLocationIntelligence
from properties.services.territory_hierarchy import (
    infer_property_territory,
    property_source_zone,
    territory_values_from_result,
)


PROPERTY_UPDATE_FIELDS = [
    "inferred_partido",
    "inferred_locality",
    "inferred_zone",
    "territory_confidence",
    "territory_source_method",
    "territory_needs_review",
    "territory_evidence",
    "territory_inferred_at",
    "inferred_neighborhood",
    "zone_needs_review",
    "zone_conflict",
]
INTELLIGENCE_UPDATE_FIELDS = ["partido_name", "locality_name", "zone_name", "evidence"]


class Command(BaseCommand):
    help = "Backfillea la jerarquia territorial Partido > Localidad > Zona usando GeoJSON locales."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--only-missing", action="store_true")
        parser.add_argument("--geo-dir")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("Usa --apply o --dry-run, no ambos.")
        dry_run = not options["apply"]
        queryset = Property.objects.select_related("location", "location_intelligence").order_by("pk")
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        if options["only_missing"]:
            queryset = queryset.filter(
                Q(inferred_partido="")
                | Q(inferred_locality="")
                | Q(territory_inferred_at__isnull=True)
            )
        if options["limit"]:
            queryset = queryset[: options["limit"]]
        queryset = list(queryset)

        counters = Counter()
        changed_properties = []
        changed_intelligence = []
        now = timezone.now()
        property_updates = []
        intelligence_updates = []
        intelligence_creates = []
        if not options["quiet"]:
            self.stdout.write(f"Modo: {'dry-run' if dry_run else 'apply'}")
            self.stdout.write(f"Propiedades a procesar: {len(queryset)}")

        for property_obj in queryset:
            result = infer_property_territory(property_obj, geo_dir=options.get("geo_dir"))
            values = territory_values_from_result(result)
            counters["processed"] += 1
            if result.partido:
                counters["partido"] += 1
            if result.locality:
                counters["locality"] += 1
            if result.zone:
                counters["zone"] += 1
            if result.needs_review:
                counters["needs_review"] += 1

            property_changes = self._property_changes(property_obj, values, now)
            if property_changes:
                counters["properties_changed"] += 1
                changed_properties.append(property_obj.pk)
                if not dry_run:
                    self._apply_property_values(property_obj, values, now)
                    property_updates.append(property_obj)

            intelligence_changes = self._intelligence_changes(property_obj, values)
            if intelligence_changes:
                counters["intelligence_changed"] += 1
                changed_intelligence.append(property_obj.pk)
                if not dry_run:
                    record = self._intelligence_record(property_obj, values)
                    if record.pk:
                        intelligence_updates.append(record)
                    else:
                        intelligence_creates.append(record)

            if not options["quiet"] and (property_changes or intelligence_changes):
                self.stdout.write(
                    self._safe(
                        f"id={property_obj.pk} partido={result.partido or '-'} "
                        f"localidad={result.locality or '-'} zona={result.zone or '-'} "
                        f"revision={'si' if result.needs_review else 'no'}"
                    )
                )

        if not dry_run:
            self._bulk_persist(property_updates, intelligence_updates, intelligence_creates)

        self.stdout.write(
            self.style.SUCCESS(
                self._safe(
                    f"Backfill territorial terminado{' (dry-run)' if dry_run else ''}: "
                    f"{counters['zone']}/{counters['processed']} con zona; "
                    f"{counters['locality']} con localidad; "
                    f"{counters['needs_review']} para revisar; "
                    f"{counters['properties_changed']} propiedades cambiadas; "
                    f"{counters['intelligence_changed']} intelligence cambiadas."
                )
            )
        )
        if not options["quiet"]:
            self.stdout.write(f"Propiedades cambiadas sample: {changed_properties[:20]}")
            self.stdout.write(f"Intelligence cambiadas sample: {changed_intelligence[:20]}")

    def _property_changes(self, property_obj, values, inferred_at):
        expected = {
            "inferred_partido": values["partido"],
            "inferred_locality": values["locality"],
            "inferred_zone": values["zone"],
            "territory_confidence": values["confidence"],
            "territory_source_method": values["source_method"],
            "territory_needs_review": values["needs_review"],
            "territory_evidence": values["evidence"],
            "inferred_neighborhood": values["zone"] or property_obj.inferred_neighborhood,
            "zone_needs_review": values["needs_review"],
        }
        return [
            field
            for field, expected_value in expected.items()
            if getattr(property_obj, field) != expected_value
        ] + ([] if property_obj.territory_inferred_at else ["territory_inferred_at"])

    def _intelligence_changes(self, property_obj, values):
        record = getattr(property_obj, "location_intelligence", None)
        if not record:
            return INTELLIGENCE_UPDATE_FIELDS
        expected_evidence = {**(record.evidence or {}), "territory_hierarchy": values["evidence"]}
        expected = {
            "partido_name": values["partido"],
            "locality_name": values["locality"],
            "zone_name": values["zone"] or record.zone_name,
            "evidence": expected_evidence,
        }
        return [
            field
            for field, expected_value in expected.items()
            if getattr(record, field) != expected_value
        ]

    def _apply_property_values(self, property_obj, values, inferred_at):
        property_obj.inferred_partido = values["partido"]
        property_obj.inferred_locality = values["locality"]
        property_obj.inferred_zone = values["zone"]
        property_obj.territory_confidence = values["confidence"]
        property_obj.territory_source_method = values["source_method"]
        property_obj.territory_needs_review = values["needs_review"]
        property_obj.territory_evidence = values["evidence"]
        property_obj.territory_inferred_at = inferred_at
        if values["zone"]:
            property_obj.inferred_neighborhood = values["zone"]
        property_obj.zone_needs_review = values["needs_review"]
        source_zone = property_source_zone(property_obj)
        property_obj.zone_conflict = bool(source_zone and values["zone"] and source_zone != values["zone"])

    def _intelligence_record(self, property_obj, values):
        record = getattr(property_obj, "location_intelligence", None)
        if record is None:
            record = PropertyLocationIntelligence(property=property_obj)
        record.partido_name = values["partido"]
        record.locality_name = values["locality"]
        if values["zone"]:
            record.zone_name = values["zone"]
        record.evidence = {**(record.evidence or {}), "territory_hierarchy": values["evidence"]}
        return record

    def _bulk_persist(self, property_updates, intelligence_updates, intelligence_creates):
        for start in range(0, len(property_updates), 250):
            Property.objects.bulk_update(
                property_updates[start : start + 250],
                PROPERTY_UPDATE_FIELDS,
                batch_size=250,
            )
        for start in range(0, len(intelligence_updates), 250):
            PropertyLocationIntelligence.objects.bulk_update(
                intelligence_updates[start : start + 250],
                INTELLIGENCE_UPDATE_FIELDS,
                batch_size=250,
            )
        for start in range(0, len(intelligence_creates), 250):
            PropertyLocationIntelligence.objects.bulk_create(
                intelligence_creates[start : start + 250],
                batch_size=250,
            )

    def _safe(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
