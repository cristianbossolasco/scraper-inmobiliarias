from django.core.management.base import BaseCommand, CommandError

from properties.models import Listing
from properties.scrapers.registry import get_adapter
from properties.services.data_quality import is_listing_url, is_rental_url
from properties.services.location_enrichment import enrich_location_data
from properties.services.normalization import normalize_address, normalize_locality


REPAIR_FIELDS = (
    "property_type",
    "operation",
    "title",
    "address",
    "locality",
    "neighborhood",
    "currency",
    "price",
    "rooms",
    "bedrooms",
    "bathrooms",
    "garages",
    "toilets",
    "covered_area",
    "total_area",
    "land_area",
    "uncovered_area",
    "semicovered_area",
    "front_width",
    "lot_depth",
    "building_floors",
    "detected_locality",
    "detected_neighborhood",
    "detected_address",
    "location_source",
    "location_confidence",
    "location_notes",
    "location_evidence",
)

CLEARABLE_EMPTY_FIELDS = {"currency"}


class Command(BaseCommand):
    help = "Reparsea publicaciones existentes y corrige campos canonicos sin tocar snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--max-listings", type=int)
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--timeout", type=int, default=20)
        parser.add_argument("--crawl-delay", type=float)
        parser.add_argument("--mark-non-sale", action="store_true")
        parser.add_argument("--mark-listing-pages", action="store_true")
        parser.add_argument("--classify-only", action="store_true")

    def handle(self, *args, **options):
        if options["max_listings"] is not None and options["max_listings"] < 1:
            raise CommandError("--max-listings debe ser positivo.")
        total_changes = 0
        touched_properties = set()
        for slug in options["source"]:
            adapter = get_adapter(slug, request_timeout=options["timeout"])
            if options["crawl_delay"] is not None:
                adapter.definition.crawl_delay = options["crawl_delay"]
            listings = (
                Listing.objects.filter(source__slug=slug, active=True)
                .select_related("property", "source")
                .order_by("id")
            )
            if options["property_id"]:
                listings = listings.filter(property_id__in=options["property_id"])
            if options["max_listings"]:
                listings = listings[: options["max_listings"]]
            self.stdout.write(f"Fuente {slug}: {len(listings)} publicaciones")
            for listing in listings:
                classification_changes = self._classification_changes(listing, None, options)
                if listing.property_id in touched_properties:
                    continue
                if classification_changes:
                    total_changes += len(classification_changes)
                    rendered = "; ".join(
                        f"{field}: {old!r} -> {new!r}"
                        for field, old, new in classification_changes
                    )
                    self.stdout.write(self._safe_line(f"  id={listing.property_id} {rendered}"))
                    touched_properties.add(listing.property_id)
                    if not options["dry_run"]:
                        self._apply_changes(listing.property, classification_changes)
                    continue
                if options["classify_only"]:
                    continue
                try:
                    data = adapter.parse(listing.url)
                except Exception as exc:
                    self.stdout.write(self._safe_line(f"  ERROR id={listing.property_id} {listing.url}: {exc}"))
                    continue
                if not data:
                    self.stdout.write(self._safe_line(f"  OMITIDA id={listing.property_id}: parser sin datos"))
                    continue
                data = enrich_location_data(data)
                changes = self._changes(listing.property, data)
                changes.extend(self._classification_changes(listing, data, options))
                changes = self._dedupe_changes(changes)
                if not changes:
                    continue
                if listing.property_id in touched_properties:
                    continue
                total_changes += len(changes)
                rendered = "; ".join(
                    f"{field}: {old!r} -> {new!r}" for field, old, new in changes
                )
                self.stdout.write(self._safe_line(f"  id={listing.property_id} {rendered}"))
                touched_properties.add(listing.property_id)
                if not options["dry_run"]:
                    self._apply_changes(listing.property, changes)
                    self._apply_listing_data(listing, data)
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{total_changes} cambios detectados{suffix}"))

    def _safe_line(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")

    def _changes(self, property_obj, data):
        changes = []
        for field in REPAIR_FIELDS:
            if field not in data:
                continue
            new = data.get(field)
            if new == "" and field not in CLEARABLE_EMPTY_FIELDS:
                continue
            old = getattr(property_obj, field)
            if str(old) != str(new):
                changes.append((field, old, new))
        return changes

    def _dedupe_changes(self, changes):
        deduped = {}
        for field, old, new in changes:
            deduped[field] = (field, old, new)
        return list(deduped.values())

    def _classification_changes(self, listing, data, options):
        changes = []
        property_obj = listing.property
        if options["mark_listing_pages"] and is_listing_url(listing.url):
            if property_obj.operation != "listing":
                changes.append(("operation", property_obj.operation, "listing"))
            if not property_obj.is_hidden:
                changes.append(("is_hidden", property_obj.is_hidden, True))
            return changes
        if options["mark_non_sale"] and is_rental_url(listing.url):
            if property_obj.operation != "rent":
                changes.append(("operation", property_obj.operation, "rent"))
            if not property_obj.is_hidden:
                changes.append(("is_hidden", property_obj.is_hidden, True))
            return changes
        operation = (data or {}).get("operation")
        if options["mark_non_sale"] and operation and operation != "sale":
            if property_obj.operation != operation:
                changes.append(("operation", property_obj.operation, operation))
            if not property_obj.is_hidden:
                changes.append(("is_hidden", property_obj.is_hidden, True))
        return changes

    def _apply_changes(self, property_obj, changes):
        for field, _old, new in changes:
            setattr(property_obj, field, new)
        property_obj.normalized_address = normalize_address(property_obj.address)
        property_obj.locality = normalize_locality(property_obj.locality)
        property_obj.save(
            update_fields=[
                *{field for field, _old, _new in changes},
                "normalized_address",
                "locality",
            ]
        )

    def _apply_listing_data(self, listing, data):
        update_fields = []
        if "source_status" in data and listing.source_status != (data.get("source_status") or ""):
            listing.source_status = data.get("source_status") or ""
            update_fields.append("source_status")
        if data.get("raw_data") and listing.raw_data != data["raw_data"]:
            listing.raw_data = data["raw_data"]
            update_fields.append("raw_data")
        if update_fields:
            listing.save(update_fields=update_fields)
