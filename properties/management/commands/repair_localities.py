from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.normalization import (
    known_neighborhood_name,
    locality_from_neighborhood,
    normalize_locality,
    normalize_whitespace,
)


class Command(BaseCommand):
    help = "Normaliza localidades sucias y mueve barrios detectados al campo de zona."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Muestra cambios sin guardar.")
        parser.add_argument("--property-id", type=int, help="Procesa una propiedad puntual.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        queryset = Property.objects.all().order_by("pk")
        if options.get("property_id"):
            queryset = queryset.filter(pk=options["property_id"])

        changed = 0
        reported = 0
        for property_obj in queryset:
            updates, notes = self._updates_for(property_obj)
            if not updates and not notes:
                continue
            changed += 1
            if reported < 80:
                rendered = ", ".join(f"{field}={value!r}" for field, value in updates.items())
                if notes:
                    rendered = f"{rendered}, nota={notes[-1]!r}" if rendered else f"nota={notes[-1]!r}"
                self.stdout.write(f"id={property_obj.pk} {rendered}")
                reported += 1
            if dry_run:
                continue
            update_fields = []
            for field, value in updates.items():
                setattr(property_obj, field, value)
                update_fields.append(field)
            if notes:
                property_obj.location_notes = self._append_notes(property_obj.location_notes, notes)
                update_fields.append("location_notes")
            property_obj.save(update_fields=sorted(set(update_fields)))

        suffix = " (dry-run)" if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{changed} localidades corregidas{suffix}"))

    def _updates_for(self, property_obj):
        updates = {}
        notes = []
        manual = property_obj.manual_overrides if isinstance(property_obj.manual_overrides, dict) else {}

        if "locality" not in manual:
            locality_updates, locality_notes = self._repair_locality_field(
                property_obj,
                "locality",
                property_obj.locality,
                manual,
            )
            updates.update(locality_updates)
            notes.extend(locality_notes)

        detected_updates, detected_notes = self._repair_locality_field(
            property_obj,
            "detected_locality",
            property_obj.detected_locality,
            manual,
        )
        updates.update({field: value for field, value in detected_updates.items() if field not in updates})
        notes.extend(detected_notes)
        return updates, notes

    def _repair_locality_field(self, property_obj, field, raw_value, manual):
        raw = normalize_whitespace(raw_value)
        if not raw:
            return {}, []

        canonical = normalize_locality(raw)
        if canonical:
            if raw == canonical:
                return {}, []
            return {field: canonical}, [f"{field}: {raw} -> {canonical}"]

        neighborhood = known_neighborhood_name(raw)
        updates = {}
        notes = []
        if neighborhood:
            target_locality = (
                normalize_locality(property_obj.detected_locality)
                or normalize_locality(property_obj.locality)
                or locality_from_neighborhood(neighborhood)
                or "Hurlingham"
            )
            if field == "locality":
                updates["locality"] = target_locality
            else:
                updates["detected_locality"] = ""
            if "neighborhood" not in manual and not property_obj.neighborhood:
                updates["neighborhood"] = neighborhood
            elif not property_obj.detected_neighborhood:
                updates["detected_neighborhood"] = neighborhood
            notes.append(f"{field}: se movio localidad invalida a zona {neighborhood}: {raw}")
            return updates, notes

        if field == "locality":
            updates["locality"] = normalize_locality(property_obj.detected_locality) or "Hurlingham"
        else:
            updates["detected_locality"] = ""
        notes.append(f"{field}: se descarto valor no-localidad: {raw}")
        return updates, notes

    def _append_notes(self, current, notes):
        existing = normalize_whitespace(current)
        lines = [existing] if existing else []
        for note in notes:
            rendered = f"[repair_localities] {note}"
            if rendered not in lines:
                lines.append(rendered)
        return "\n".join(lines)
