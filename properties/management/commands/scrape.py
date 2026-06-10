from pathlib import Path
import threading
from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from properties.models import ScrapeJob
from properties.scrapers import get_adapter_classes
from properties.services.scraping import create_scrape_job, run_scrape_job, serialize_job


class Command(BaseCommand):
    help = "Recolecta propiedades desde una o mas fuentes."
    poll_interval_seconds = 2

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", dest="sources")
        parser.add_argument("--all", action="store_true", dest="all_sources")
        parser.add_argument("--max-pages", type=int, default=None)
        parser.add_argument("--start-page", type=int, default=None)
        parser.add_argument("--max-listings", type=int, default=None)
        parser.add_argument("--geocode-limit", type=int, default=25)
        parser.add_argument(
            "--mode",
            choices=[ScrapeJob.Mode.TRIAL, ScrapeJob.Mode.COMPLETE],
            default=ScrapeJob.Mode.COMPLETE,
        )
        parser.add_argument("--request-timeout", type=int, default=None)
        parser.add_argument("--max-errors", type=int, default=None)

    def handle(self, *args, **options):
        slugs = options["sources"] or []
        if options["all_sources"]:
            slugs = [
                adapter.definition.slug
                for adapter in get_adapter_classes(enabled_only=True)
            ]
        if not slugs:
            raise CommandError("Use --source SLUG o --all.")

        lock_path = Path(".scrape.lock")
        if lock_path.exists():
            raise CommandError("Ya existe una ejecucion de scraping en curso.")
        lock_path.touch()
        try:
            workers = {slug: 1 for slug in slugs}
            job = create_scrape_job(
                slugs,
                workers,
                max_pages=options["max_pages"],
                start_page=options["start_page"],
                max_listings=options["max_listings"],
                geocode_limit=options["geocode_limit"],
                scrape_mode=options["mode"],
                request_timeout_seconds=options["request_timeout"],
                max_errors_per_source=options["max_errors"],
                runner=ScrapeJob.Runner.COMMAND,
            )
            self.stdout.write(f"Iniciando job #{job.pk}...")
            self._write_estimates(job)
            if options["geocode_limit"]:
                self.stdout.write(
                    f"Luego del scraping se geocodificaran hasta {options['geocode_limit']} propiedades por fuente."
                )
            else:
                self.stdout.write("Geocodificacion posterior desactivada para esta corrida.")

            thread = threading.Thread(target=run_scrape_job, args=(job.pk,), daemon=True)
            thread.start()
            self._poll_job_progress(job.pk, thread)
            thread.join()
            job.refresh_from_db()
            payload = serialize_job(job)
            for source in payload["sources"]:
                self.stdout.write(
                    f"{source['name']}: {source['total_discovered']} URLs descubiertas; "
                    f"{source['total_to_process']} a procesar."
                )
                if source["logs"]:
                    self.stdout.write(source["logs"].rstrip())
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{source['name']}: {source['created']} nuevas, "
                        f"{source['updated']} actualizadas, {source['errors']} errores."
                    )
                )
            if payload["status"] in {"failed", "partial"}:
                raise CommandError(f"Job #{job.pk} termino con estado {payload['status_label']}.")
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        finally:
            lock_path.unlink(missing_ok=True)

    def _write_estimates(self, job):
        adapters = {
            adapter.definition.slug: adapter.definition
            for adapter in get_adapter_classes()
        }
        for slug in job.selected_sources:
            definition = adapters.get(slug)
            if not definition:
                continue
            workers = max(int(job.worker_config.get(slug, 1)), 1)
            if job.max_listings:
                seconds = (job.max_listings * definition.crawl_delay) / workers
                minutes = max(round(seconds / 60, 1), 0.1)
                self.stdout.write(
                    f"Estimacion {definition.name}: {job.max_listings} fichas con delay "
                    f"{definition.crawl_delay}s y {workers} worker(s) ~= {minutes} minutos."
                )

    def _poll_job_progress(self, job_id, thread):
        log_offsets = {}
        last_snapshot = {}
        while thread.is_alive():
            self._write_progress_snapshot(job_id, log_offsets, last_snapshot)
            sleep(self.poll_interval_seconds)
        self._write_progress_snapshot(job_id, log_offsets, last_snapshot, force=True)

    def _write_progress_snapshot(self, job_id, log_offsets, last_snapshot, force=False):
        close_old_connections()
        try:
            job = ScrapeJob.objects.prefetch_related("sources").get(pk=job_id)
        except ScrapeJob.DoesNotExist:
            return
        for source in job.sources.all():
            total = source.total_to_process or source.total_discovered or "?"
            summary = (
                source.status,
                source.processed,
                total,
                source.created,
                source.updated,
                source.skipped,
                source.errors,
                source.current_url,
                source.geocode_pending,
                source.geocoded,
                source.geocode_failed,
            )
            if force or last_snapshot.get(source.pk) != summary:
                self.stdout.write(
                    f"[{source.name}] {source.get_status_display()} | "
                    f"{source.processed}/{total} procesadas | "
                    f"{source.created} nuevas, {source.updated} actualizadas, "
                    f"{source.skipped} omitidas, {source.errors} errores"
                )
                if source.current_url:
                    self.stdout.write(f"URL actual: {source.current_url}")
                if source.geocode_pending:
                    self.stdout.write(
                        f"Geocodificacion: {source.geocoded} ubicadas, "
                        f"{source.geocode_failed} sin resultado/error, "
                        f"{source.geocode_pending} pendientes iniciales."
                    )
                last_snapshot[source.pk] = summary

            logs = source.logs or ""
            offset = log_offsets.get(source.pk, 0)
            if len(logs) < offset:
                offset = 0
            new_logs = logs[offset:]
            if new_logs.strip():
                self.stdout.write(new_logs.rstrip())
            log_offsets[source.pk] = len(logs)
