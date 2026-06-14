from collections import defaultdict
from urllib.parse import urlparse

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from properties.models import Listing, Property
from properties.services.normalization import normalize_address


FILL_IF_EMPTY_FIELDS = (
    "property_type",
    "operation",
    "title",
    "description",
    "address",
    "normalized_address",
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
    "age_years",
    "condition_category",
    "features",
    "detected_locality",
    "detected_neighborhood",
    "detected_address",
    "detected_latitude",
    "detected_longitude",
    "inferred_neighborhood",
    "inferred_neighborhood_method",
    "inferred_neighborhood_distance_m",
    "location_source",
    "location_confidence",
    "location_notes",
    "location_evidence",
)


class UnionFind:
    def __init__(self):
        self.parent = {}

    def add(self, value):
        self.parent.setdefault(value, value)

    def find(self, value):
        self.add(value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, *values):
        values = [int(value) for value in values]
        if not values:
            return
        root = self.find(values[0])
        for value in values[1:]:
            other = self.find(value)
            if other != root:
                self.parent[other] = root

    def components(self):
        grouped = defaultdict(set)
        for value in list(self.parent):
            grouped[self.find(value)].add(value)
        return sorted(
            (sorted(values) for values in grouped.values() if len(values) > 1),
            key=lambda values: (values[0], len(values), values),
        )


class Command(BaseCommand):
    help = "Fusiona propiedades duplicadas sin borrado fisico."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pair",
            action="append",
            default=[],
            help="Par de IDs a fusionar, por ejemplo 1088:3830. Compatible con el flujo anterior.",
        )
        parser.add_argument(
            "--component",
            action="append",
            default=[],
            help="Componente completo a fusionar, por ejemplo 272,672,2639.",
        )
        parser.add_argument(
            "--detect-url-tail-sources",
            default="",
            help="Slugs separados por coma. Une propiedades cuyas URLs comparten el ultimo segmento del path.",
        )
        parser.add_argument(
            "--canonical-id",
            type=int,
            help="ID de propiedad que debe quedar como canonica para todos los componentes indicados.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra las acciones sin escribir cambios.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        components, detected_stats = self._collect_components(options)
        if not components:
            raise CommandError("No hay componentes para fusionar.")
        self.stdout.write(
            f"Componentes a procesar: {len(components)}; "
            f"propiedades involucradas: {sum(len(component) for component in components)}"
        )
        if detected_stats:
            self.stdout.write(
                "Deteccion URL tail: "
                f"{detected_stats['same_property']} grupos ya unidos; "
                f"{detected_stats['multi_property']} grupos con propiedades distintas."
            )
        for property_ids in components:
            self._merge_component(property_ids, dry_run, options.get("canonical_id"))
        if not dry_run:
            cache.clear()
        suffix = "dry-run" if dry_run else "ejecutado"
        self.stdout.write(self.style.SUCCESS(f"Merge manual {suffix}: {len(components)} componentes procesados."))

    def _collect_components(self, options):
        union = UnionFind()
        for value in options["pair"]:
            union.union(*self._parse_component(value, minimum=2, maximum=2))
        for value in options["component"]:
            union.union(*self._parse_component(value, minimum=2))
        detected_stats = {}
        slugs = [slug.strip() for slug in (options["detect_url_tail_sources"] or "").split(",") if slug.strip()]
        if slugs:
            stats = self._add_url_tail_components(union, slugs)
            detected_stats = stats
        components = union.components()
        self._validate_components(components)
        return components, detected_stats

    def _parse_component(self, value, minimum=2, maximum=None):
        parts = [
            part.strip()
            for token in str(value).replace("+", ",").replace(":", ",").split(",")
            for part in [token.strip()]
            if part
        ]
        if len(parts) < minimum or (maximum is not None and len(parts) > maximum):
            raise CommandError(f"Componente invalido: {value!r}.")
        try:
            ids = [int(part) for part in parts]
        except ValueError as exc:
            raise CommandError(f"Componente invalido: {value!r}.") from exc
        if len(set(ids)) != len(ids):
            raise CommandError(f"Componente con IDs repetidos: {value!r}.")
        return ids

    def _add_url_tail_components(self, union, slugs):
        groups = defaultdict(list)
        listings = Listing.objects.select_related("source").filter(source__slug__in=slugs)
        for listing in listings:
            key = self._url_tail(listing.url)
            if key:
                groups[key].append(listing)
        same_property = 0
        multi_property = 0
        for items in groups.values():
            property_ids = sorted({item.property_id for item in items})
            source_slugs = {item.source.slug for item in items}
            if len(source_slugs) < 2:
                continue
            if len(property_ids) == 1:
                same_property += 1
                continue
            multi_property += 1
            union.union(*property_ids)
        return {"same_property": same_property, "multi_property": multi_property}

    def _url_tail(self, url):
        path = urlparse(url or "").path.rstrip("/")
        if not path:
            return ""
        return path.rsplit("/", 1)[-1].strip().lower()

    def _validate_components(self, components):
        ids = {property_id for component in components for property_id in component}
        existing = set(Property.objects.filter(pk__in=ids).values_list("pk", flat=True))
        missing = sorted(ids - existing)
        if missing:
            raise CommandError(f"No existen las propiedades: {', '.join(map(str, missing))}.")

    def _get_properties(self, property_ids):
        properties = list(
            Property.objects.filter(pk__in=property_ids)
            .prefetch_related("listings__images", "listings__agency", "listings__source")
            .order_by("pk")
        )
        by_id = {property_obj.pk: property_obj for property_obj in properties}
        return [by_id[property_id] for property_id in property_ids]

    def _choose_canonical(self, properties, canonical_id=None):
        if canonical_id is not None:
            for property_obj in properties:
                if property_obj.pk == canonical_id:
                    return property_obj
            raise CommandError(
                f"La propiedad canonica #{canonical_id} no pertenece al componente."
            )
        return sorted(properties, key=lambda item: (-self._score(item), item.pk))[0]

    def _score(self, property_obj):
        listings = list(property_obj.listings.all())
        images = sum(listing.images.count() for listing in listings)
        filled = sum(
            1
            for field in FILL_IF_EMPTY_FIELDS
            if self._has_value(getattr(property_obj, field, None))
        )
        return (
            (1000 if property_obj.status != Property.Status.REMOVED else 0)
            + (700 if not property_obj.is_hidden else 0)
            + (200 if property_obj.status == Property.Status.ACTIVE else 0)
            + filled
            + len(listings) * 3
            + min(images, 8)
            + (12 if property_obj.is_favorite else 0)
            + (6 if property_obj.personal_notes else 0)
            + (5 if hasattr(property_obj, "location") else 0)
            + (2 if property_obj.reviewed_at else 0)
        )

    def _has_value(self, value):
        if value is None:
            return False
        if value == "":
            return False
        if isinstance(value, (list, dict)) and not value:
            return False
        return True

    def _merge_component(self, property_ids, dry_run, canonical_id=None):
        properties = self._get_properties(property_ids)
        canonical = self._choose_canonical(properties, canonical_id)
        duplicates = [property_obj for property_obj in properties if property_obj.pk != canonical.pk]
        component_policy = {
            "canonical_hidden": all(property_obj.is_hidden for property_obj in properties),
            "canonical_status": (
                Property.Status.ACTIVE
                if any(property_obj.status == Property.Status.ACTIVE for property_obj in properties)
                else canonical.status
            ),
        }
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"{prefix}componente {property_ids}: {canonical.pk} absorbe "
            f"{[property_obj.pk for property_obj in duplicates]}"
        )
        for duplicate in sorted(duplicates, key=lambda item: (-self._score(item), item.pk)):
            plan = self._plan(canonical, duplicate, component_policy)
            self._print_plan(canonical, duplicate, plan, dry_run)
            if not dry_run:
                self._apply_plan(canonical, duplicate, plan)

    def _plan(self, canonical, duplicate, component_policy):
        fill_fields = []
        for field in FILL_IF_EMPTY_FIELDS:
            current = getattr(canonical, field, None)
            incoming = getattr(duplicate, field, None)
            if not self._has_value(current) and self._has_value(incoming):
                fill_fields.append((field, incoming))
        listing_ids = list(duplicate.listings.values_list("id", flat=True))
        return {
            "fill_fields": fill_fields,
            "listing_ids": listing_ids,
            "move_location": not hasattr(canonical, "location") and hasattr(duplicate, "location"),
            "favorite": canonical.is_favorite or duplicate.is_favorite,
            "reviewed_at": canonical.reviewed_at or duplicate.reviewed_at,
            "notes": self._merged_notes(canonical, duplicate),
            "manual_overrides": {**(duplicate.manual_overrides or {}), **(canonical.manual_overrides or {})},
            "first_seen_at": min(canonical.first_seen_at, duplicate.first_seen_at),
            "last_seen_at": max(canonical.last_seen_at, duplicate.last_seen_at),
            **component_policy,
        }

    def _merged_notes(self, canonical, duplicate):
        notes = canonical.personal_notes or ""
        duplicate_note = duplicate.personal_notes or ""
        marker = f"Fusionada con propiedad #{duplicate.pk}"
        if marker not in notes:
            notes = "\n\n".join(part for part in [notes, marker] if part)
        if duplicate_note and duplicate_note not in notes:
            notes = "\n\n".join([notes, f"Notas de #{duplicate.pk}: {duplicate_note}"])
        return notes

    def _print_plan(self, canonical, duplicate, plan, dry_run):
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"  {prefix}{canonical.pk} absorbe {duplicate.pk}: "
            f"{len(plan['listing_ids'])} listings, "
            f"{len(plan['fill_fields'])} campos rellenados, "
            f"favorita={plan['favorite']}, "
            f"oculta_final={plan['canonical_hidden']}, "
            f"mueve_ubicacion={plan['move_location']}"
        )
        for field, value in plan["fill_fields"]:
            self.stdout.write(f"    - {field}: {value!r}")

    @transaction.atomic
    def _apply_plan(self, canonical, duplicate, plan):
        now = timezone.now()
        update_fields = set()
        for field, value in plan["fill_fields"]:
            setattr(canonical, field, value)
            update_fields.add(field)
        canonical.is_favorite = plan["favorite"]
        canonical.is_hidden = plan["canonical_hidden"]
        canonical.status = plan["canonical_status"]
        canonical.reviewed_at = plan["reviewed_at"]
        canonical.personal_notes = plan["notes"]
        canonical.manual_overrides = plan["manual_overrides"]
        canonical.first_seen_at = plan["first_seen_at"]
        canonical.last_seen_at = plan["last_seen_at"]
        update_fields.update(
            {
                "is_favorite",
                "is_hidden",
                "status",
                "reviewed_at",
                "personal_notes",
                "manual_overrides",
                "first_seen_at",
                "last_seen_at",
            }
        )
        if "address" in update_fields:
            canonical.normalized_address = normalize_address(canonical.address)
            update_fields.add("normalized_address")
        canonical.save(update_fields=sorted(update_fields))
        Listing.objects.filter(pk__in=plan["listing_ids"]).update(property=canonical)
        if plan["move_location"]:
            location = duplicate.location
            location.property = canonical
            location.save(update_fields=["property"])
        duplicate.is_hidden = True
        duplicate.status = Property.Status.REMOVED
        duplicate.personal_notes = "\n\n".join(
            part
            for part in [
                duplicate.personal_notes,
                f"Fusionada en propiedad #{canonical.pk} el {timezone.localtime(now).isoformat()}",
            ]
            if part
        )
        duplicate.save(update_fields=["is_hidden", "status", "personal_notes"])
