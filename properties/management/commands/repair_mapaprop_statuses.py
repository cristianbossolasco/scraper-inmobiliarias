from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from properties.models import Listing, Property, Source
from properties.scrapers.registry import get_adapter
from properties.services.ingestion import manual_override_fields


REVIEW_PROPERTY_IDS = (2608, 2602, 2600, 2595, 2050, 2547)


class Command(BaseCommand):
    help = "Reparsea fichas Mapaprop para corregir estados y precios sospechosos."

    def add_arguments(self, parser):
        parser.add_argument(
            "--property-id",
            action="append",
            type=int,
            default=[],
            help="ID de propiedad a reparsear. Puede repetirse.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Audita todas las publicaciones Mapaprop existentes.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica cambios. Sin este flag solo informa el dry-run.",
        )
        parser.add_argument("--limit", type=int, help="Limita la cantidad de publicaciones a procesar.")
        parser.add_argument("--timeout", type=int, default=25, help="Timeout HTTP por ficha.")

    def handle(self, *args, **options):
        source = Source.objects.filter(slug="mapaprop").first()
        if not source:
            raise CommandError("No existe la fuente mapaprop.")

        listings = list(self._candidate_listings(source, options))
        if not listings:
            self.stdout.write("No hay publicaciones Mapaprop para reparar.")
            return

        adapter = get_adapter("mapaprop", request_timeout=options["timeout"])
        dry_run = not options["apply"]
        self.stdout.write(
            f"{'DRY-RUN' if dry_run else 'APPLY'} repair_mapaprop_statuses: "
            f"publicaciones={len(listings)}"
        )

        stats = {"changed": 0, "unchanged": 0, "errors": 0}
        preferred_prices_by_property = {}
        for listing in listings:
            try:
                parsed = adapter.parse(listing.url)
            except Exception as exc:  # pragma: no cover - exercised by live repair paths.
                stats["errors"] += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"ERROR #{listing.property_id} {listing.url}: {type(exc).__name__}: {exc}"
                    )
                )
                continue
            parsed = parsed or {}
            if parsed.get("price") is not None:
                preferred_prices_by_property[listing.property_id] = (
                    parsed.get("currency") or "",
                    parsed.get("price"),
                )
            changes = self._planned_changes(
                listing,
                parsed,
                preserve_valid_price=(
                    parsed.get("price") is None
                    and listing.property_id in preferred_prices_by_property
                ),
            )
            if changes["summary"]:
                stats["changed"] += 1
                self._write_change_summary(listing, changes, dry_run)
                if not dry_run:
                    self._apply_changes(listing, changes)
            else:
                stats["unchanged"] += 1

        if not dry_run:
            cache.clear()
        self.stdout.write(
            self.style.SUCCESS(
                f"Finalizado: cambios={stats['changed']} sin_cambios={stats['unchanged']} "
                f"errores={stats['errors']}"
            )
        )

    def _candidate_listings(self, source, options):
        queryset = (
            Listing.objects.select_related("property", "source")
            .filter(source=source)
            .order_by("property_id", "pk")
        )
        property_ids = set(options["property_id"] or [])
        if not options["all"]:
            if property_ids:
                queryset = queryset.filter(property_id__in=property_ids)
            else:
                property_ids.update(REVIEW_PROPERTY_IDS)
                suspicious = (
                    Q(property__price=1)
                    | Q(property__price__isnull=True)
                    | Q(property__currency="ARS")
                    | ~Q(property__status=Property.Status.ACTIVE)
                    | ~Q(source_status="")
                )
                queryset = queryset.filter(suspicious | Q(property_id__in=property_ids))
        elif property_ids:
            queryset = queryset.filter(property_id__in=property_ids)
        limit = options.get("limit")
        if limit:
            queryset = queryset[:limit]
        return queryset

    def _protected_fields(self, property_obj):
        protected = set(manual_override_fields(property_obj))
        if "price" in protected:
            protected.add("currency")
        if "currency" in protected:
            protected.add("price")
        return protected

    def _planned_changes(self, listing, parsed, preserve_valid_price=False):
        property_obj = listing.property
        protected = self._protected_fields(property_obj)
        parsed_status = parsed.get("status") or Property.Status.ACTIVE
        parsed_source_status = parsed.get("source_status") or ""
        parsed_price = parsed.get("price")
        parsed_currency = parsed.get("currency") or ""
        property_updates = {}
        listing_updates = {}
        summary = []

        if "status" not in protected and property_obj.status != parsed_status:
            property_updates["status"] = parsed_status
            summary.append(f"status {property_obj.status}->{parsed_status}")
        if listing.source_status != parsed_source_status:
            listing_updates["source_status"] = parsed_source_status
            summary.append(
                f"source_status {listing.source_status or '-'}->{parsed_source_status or '-'}"
            )
        if not preserve_valid_price and "price" not in protected and property_obj.price != parsed_price:
            property_updates["price"] = parsed_price
            summary.append(f"price {property_obj.price or '-'}->{parsed_price or '-'}")
        if (
            not preserve_valid_price
            and "currency" not in protected
            and (property_obj.currency or "") != parsed_currency
        ):
            property_updates["currency"] = parsed_currency
            summary.append(f"currency {property_obj.currency or '-'}->{parsed_currency or '-'}")

        raw_data = dict(listing.raw_data or {})
        parsed_raw = parsed.get("raw_data") or {}
        raw_changed = False
        if parsed_raw:
            for key, value in parsed_raw.items():
                if raw_data.get(key) != value:
                    raw_data[key] = value
                    raw_changed = True
        if summary or raw_changed:
            raw_data["mapaprop_repair"] = {
                "observed_at": timezone.now().isoformat(),
                "status": parsed_status,
                "source_status": parsed_source_status,
                "price": str(parsed_price) if parsed_price is not None else "",
                "currency": parsed_currency,
                "protected_fields": sorted(protected),
            }
            if raw_data != (listing.raw_data or {}):
                listing_updates["raw_data"] = raw_data
                if not summary:
                    summary.append("raw_data actualizado")

        return {
            "property_updates": property_updates,
            "listing_updates": listing_updates,
            "summary": summary,
            "protected": protected,
        }

    def _write_change_summary(self, listing, changes, dry_run):
        prefix = "ACTUALIZARIA" if dry_run else "ACTUALIZA"
        protected = ", protegidos=" + ",".join(sorted(changes["protected"])) if changes["protected"] else ""
        self.stdout.write(
            f"{prefix} #{listing.property_id} listing={listing.pk}: "
            f"{'; '.join(changes['summary'])}{protected}"
        )

    @transaction.atomic
    def _apply_changes(self, listing, changes):
        property_updates = changes["property_updates"]
        listing_updates = changes["listing_updates"]
        if property_updates:
            Property.objects.filter(pk=listing.property_id).update(**property_updates)
        if listing_updates:
            Listing.objects.filter(pk=listing.pk).update(**listing_updates)
