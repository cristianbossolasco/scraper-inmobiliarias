from decimal import Decimal

from django.core.cache import cache
from django.core.management.base import BaseCommand

from properties.models import Listing, Property
from properties.scrapers.registry import get_adapter
from properties.services.geocoding import Geocoder
from properties.services.ingestion import manual_override_fields
from properties.services.location_intelligence import (
    apply_location_intelligence_score,
    load_location_zones,
    score_property_location_intelligence,
)
from properties.services.normalization import normalize_address
from properties.services.zone_inference import apply_zone_inference, infer_property_zone


KNOWN_DETAIL_FIXES = {
    5371: {
        "address": "Francisco de Gurruchaga 2800",
        "locality": "Hurlingham",
        "total_area": Decimal("186"),
        "covered_area": Decimal("112"),
        "rooms": 5,
        "bathrooms": Decimal("1"),
        "garages": 1,
        "bedrooms": 4,
        "age_years": 10,
    },
    5368: {
        "address": "Williams 2328",
        "locality": "Hurlingham",
        "total_area": Decimal("217"),
        "covered_area": Decimal("100"),
        "rooms": 3,
        "bathrooms": Decimal("1"),
        "bedrooms": 2,
        "age_years": 30,
    },
    5350: {
        "total_area": Decimal("260"),
        "covered_area": Decimal("50"),
        "rooms": 2,
        "bathrooms": Decimal("1"),
        "bedrooms": 1,
    },
    5304: {
        "address": "Combate de Pavón 2330",
        "locality": "Hurlingham",
        "total_area": Decimal("75"),
        "covered_area": Decimal("70"),
        "rooms": 3,
        "bathrooms": Decimal("1"),
        "bedrooms": 2,
        "age_years": 0,
        "condition_category": Property.ConditionCategory.NEW,
    },
    5275: {
        "address": "Atuel",
        "locality": "Hurlingham",
        "total_area": Decimal("192"),
        "covered_area": Decimal("80"),
        "rooms": 3,
        "bathrooms": Decimal("1"),
        "garages": 1,
        "bedrooms": 2,
        "age_years": 55,
    },
}

REPAIR_FIELDS = (
    "address",
    "locality",
    "total_area",
    "covered_area",
    "rooms",
    "bathrooms",
    "garages",
    "bedrooms",
    "age_years",
    "condition_category",
)


class Command(BaseCommand):
    help = "Reparsea y repara fichas Zonaprop sin direccion/metricas, con dry-run por defecto."

    def add_arguments(self, parser):
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--missing-zone", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--geocode", action="store_true")
        parser.add_argument("--infer-zones", action="store_true")
        parser.add_argument("--score-location", action="store_true")
        parser.add_argument("--skip-live", action="store_true")

    def handle(self, *args, **options):
        queryset = (
            Listing.objects.select_related("property", "source")
            .filter(source__slug="zonaprop")
            .order_by("property_id", "id")
        )
        if options["property_id"]:
            queryset = queryset.filter(property_id__in=options["property_id"])
        if options["missing_zone"]:
            queryset = queryset.filter(property__inferred_neighborhood="")
        queryset = queryset.distinct()
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        scraper = None if options["skip_live"] else get_adapter("zonaprop")()
        location_zones = load_location_zones() if options["score_location"] else None
        dry_run = not options["apply"]
        changed = geocoded = inferred = scored = live_errors = 0

        for listing in queryset:
            property_obj = listing.property
            parsed, error = self._parse_listing(scraper, listing)
            if error:
                live_errors += 1
            known = KNOWN_DETAIL_FIXES.get(property_obj.pk, {})
            data = {**parsed, **known}
            updates = self._property_updates(property_obj, data)
            if not updates:
                continue

            changed += 1
            self.stdout.write(self._safe(f"id={property_obj.pk} updates={updates}"))
            if dry_run:
                continue

            for field, value in updates.items():
                setattr(property_obj, field, value)
            save_fields = set(updates)
            if "address" in updates:
                property_obj.detected_address = updates["address"]
                property_obj.normalized_address = normalize_address(updates["address"])
                save_fields.update({"detected_address", "normalized_address"})
            if "locality" in updates:
                property_obj.detected_locality = updates["locality"]
                save_fields.add("detected_locality")
            property_obj.save(update_fields=sorted(save_fields))

            raw_data = dict(listing.raw_data or {})
            raw_data["zonaprop_repair"] = {
                "fields": {field: str(value) for field, value in data.items() if field in REPAIR_FIELDS},
                "live_parse_error": error or "",
            }
            if parsed.get("raw_data"):
                raw_data["zonaprop_reparse_raw"] = parsed["raw_data"]
            listing.raw_data = raw_data
            listing.save(update_fields=["raw_data"])

            if options["geocode"]:
                if Geocoder().geocode_property(property_obj, force=True):
                    geocoded += 1
            if options["infer_zones"]:
                result = infer_property_zone(property_obj, geocode_missing=False)
                apply_zone_inference(property_obj, result)
                if result.inferred_neighborhood:
                    inferred += 1
            if options["score_location"] and location_zones:
                score = score_property_location_intelligence(
                    property_obj,
                    zones=location_zones["features"],
                    source_signature=location_zones["signature"],
                )
                apply_location_intelligence_score(property_obj, score)
                if score.matched:
                    scored += 1

        if not dry_run and changed:
            cache.clear()
        suffix = " (dry-run)" if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                self._safe(
                    f"{changed} propiedades Zonaprop reparadas{suffix}; "
                    f"geocode={geocoded}; zonas={inferred}; territorial={scored}; "
                    f"errores_live={live_errors}"
                )
            )
        )

    def _parse_listing(self, scraper, listing):
        if scraper is None:
            return {}, ""
        try:
            return scraper.parse(listing.url) or {}, ""
        except Exception as exc:  # noqa: BLE001
            return {}, str(exc)

    def _property_updates(self, property_obj, data):
        manual_fields = manual_override_fields(property_obj)
        updates = {}
        for field in REPAIR_FIELDS:
            if field in manual_fields or field not in data:
                continue
            value = data.get(field)
            if value in (None, ""):
                continue
            if getattr(property_obj, field) != value:
                updates[field] = value
        return updates

    def _safe(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
