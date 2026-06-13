from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from properties.models import Listing, ScrapeJob, ScrapeJobSource
from properties.services.ingestion import mark_missing


JOB_SOURCE_EXCLUSIONS = {
    166: {"odriozola", "zonaprop", "remax-datawork"},
}


class Command(BaseCommand):
    help = "Marca publicaciones ausentes reconstruyendo vistas desde un ScrapeJob previo."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica los cambios. Sin este flag solo informa el dry-run.",
        )

    def handle(self, *args, **options):
        job = (
            ScrapeJob.objects.prefetch_related("sources__source")
            .filter(pk=options["job_id"])
            .first()
        )
        if not job:
            raise CommandError(f"No existe ScrapeJob #{options['job_id']}.")

        apply_changes = options["apply"]
        mode_label = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(
            f"{mode_label} ScrapeJob #{job.pk}: {job.started_at} -> {job.finished_at}"
        )

        totals = {
            "sources_applied": 0,
            "sources_skipped": 0,
            "seen": 0,
            "stale": 0,
            "will_increment": 0,
            "will_deactivate": 0,
            "will_remove_properties": 0,
        }

        for job_source in job.sources.select_related("source").order_by("slug"):
            skip_reason = self._skip_reason(job, job_source)
            if skip_reason:
                totals["sources_skipped"] += 1
                self.stdout.write(f"[SKIP] {job_source.slug}: {skip_reason}")
                continue

            seen_external_ids = self._seen_external_ids(job_source)
            stale = self._stale_queryset(job_source, seen_external_ids)
            stale_count = stale.count()
            will_deactivate_queryset = stale.filter(missing_runs__gte=1)
            will_deactivate = will_deactivate_queryset.count()
            will_remove_properties = self._property_removal_count(will_deactivate_queryset)
            will_increment = stale_count - will_deactivate

            totals["sources_applied"] += 1
            totals["seen"] += len(seen_external_ids)
            totals["stale"] += stale_count
            totals["will_increment"] += will_increment
            totals["will_deactivate"] += will_deactivate
            totals["will_remove_properties"] += will_remove_properties

            action = "[APPLY]" if apply_changes else "[DRY]"
            self.stdout.write(
                f"{action} {job_source.slug}: vistas={len(seen_external_ids)} "
                f"ausentes={stale_count} incrementan={will_increment} "
                f"desactivan={will_deactivate} "
                f"propiedades_retiran={will_remove_properties}"
            )

            if apply_changes:
                with transaction.atomic():
                    mark_missing(job_source.source, seen_external_ids)

        suffix = "" if apply_changes else " (sin cambios)"
        self.stdout.write(
            self.style.SUCCESS(
                "Resumen: "
                f"fuentes_aplicables={totals['sources_applied']} "
                f"fuentes_omitidas={totals['sources_skipped']} "
                f"vistas={totals['seen']} "
                f"ausentes={totals['stale']} "
                f"incrementan={totals['will_increment']} "
                f"desactivan={totals['will_deactivate']}"
                f" propiedades_retiran={totals['will_remove_properties']}"
                f"{suffix}"
            )
        )

    def _skip_reason(self, job, job_source):
        excluded = JOB_SOURCE_EXCLUSIONS.get(job.pk, set())
        if job_source.slug in excluded:
            return "omitida por exclusion conservadora para este job"
        if job.scrape_mode != ScrapeJob.Mode.COMPLETE:
            return "job no fue completo"
        if job.max_pages is not None or job.start_page is not None or job.max_listings is not None:
            return "job tuvo limites de muestra/paginacion"
        if (job.retry_urls or {}).get(job_source.slug):
            return "fuente fue reproceso selectivo"
        if job_source.status != ScrapeJobSource.Status.SUCCESS:
            return f"estado {job_source.status}"
        if job_source.total_to_process <= 0:
            return "sin fichas procesables"
        if job_source.processed != job_source.total_to_process:
            return f"procesadas {job_source.processed}/{job_source.total_to_process}"
        if job_source.errors:
            return f"{job_source.errors} errores"
        if not job_source.started_at or not job_source.finished_at:
            return "sin ventana temporal completa"
        return ""

    def _seen_external_ids(self, job_source):
        return list(
            Listing.objects.filter(
                source=job_source.source,
                last_seen_at__gte=job_source.started_at,
                last_seen_at__lte=job_source.finished_at,
            )
            .order_by()
            .values_list("external_id", flat=True)
            .distinct()
        )

    def _stale_queryset(self, job_source, seen_external_ids):
        return Listing.objects.filter(source=job_source.source, active=True).exclude(
            external_id__in=seen_external_ids
        )

    def _property_removal_count(self, listings_to_deactivate):
        listing_ids = set(listings_to_deactivate.values_list("id", flat=True))
        property_ids = (
            listings_to_deactivate.order_by()
            .values_list("property_id", flat=True)
            .distinct()
        )
        count = 0
        for property_id in property_ids:
            if not Listing.objects.filter(property_id=property_id, active=True).exclude(
                pk__in=listing_ids
            ).exists():
                count += 1
        return count
