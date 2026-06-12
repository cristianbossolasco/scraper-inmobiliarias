from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.geocoding import Geocoder, address_number, street_key
from properties.services.location_enrichment import clean_detected_address
from properties.services.normalization import (
    extract_embedded_neighborhood,
    normalize_address,
)


def same_geocoding_target(before, after):
    return (
        bool(before and after)
        and address_number(before) == address_number(after)
        and street_key(before) == street_key(after)
    )


KNOWN_ADDRESS_CORRECTIONS = {
    4510: {"address": "Tambo Nuevo 800", "locality": "Hurlingham", "neighborhood": "Villa Alemania"},
    2987: {"address": "Rossini 2000", "locality": "Hurlingham"},
    1068: {"address": "GRANADA 500"},
    845: {"address": "Bonorino 634", "locality": "Villa Tesei", "neighborhood": "Santos Tesei"},
    2792: {"address": "NECOCHEA 1300", "locality": "Hurlingham", "neighborhood": "Barrio Inglés"},
    698: {"address": "José de Andonaegui 2600", "locality": "William C. Morris"},
    4409: {"address": "Las Araucarias 1900", "locality": "Hurlingham", "neighborhood": "Los Troncos"},
    4548: {"address": "Juan Díaz de Solís 1686", "locality": "William C. Morris"},
    4571: {"address": "Eduardo Acevedo 329", "locality": "William C. Morris", "neighborhood": "Villa Club"},
}


class Command(BaseCommand):
    help = "Limpia direcciones existentes que contienen metadata pegada."

    def add_arguments(self, parser):
        parser.add_argument("--source")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--geocode", action="store_true")

    def handle(self, *args, **options):
        queryset = Property.objects.all()
        if options["source"]:
            queryset = queryset.filter(listings__source__slug=options["source"]).distinct()
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        changed = 0
        changed_ids = []
        for property_obj in queryset.order_by("id"):
            cleaned = clean_detected_address(property_obj.address)
            detected_cleaned = clean_detected_address(property_obj.detected_address)
            embedded_neighborhood = extract_embedded_neighborhood(
                property_obj.address or property_obj.detected_address
            )
            updates = {}
            known = KNOWN_ADDRESS_CORRECTIONS.get(property_obj.pk, {})
            if cleaned and cleaned != property_obj.address:
                updates["address"] = cleaned
                updates["normalized_address"] = normalize_address(cleaned)
            elif cleaned and normalize_address(cleaned) != property_obj.normalized_address:
                updates["normalized_address"] = normalize_address(cleaned)
            if detected_cleaned and detected_cleaned != property_obj.detected_address:
                updates["detected_address"] = detected_cleaned
            if known.get("address"):
                cleaned_known = clean_detected_address(known["address"])
                updates["address"] = cleaned_known
                updates["detected_address"] = cleaned_known
                updates["normalized_address"] = normalize_address(cleaned_known)
            if known.get("locality"):
                updates["locality"] = known["locality"]
                if property_obj.detected_locality:
                    updates["detected_locality"] = known["locality"]
            if known.get("neighborhood"):
                updates["neighborhood"] = known["neighborhood"]
                if property_obj.detected_neighborhood:
                    updates["detected_neighborhood"] = known["neighborhood"]
            elif embedded_neighborhood and property_obj.neighborhood in {"", "Hurlingham"}:
                updates["neighborhood"] = embedded_neighborhood
                if property_obj.detected_neighborhood in {"", "Hurlingham"}:
                    updates["detected_neighborhood"] = embedded_neighborhood
            if not updates:
                continue
            changed += 1
            changed_ids.append(property_obj.pk)
            self.stdout.write(f"id={property_obj.pk} {updates}")
            if not options["dry_run"]:
                old_address = property_obj.address or property_obj.detected_address or ""
                address_changed = "address" in updates or "detected_address" in updates
                for field, value in updates.items():
                    setattr(property_obj, field, value)
                property_obj.save(update_fields=list(updates))
                location = getattr(property_obj, "location", None)
                new_address = property_obj.address or property_obj.detected_address or ""
                if (
                    address_changed
                    and location
                    and not location.manually_corrected
                    and not same_geocoding_target(old_address, new_address)
                ):
                    location.delete()
        if options["geocode"] and changed_ids and not options["dry_run"]:
            geocoder = Geocoder()
            located = 0
            no_result = 0
            errors = 0
            for property_obj in Property.objects.filter(pk__in=changed_ids).order_by("id"):
                try:
                    if geocoder.geocode_property(property_obj, force=True):
                        located += 1
                    else:
                        no_result += 1
                except Exception as exc:
                    errors += 1
                    self.stderr.write(f"{property_obj.pk}: {exc}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"{located} propiedades geolocalizadas; {no_result} sin resultado; {errors} errores."
                )
            )
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{changed} direcciones corregidas{suffix}"))
