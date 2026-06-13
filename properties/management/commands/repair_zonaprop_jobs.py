from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django.utils import timezone

from properties.models import (
    Listing,
    ListingIdentity,
    Property,
    PropertyLocation,
    ScrapeJob,
    ScrapeJobSource,
    Source,
)
from properties.services.ingestion import PROPERTY_FIELDS, manual_override_fields
from properties.services.normalization import normalize_address


TERMINAL_JOB_STATUSES = {
    ScrapeJob.Status.SUCCESS,
    ScrapeJob.Status.PARTIAL,
    ScrapeJob.Status.FAILED,
    ScrapeJob.Status.CANCELLED,
    ScrapeJob.Status.INTERRUPTED,
}
TERMINAL_SOURCE_STATUSES = {
    ScrapeJobSource.Status.SUCCESS,
    ScrapeJobSource.Status.PARTIAL,
    ScrapeJobSource.Status.FAILED,
    ScrapeJobSource.Status.CANCELLED,
    ScrapeJobSource.Status.INTERRUPTED,
}


class Command(BaseCommand):
    help = "Revierte publicaciones introducidas por ScrapeJobs afectados de Zonaprop."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", action="append", type=int, required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica los cambios. Sin este flag solo informa el dry-run.",
        )

    def handle(self, *args, **options):
        job_ids = sorted(set(options["job_id"]))
        apply_changes = options["apply"]
        source = Source.objects.filter(slug="zonaprop").first()
        if not source:
            raise CommandError("No existe la fuente zonaprop.")

        job_sources = self._job_sources(source, job_ids)
        plan = self._build_plan(source, job_sources)
        self._print_plan(plan, apply_changes)

        if not apply_changes:
            self.stdout.write(self.style.SUCCESS("Dry-run finalizado sin cambios."))
            return

        with transaction.atomic():
            self._apply_plan(plan)
        self.stdout.write(self.style.SUCCESS("Reparacion Zonaprop aplicada."))

    def _job_sources(self, source, job_ids):
        job_sources = []
        for job_id in job_ids:
            job_source = (
                ScrapeJobSource.objects.select_related("job", "source")
                .filter(job_id=job_id, source=source)
                .first()
            )
            if not job_source:
                raise CommandError(f"ScrapeJob #{job_id} no tiene fuente zonaprop.")
            if job_source.job.status not in TERMINAL_JOB_STATUSES:
                raise CommandError(f"ScrapeJob #{job_id} no esta terminado.")
            if job_source.status not in TERMINAL_SOURCE_STATUSES:
                raise CommandError(f"Zonaprop en ScrapeJob #{job_id} no esta terminado.")
            if not job_source.started_at or not job_source.finished_at:
                raise CommandError(
                    f"Zonaprop en ScrapeJob #{job_id} no tiene ventana completa."
                )
            job_sources.append(job_source)
        return job_sources

    def _build_plan(self, source, job_sources):
        created_ids = set()
        by_job = {}
        min_started = min(job_source.started_at for job_source in job_sources)
        max_finished = max(job_source.finished_at for job_source in job_sources)
        for job_source in job_sources:
            ids = set(
                Listing.objects.filter(
                    source=source,
                    first_seen_at__gte=job_source.started_at,
                    first_seen_at__lte=job_source.finished_at,
                ).values_list("id", flat=True)
            )
            by_job[job_source.job_id] = len(ids)
            created_ids.update(ids)

        rollback_listings = list(
            Listing.objects.select_related("property")
            .filter(pk__in=created_ids)
            .order_by("id")
        )
        rollback_ids = {listing.pk for listing in rollback_listings}
        active_rollback_ids = {
            listing.pk for listing in rollback_listings if listing.active
        }
        identity_items = [
            {
                "source_id": listing.source_id,
                "external_id": listing.external_id,
                "url": listing.url,
                "first_seen_at": listing.first_seen_at,
                "last_seen_at": listing.last_seen_at,
            }
            for listing in rollback_listings
        ]
        by_property = defaultdict(list)
        for listing in rollback_listings:
            by_property[listing.property_id].append(listing.pk)

        properties = (
            Property.objects.filter(pk__in=by_property)
            .prefetch_related("listings")
            .order_by("id")
        )
        delete_property_ids = []
        preserve_property_ids = []
        shared_property_ids = []
        for property_obj in properties:
            listing_ids = {listing.pk for listing in property_obj.listings.all()}
            if listing_ids and listing_ids.issubset(rollback_ids):
                if self._has_manual_state(property_obj):
                    preserve_property_ids.append(property_obj.pk)
                else:
                    delete_property_ids.append(property_obj.pk)
            else:
                shared_property_ids.append(property_obj.pk)

        delete_property_ids = sorted(delete_property_ids)
        preserve_property_ids = sorted(preserve_property_ids)
        shared_property_ids = sorted(shared_property_ids)
        listing_ids_by_property = {
            key: sorted(value) for key, value in by_property.items()
        }
        shared_listing_ids = [
            listing_id
            for property_id in shared_property_ids
            for listing_id in listing_ids_by_property[property_id]
        ]
        preserved_listing_ids = [
            listing_id
            for property_id in preserve_property_ids
            for listing_id in listing_ids_by_property[property_id]
        ]
        preserved_active_listing_ids = [
            listing_id
            for listing_id in preserved_listing_ids
            if listing_id in active_rollback_ids
        ]
        deleted_with_property_listing_ids = [
            listing_id
            for property_id in delete_property_ids
            for listing_id in listing_ids_by_property[property_id]
        ]
        return {
            "job_ids": [job_source.job_id for job_source in job_sources],
            "by_job": by_job,
            "min_started": min_started,
            "max_finished": max_finished,
            "rollback_ids": sorted(rollback_ids),
            "identity_items": identity_items,
            "affected_property_ids": sorted(by_property),
            "listing_ids_by_property": listing_ids_by_property,
            "delete_property_ids": delete_property_ids,
            "preserve_property_ids": preserve_property_ids,
            "shared_property_ids": shared_property_ids,
            "shared_listing_ids": sorted(shared_listing_ids),
            "preserved_listing_ids": sorted(preserved_listing_ids),
            "preserved_active_listing_ids": sorted(preserved_active_listing_ids),
            "deleted_with_property_listing_ids": sorted(deleted_with_property_listing_ids),
        }

    def _has_manual_state(self, property_obj):
        if property_obj.manual_overrides:
            return True
        if property_obj.data_manually_corrected_at:
            return True
        if property_obj.is_favorite or property_obj.is_hidden:
            return True
        if property_obj.reviewed_at or property_obj.personal_notes:
            return True
        try:
            return bool(property_obj.location.manually_corrected)
        except PropertyLocation.DoesNotExist:
            return False

    def _print_plan(self, plan, apply_changes):
        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(
            f"{mode} repair_zonaprop_jobs: jobs={','.join(map(str, plan['job_ids']))}"
        )
        for job_id, count in sorted(plan["by_job"].items()):
            self.stdout.write(f"  job {job_id}: listings_creados={count}")
        self.stdout.write(
            "Resumen: "
            f"listings_candidatos={len(plan['rollback_ids'])} "
            f"propiedades_afectadas={len(plan['affected_property_ids'])} "
            f"propiedades_borrables={len(plan['delete_property_ids'])} "
            f"propiedades_preservadas={len(plan['preserve_property_ids'])} "
            f"propiedades_recompuestas={len(plan['shared_property_ids'])}"
        )
        self.stdout.write(
            "Acciones: "
            f"listings_a_borrar={len(plan['shared_listing_ids']) + len(plan['deleted_with_property_listing_ids'])} "
            f"listings_a_desactivar={len(plan['preserved_active_listing_ids'])} "
            f"listings_preservados={len(plan['preserved_listing_ids'])} "
            f"propiedades_a_borrar={len(plan['delete_property_ids'])}"
        )

    def _apply_plan(self, plan):
        self._remember_listing_identities(plan)

        for property_id in plan["shared_property_ids"]:
            self._restore_shared_property(property_id, plan)

        if plan["shared_listing_ids"]:
            Listing.objects.filter(pk__in=plan["shared_listing_ids"]).delete()

        if plan["delete_property_ids"]:
            Property.objects.filter(pk__in=plan["delete_property_ids"]).delete()

        for property_obj in Property.objects.filter(pk__in=plan["preserve_property_ids"]):
            listing_ids = plan["listing_ids_by_property"][property_obj.pk]
            Listing.objects.filter(pk__in=listing_ids).update(
                active=False,
                source_status="removed",
                missing_runs=2,
            )
            self._mark_preserved_property_removed(property_obj, plan)

    def _remember_listing_identities(self, plan):
        if not plan["identity_items"]:
            return
        now = timezone.now()
        existing = set(
            ListingIdentity.objects.filter(
                source_id__in={item["source_id"] for item in plan["identity_items"]},
                external_id__in=[item["external_id"] for item in plan["identity_items"]],
            ).values_list("source_id", "external_id")
        )
        to_create = [
            ListingIdentity(
                source_id=item["source_id"],
                external_id=item["external_id"],
                url=item["url"],
                first_seen_at=item["first_seen_at"],
                last_seen_at=now,
                last_seen_reason="repair_zonaprop_jobs",
            )
            for item in plan["identity_items"]
            if (item["source_id"], item["external_id"]) not in existing
        ]
        if to_create:
            ListingIdentity.objects.bulk_create(to_create, ignore_conflicts=True)
        for item in plan["identity_items"]:
            if (item["source_id"], item["external_id"]) not in existing:
                continue
            ListingIdentity.objects.filter(
                source_id=item["source_id"],
                external_id=item["external_id"],
            ).update(
                url=item["url"],
                last_seen_at=now,
                last_seen_reason="repair_zonaprop_jobs",
            )

    def _restore_shared_property(self, property_id, plan):
        property_obj = Property.objects.get(pk=property_id)
        survivor = (
            Listing.objects.filter(property_id=property_id, active=True)
            .exclude(pk__in=plan["rollback_ids"])
            .order_by("-last_seen_at", "-id")
            .first()
        )
        if not survivor:
            property_obj.status = Property.Status.REMOVED
            property_obj.save(update_fields=["status"])
            return

        snapshot = survivor.snapshots.order_by("-observed_at").first()
        if snapshot:
            self._apply_snapshot(property_obj, snapshot.payload)
        if property_obj.status == Property.Status.REMOVED:
            property_obj.status = Property.Status.ACTIVE
            property_obj.save(update_fields=["status"])
        Property.objects.filter(pk=property_obj.pk).update(
            last_seen_at=survivor.last_seen_at
        )
        self._restore_location_from_history(property_obj, plan)

    def _apply_snapshot(self, property_obj, payload):
        protected_fields = manual_override_fields(property_obj)
        update_fields = set()
        for field_name in PROPERTY_FIELDS:
            if field_name in protected_fields or field_name not in payload:
                continue
            value = self._coerce_property_value(field_name, payload.get(field_name))
            setattr(property_obj, field_name, value)
            update_fields.add(field_name)
        if "address" in update_fields and "normalized_address" not in protected_fields:
            property_obj.normalized_address = normalize_address(property_obj.address)
            update_fields.add("normalized_address")
        if update_fields:
            property_obj.save(update_fields=sorted(update_fields))

    def _coerce_property_value(self, field_name, value):
        field = Property._meta.get_field(field_name)
        if field_name == "status" and value in (None, ""):
            return Property.Status.ACTIVE
        if field_name == "operation" and value in (None, ""):
            return "sale"
        if field_name == "property_type" and value in (None, ""):
            return Property.Type.OTHER
        if isinstance(field, models.DecimalField):
            if value in (None, ""):
                return None
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError):
                return None
        if isinstance(field, models.IntegerField):
            if value in (None, ""):
                return None
            return int(value)
        if isinstance(field, models.FloatField):
            if value in (None, ""):
                return None
            return float(value)
        if isinstance(field, models.JSONField):
            return field.get_default() if value is None else value
        return "" if value is None else value

    def _restore_location_from_history(self, property_obj, plan):
        try:
            location = property_obj.location
        except PropertyLocation.DoesNotExist:
            return
        if location.manually_corrected:
            return
        history = (
            property_obj.location_history.filter(
                changed_at__gte=plan["min_started"],
                changed_at__lte=plan["max_finished"],
            )
            .order_by("changed_at")
            .first()
        )
        if not history:
            return
        location.latitude = history.latitude
        location.longitude = history.longitude
        location.precision = history.precision
        location.provider = history.provider
        location.save(update_fields=["latitude", "longitude", "precision", "provider"])

    def _mark_preserved_property_removed(self, property_obj, plan):
        marker = (
            f"Reparacion Zonaprop jobs {', '.join(map(str, plan['job_ids']))}: "
            "publicaciones afectadas desactivadas; propiedad preservada por datos manuales."
        )
        notes = property_obj.personal_notes or ""
        if marker not in notes:
            notes = "\n\n".join(part for part in [notes, marker] if part)
        property_obj.personal_notes = notes
        property_obj.is_hidden = True
        property_obj.status = Property.Status.REMOVED
        property_obj.save(update_fields=["personal_notes", "is_hidden", "status"])
