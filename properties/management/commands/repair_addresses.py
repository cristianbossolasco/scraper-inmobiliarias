from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.geocoding import Geocoder
from properties.services.location_enrichment import clean_detected_address
from properties.services.normalization import normalize_address


class Command(BaseCommand):
    help = "Limpia direcciones existentes que contienen metadata pegada."

    def add_arguments(self, parser):
        parser.add_argument("--source")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--geocode", action="store_true")

    def handle(self, *args, **options):
        queryset = Property.objects.all()
        if options["source"]:
            queryset = queryset.filter(listings__source__slug=options["source"]).distinct()
        changed = 0
        changed_ids = []
        for property_obj in queryset.order_by("id"):
            cleaned = clean_detected_address(property_obj.address)
            detected_cleaned = clean_detected_address(property_obj.detected_address)
            updates = {}
            if cleaned and cleaned != property_obj.address:
                updates["address"] = cleaned
                updates["normalized_address"] = normalize_address(cleaned)
            elif cleaned and normalize_address(cleaned) != property_obj.normalized_address:
                updates["normalized_address"] = normalize_address(cleaned)
            if detected_cleaned and detected_cleaned != property_obj.detected_address:
                updates["detected_address"] = detected_cleaned
            if not updates:
                continue
            changed += 1
            changed_ids.append(property_obj.pk)
            self.stdout.write(f"id={property_obj.pk} {updates}")
            if not options["dry_run"]:
                for field, value in updates.items():
                    setattr(property_obj, field, value)
                property_obj.save(update_fields=list(updates))
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
