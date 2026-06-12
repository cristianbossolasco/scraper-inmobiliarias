import hashlib

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from properties.models import Listing, Property
from properties.scrapers.registry import get_adapter
from properties.services.ingestion import PROPERTY_FIELDS, canonicalize_listing_data
from properties.services.normalization import is_plausible_property_address, normalize_address


CLEARABLE_FIELDS = {
    "address",
    "normalized_address",
    "neighborhood",
    "currency",
    "detected_neighborhood",
    "detected_address",
    "location_notes",
}


class Command(BaseCommand):
    help = "Separa publicaciones fusionadas por direcciones genericas o contaminadas."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", default=[])
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--audit-only", action="store_true")
        parser.add_argument("--max-properties", type=int)
        parser.add_argument("--max-listings-per-property", type=int)
        parser.add_argument("--timeout", type=int, default=20)

    def handle(self, *args, **options):
        if not options["source"] and not options["property_id"]:
            raise CommandError("Indica --source o --property-id para acotar la reparacion.")
        sources = options["source"] or None
        candidates = self._candidates(sources, options["property_id"])
        if options["max_properties"]:
            candidates = candidates[: options["max_properties"]]

        total_moved = 0
        for property_obj in candidates:
            listings = self._candidate_listings(property_obj, sources)
            if options["max_listings_per_property"]:
                listings = listings[: options["max_listings_per_property"]]
            self.stdout.write(
                self._safe(
                    f"Propiedad {property_obj.pk}: {len(listings)} publicaciones; "
                    f"direccion={property_obj.address!r}; titulo={property_obj.title!r}"
                )
            )
            if options["audit_only"]:
                for listing in listings:
                    self.stdout.write(
                        self._safe(f"  {listing.source.slug} {listing.external_id} {listing.url}")
                    )
                continue
            moved = self._repair_property(property_obj, listings, options)
            total_moved += moved

        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{total_moved} publicaciones separadas{suffix}"))

    def _candidates(self, sources, property_ids):
        queryset = Property.objects.all()
        if property_ids:
            queryset = queryset.filter(pk__in=property_ids)
        if sources:
            queryset = queryset.filter(listings__source__slug__in=sources)
        queryset = queryset.annotate(
            merged_count=Count(
                "listings",
                filter=Q(listings__source__slug__in=sources) if sources else Q(listings__isnull=False),
                distinct=True,
            )
        ).filter(merged_count__gt=1)
        if not property_ids:
            queryset = [
                property_obj
                for property_obj in queryset.order_by("id")
                if not is_plausible_property_address(property_obj.address)
            ]
        else:
            queryset = list(queryset.order_by("id"))
        return queryset

    def _candidate_listings(self, property_obj, sources):
        queryset = (
            property_obj.listings.select_related("source", "agency", "property")
            .prefetch_related("images")
            .filter(active=True)
            .order_by("id")
        )
        if sources:
            queryset = queryset.filter(source__slug__in=sources)
        return list(queryset)

    def _repair_property(self, property_obj, listings, options):
        parsed_items = []
        adapters = {}
        for listing in listings:
            adapter = adapters.get(listing.source.slug)
            if adapter is None:
                adapter = get_adapter(listing.source.slug, request_timeout=options["timeout"])
                adapters[listing.source.slug] = adapter
            try:
                data = adapter.parse(listing.url)
            except Exception as exc:
                self.stdout.write(self._safe(f"  ERROR {listing.external_id}: {exc}"))
                continue
            if not data:
                self.stdout.write(self._safe(f"  OMITIDA {listing.external_id}: parser sin datos"))
                continue
            data = canonicalize_listing_data(data, source=listing.source)
            parsed_items.append((listing, data))

        moved = 0
        with transaction.atomic():
            keep_current = True
            for listing, data in parsed_items:
                if not keep_current and data["fingerprint"] == property_obj.fingerprint:
                    data = dict(data)
                    identity = f"split|{listing.source.slug}|{listing.external_id}|{listing.url}"
                    data["fingerprint"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                target, would_create = self._target_property(
                    property_obj,
                    data,
                    keep_current,
                    dry_run=options["dry_run"],
                )
                keep_current = False
                action = "crearia" if would_create else ("mantiene" if target.pk == property_obj.pk else "mueve")
                self.stdout.write(
                    self._safe(
                        f"  {action} listing={listing.external_id} -> propiedad {target.pk or 'nueva'} "
                        f"title={data.get('title')!r} address={data.get('address')!r}"
                    )
                )
                if options["dry_run"]:
                    if target.pk != property_obj.pk or would_create:
                        moved += 1
                    continue
                self._apply_property_data(target, data)
                self._apply_listing_data(listing, target, data)
                if target.pk != property_obj.pk:
                    moved += 1
        return moved

    def _target_property(self, current, data, keep_current, dry_run=False):
        fingerprint = data["fingerprint"]
        existing = Property.objects.filter(fingerprint=fingerprint).exclude(pk=current.pk).first()
        if existing:
            return existing, False
        if keep_current:
            return current, False
        if dry_run:
            return Property(fingerprint=fingerprint), True
        return Property.objects.create(
            fingerprint=fingerprint,
            property_type=data.get("property_type") or Property.Type.OTHER,
            operation=data.get("operation") or "sale",
            title=data.get("title") or "Propiedad sin titulo",
            description=data.get("description") or "",
            address=data.get("address") or "",
            normalized_address=data.get("normalized_address") or "",
            locality=data.get("locality") or "Hurlingham",
            neighborhood=data.get("neighborhood") or "",
            currency=data.get("currency") or "",
            price=data.get("price"),
            rooms=data.get("rooms"),
            bedrooms=data.get("bedrooms"),
            bathrooms=data.get("bathrooms"),
            garages=data.get("garages"),
            toilets=data.get("toilets"),
            covered_area=data.get("covered_area"),
            total_area=data.get("total_area"),
            land_area=data.get("land_area"),
            uncovered_area=data.get("uncovered_area"),
            semicovered_area=data.get("semicovered_area"),
            front_width=data.get("front_width"),
            lot_depth=data.get("lot_depth"),
            building_floors=data.get("building_floors"),
            age_years=data.get("age_years"),
            features=data.get("features") or [],
            status=data.get("status") or Property.Status.ACTIVE,
            detected_locality=data.get("detected_locality") or "",
            detected_neighborhood=data.get("detected_neighborhood") or "",
            detected_address=data.get("detected_address") or "",
            detected_latitude=data.get("detected_latitude"),
            detected_longitude=data.get("detected_longitude"),
            location_source=data.get("location_source") or Property.LocationSource.UNKNOWN,
            location_confidence=data.get("location_confidence") or Property.LocationConfidence.UNKNOWN,
            location_notes=data.get("location_notes") or "",
            location_evidence=data.get("location_evidence") or {},
        ), False

    def _apply_property_data(self, property_obj, data):
        for field in PROPERTY_FIELDS:
            value = data.get(field)
            if value in (None, []):
                continue
            if value == "" and field not in CLEARABLE_FIELDS:
                continue
            setattr(property_obj, field, value)
        property_obj.fingerprint = data["fingerprint"]
        property_obj.normalized_address = normalize_address(property_obj.address) if property_obj.address else ""
        property_obj.last_seen_at = timezone.now()
        property_obj.save()

    def _apply_listing_data(self, listing, target, data):
        listing.property = target
        listing.url = data.get("url") or listing.url
        listing.source_status = data.get("source_status") or ""
        listing.raw_data = data.get("raw_data") or {}
        listing.active = True
        listing.missing_runs = 0
        listing.save(
            update_fields=[
                "property",
                "url",
                "source_status",
                "raw_data",
                "active",
                "missing_runs",
            ]
        )

    def _safe(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
