import threading
import queue
import random
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from time import monotonic, sleep

from django.conf import settings
from django.db import OperationalError, close_old_connections, connection, transaction
from django.utils import timezone

from properties.models import Property, ScrapeJob, ScrapeJobSource, ScrapeRun, Source
from properties.scrapers import get_adapter, get_adapter_classes
from properties.services.geocoding import Geocoder, has_geocodable_address
from properties.services.ingestion import ingest_listing, mark_listing_removed, mark_missing


WRITE_LOCK = threading.RLock()
JOB_THREADS = {}
DB_WRITER = None
DB_WRITER_LOCK = threading.Lock()
ACTIVE_JOB_STATUSES = {ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING}
ACTIVE_SOURCE_STATUSES = {
    ScrapeJobSource.Status.PENDING,
    ScrapeJobSource.Status.DISCOVERING,
    ScrapeJobSource.Status.RUNNING,
}
BLOCKED_SOURCE_SLUGS = {"inmuebles-clarin"}


class ActiveScrapeJobError(ValueError):
    def __init__(self, active_job_id):
        self.active_job_id = active_job_id
        super().__init__(f"Ya hay un scraping en curso: Job #{active_job_id}.")


class ListingGoneError(RuntimeError):
    def __init__(self, url=None, external_id=None, message=None):
        self.url = url
        self.external_id = external_id
        super().__init__(message or "Publicacion retirada o no disponible.")


def using_sqlite():
    return settings.DATABASES["default"]["ENGINE"].endswith("sqlite3")


class DbWriteQueue:
    def __init__(self):
        self.items = queue.Queue()
        self.thread_id = None
        self.stats_lock = threading.Lock()
        self.stats = {
            "safe_sqlite": using_sqlite(),
            "queued": 0,
            "completed": 0,
            "lock_retries": 0,
            "max_wait_seconds": 0.0,
        }
        self.thread = threading.Thread(target=self._run, daemon=True, name="sqlite-db-writer")
        self.thread.start()

    def submit(self, operation):
        if threading.get_ident() == self.thread_id:
            return direct_db_write(operation)
        event = threading.Event()
        payload = {
            "operation": operation,
            "result": None,
            "error": None,
            "event": event,
            "enqueued_at": monotonic(),
        }
        with self.stats_lock:
            self.stats["queued"] += 1
        self.items.put(payload)
        event.wait()
        waited = monotonic() - payload["enqueued_at"]
        with self.stats_lock:
            self.stats["max_wait_seconds"] = max(self.stats["max_wait_seconds"], round(waited, 3))
        if payload["error"] is not None:
            raise payload["error"]
        return payload["result"]

    def snapshot(self):
        with self.stats_lock:
            return dict(self.stats)

    def _run(self):
        self.thread_id = threading.get_ident()
        close_old_connections()
        while True:
            payload = self.items.get()
            try:
                payload["result"] = direct_db_write(payload["operation"], stats=self)
            except Exception as exc:
                payload["error"] = exc
            finally:
                with self.stats_lock:
                    self.stats["completed"] += 1
                payload["event"].set()
                close_old_connections()

    def record_lock_retry(self):
        with self.stats_lock:
            self.stats["lock_retries"] += 1


def db_writer():
    global DB_WRITER
    if DB_WRITER is None:
        with DB_WRITER_LOCK:
            if DB_WRITER is None:
                DB_WRITER = DbWriteQueue()
    return DB_WRITER


def is_database_locked(exc):
    return "database is locked" in str(exc).lower() or "database table is locked" in str(exc).lower()


def is_source_block_error(exc):
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("403", "forbidden", "cloudfront", "request blocked")
    )


def is_listing_gone_error(exc):
    if isinstance(exc, ListingGoneError):
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in {404, 410}:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "aviso terminado",
            "404 client error",
            "410 client error",
            "404 not found",
            "410 gone",
        )
    )


def get_adapter_compatible(slug, **kwargs):
    try:
        return get_adapter(slug, **kwargs)
    except TypeError as exc:
        if "start_page" not in kwargs or "start_page" not in str(exc):
            raise
        compatible_kwargs = dict(kwargs)
        compatible_kwargs.pop("start_page", None)
        return get_adapter(slug, **compatible_kwargs)


def direct_db_write(operation, retries=8, stats=None):
    last_error = None
    for attempt in range(retries):
        try:
            with WRITE_LOCK:
                return operation()
        except OperationalError as exc:
            if not is_database_locked(exc):
                raise
            last_error = exc
            close_old_connections()
            if stats is not None:
                stats.record_lock_retry()
            sleep(min((0.25 * (2**attempt)) + random.uniform(0, 0.15), 3))
    raise last_error


def db_write(operation):
    if connection.in_atomic_block:
        return direct_db_write(operation)
    if using_sqlite():
        return db_writer().submit(operation)
    return direct_db_write(operation)


def db_writer_snapshot():
    if not using_sqlite():
        return {"safe_sqlite": False, "queued": 0, "completed": 0, "lock_retries": 0, "max_wait_seconds": 0.0}
    return db_writer().snapshot()


def active_scrape_job():
    return (
        ScrapeJob.objects.filter(
            status__in=ACTIVE_JOB_STATUSES,
            sources__status__in=ACTIVE_SOURCE_STATUSES,
        )
        .distinct()
        .order_by("-created_at")
        .first()
    )


def source_catalog(include_disabled=True):
    adapters = get_adapter_classes(enabled_only=not include_disabled)
    return [
        {
            "slug": adapter.definition.slug,
            "name": adapter.definition.name,
            "enabled": adapter.definition.enabled,
            "crawl_delay": adapter.definition.crawl_delay,
            "notes": adapter.definition.notes,
        }
        for adapter in adapters
        if adapter.definition.slug not in BLOCKED_SOURCE_SLUGS
    ]


def ensure_source(definition):
    def operation():
        source, _ = Source.objects.update_or_create(
            slug=definition.slug,
            defaults={
                "name": definition.name,
                "base_url": definition.base_url,
                "enabled": definition.enabled,
                "crawl_delay_seconds": definition.crawl_delay,
                "notes": definition.notes,
            },
        )
        return source

    return db_write(operation)


def append_source_log(job_source, message):
    timestamp = timezone.localtime().strftime("%H:%M:%S")
    job_source.logs = (job_source.logs + f"[{timestamp}] {message}\n")[-8000:]
    db_write(lambda: job_source.save(update_fields=["logs"]))


def elapsed_seconds(started_at, finished_at=None):
    if not started_at:
        return 0
    end = finished_at or timezone.now()
    return max(round((end - started_at).total_seconds()), 0)


def record_source_error(job_source, url, exc):
    entry = {
        "url": url,
        "error": str(exc),
        "timestamp": timezone.localtime().isoformat(),
    }
    errors = list(job_source.error_urls or [])
    errors.append(entry)
    job_source.error_urls = errors[-200:]


def serialize_job(job):
    job.refresh_from_db()
    sources = []
    for source in job.sources.select_related("source").order_by("name"):
        percent = 0
        if source.total_to_process:
            percent = round((source.processed / source.total_to_process) * 100, 1)
        sources.append(
            {
                "slug": source.slug,
                "name": source.name,
                "status": source.status,
                "status_label": source.get_status_display(),
                "workers": source.workers,
                "total_discovered": source.total_discovered,
                "total_to_process": source.total_to_process,
                "processed": source.processed,
                "created": source.created,
                "updated": source.updated,
                "skipped": source.skipped,
                "errors": source.errors,
                "geocode_pending": source.geocode_pending,
                "geocoded": source.geocoded,
                "geocode_failed": source.geocode_failed,
                "current_url": source.current_url,
                "error_urls": source.error_urls or [],
                "logs": source.logs,
                "percent": percent,
                "started_at": source.started_at.isoformat() if source.started_at else None,
                "finished_at": source.finished_at.isoformat() if source.finished_at else None,
                "elapsed_seconds": elapsed_seconds(source.started_at, source.finished_at),
            }
        )
    return {
        "id": job.pk,
        "status": job.status,
        "status_label": job.get_status_display(),
        "cancel_requested": job.cancel_requested,
        "selected_sources": job.selected_sources,
        "worker_config": job.worker_config,
        "runner": job.runner,
        "runner_label": job.get_runner_display(),
        "scrape_mode": job.scrape_mode,
        "scrape_mode_label": job.get_scrape_mode_display(),
        "max_pages": job.max_pages,
        "start_page": job.start_page,
        "max_listings": job.max_listings,
        "geocode_limit": job.geocode_limit,
        "mark_missing": job.mark_missing,
        "request_timeout_seconds": job.request_timeout_seconds,
        "max_errors_per_source": job.max_errors_per_source,
        "retry_urls": job.retry_urls,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_seconds": elapsed_seconds(job.started_at, job.finished_at),
        "error_log": job.error_log,
        "db_writer": db_writer_snapshot(),
        "sources": sources,
    }


def mark_stale_running_jobs():
    live_ids = {job_id for job_id, thread in JOB_THREADS.items() if thread.is_alive()}
    unfinished_statuses = [
        ScrapeJobSource.Status.PENDING,
        ScrapeJobSource.Status.DISCOVERING,
        ScrapeJobSource.Status.RUNNING,
    ]
    stale = (
        ScrapeJob.objects.filter(
            status=ScrapeJob.Status.RUNNING,
            runner=ScrapeJob.Runner.WEB,
        )
        .exclude(pk__in=live_ids)
    )
    now = timezone.now()
    for job in stale:
        job.status = ScrapeJob.Status.INTERRUPTED
        job.finished_at = job.finished_at or now
        job.error_log += "Servidor reiniciado o thread no disponible.\n"
        job.save(update_fields=["status", "finished_at", "error_log"])
        job.sources.filter(status__in=unfinished_statuses).update(
            status=ScrapeJobSource.Status.INTERRUPTED, finished_at=now
        )

    inconsistent = ScrapeJob.objects.filter(
        status=ScrapeJob.Status.SUCCESS,
        sources__status__in=unfinished_statuses,
    ).distinct()
    for job in inconsistent:
        job.status = ScrapeJob.Status.PARTIAL
        job.finished_at = job.finished_at or now
        job.error_log += "Job finalizado con fuentes pendientes; se marca como parcial.\n"
        job.save(update_fields=["status", "finished_at", "error_log"])
        job.sources.filter(status__in=unfinished_statuses).update(
            status=ScrapeJobSource.Status.INTERRUPTED, finished_at=now
        )


def create_scrape_job(
    selected_sources,
    worker_config,
    max_pages=None,
    start_page=None,
    max_listings=None,
    geocode_limit=25,
    mark_missing=True,
    scrape_mode=ScrapeJob.Mode.COMPLETE,
    request_timeout_seconds=None,
    max_errors_per_source=None,
    runner=ScrapeJob.Runner.WEB,
    retry_urls=None,
    enforce_single_active=False,
):
    if scrape_mode not in ScrapeJob.Mode.values:
        raise ValueError("Modo de scraping invalido.")
    if scrape_mode == ScrapeJob.Mode.TRIAL and max_listings is None and not retry_urls:
        max_listings = 3
    cleaned_sources = []
    cleaned_workers = {}
    adapters = {}
    for slug in selected_sources:
        if slug in BLOCKED_SOURCE_SLUGS:
            raise ValueError(f"La fuente {slug} esta bloqueada permanentemente.")
        adapter = get_adapter(slug)
        adapters[slug] = adapter
        cleaned_sources.append(slug)
        workers = int(worker_config.get(slug, 1))
        if workers < 1:
            raise ValueError(f"Workers invalido para {slug}")
        cleaned_workers[slug] = workers
    if not cleaned_sources:
        raise ValueError("Seleccione al menos una fuente.")

    def operation():
        if enforce_single_active:
            active = active_scrape_job()
            if active:
                raise ActiveScrapeJobError(active.pk)
        job = ScrapeJob.objects.create(
            selected_sources=cleaned_sources,
            worker_config=cleaned_workers,
            runner=runner,
            scrape_mode=scrape_mode,
            max_pages=max_pages,
            start_page=start_page,
            max_listings=max_listings,
            geocode_limit=geocode_limit,
            mark_missing=mark_missing,
            request_timeout_seconds=request_timeout_seconds,
            max_errors_per_source=max_errors_per_source,
            retry_urls=retry_urls or {},
        )
        for slug in cleaned_sources:
            adapter = adapters[slug]
            source = ensure_source(adapter.definition)
            ScrapeJobSource.objects.create(
                job=job,
                source=source,
                slug=slug,
                name=adapter.definition.name,
                workers=cleaned_workers[slug],
            )
        return job

    job = db_write(operation)
    return job


def retry_scrape_job(original_job, enforce_single_active=False):
    return create_scrape_job(
        original_job.selected_sources,
        original_job.worker_config,
        max_pages=original_job.max_pages,
        start_page=original_job.start_page,
        max_listings=original_job.max_listings,
        geocode_limit=original_job.geocode_limit,
        mark_missing=original_job.mark_missing,
        scrape_mode=original_job.scrape_mode,
        request_timeout_seconds=original_job.request_timeout_seconds,
        max_errors_per_source=original_job.max_errors_per_source,
        runner=ScrapeJob.Runner.WEB,
        enforce_single_active=enforce_single_active,
    )


def retry_scrape_job_errors(original_job, enforce_single_active=False):
    retry_urls = {}
    selected_sources = []
    worker_config = {}
    for source in original_job.sources.all():
        urls = [
            item.get("url")
            for item in (source.error_urls or [])
            if item.get("url")
        ]
        urls = list(dict.fromkeys(urls))
        if not urls:
            continue
        retry_urls[source.slug] = urls
        selected_sources.append(source.slug)
        worker_config[source.slug] = source.workers
    if not selected_sources:
        raise ValueError("El job no tiene URLs con error para reprocesar.")
    return create_scrape_job(
        selected_sources,
        worker_config,
        max_pages=None,
        start_page=None,
        max_listings=None,
        geocode_limit=original_job.geocode_limit,
        mark_missing=original_job.mark_missing,
        scrape_mode=ScrapeJob.Mode.TRIAL,
        request_timeout_seconds=original_job.request_timeout_seconds,
        max_errors_per_source=original_job.max_errors_per_source,
        runner=ScrapeJob.Runner.WEB,
        retry_urls=retry_urls,
        enforce_single_active=enforce_single_active,
    )


def start_scrape_job(job):
    thread = threading.Thread(target=run_scrape_job, args=(job.pk,), daemon=True)
    JOB_THREADS[job.pk] = thread
    thread.start()
    return thread


def job_cancelled(job_id):
    return ScrapeJob.objects.filter(pk=job_id, cancel_requested=True).exists()


def run_scrape_job(job_id):
    close_old_connections()
    current_thread = threading.current_thread()
    if job_id not in JOB_THREADS:
        JOB_THREADS[job_id] = current_thread
    job = ScrapeJob.objects.get(pk=job_id)
    now = timezone.now()
    job.status = ScrapeJob.Status.RUNNING
    job.started_at = now
    db_write(lambda: job.save(update_fields=["status", "started_at"]))

    try:
        with ThreadPoolExecutor(max_workers=len(job.selected_sources)) as executor:
            futures = [
                executor.submit(run_scrape_job_source, job_id, slug)
                for slug in job.selected_sources
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    job.error_log += f"{exc}\n"
                    db_write(lambda: job.save(update_fields=["error_log"]))
        job.refresh_from_db()
        source_statuses = list(job.sources.values_list("status", flat=True))
        unfinished = {
            ScrapeJobSource.Status.PENDING,
            ScrapeJobSource.Status.DISCOVERING,
            ScrapeJobSource.Status.RUNNING,
            ScrapeJobSource.Status.INTERRUPTED,
        }
        if job.cancel_requested or all(
            status == ScrapeJobSource.Status.CANCELLED for status in source_statuses
        ):
            job.status = ScrapeJob.Status.CANCELLED
        elif any(
            status
            in {
                ScrapeJobSource.Status.FAILED,
                ScrapeJobSource.Status.PARTIAL,
                *unfinished,
            }
            for status in source_statuses
        ):
            job.status = ScrapeJob.Status.PARTIAL
        else:
            job.status = ScrapeJob.Status.SUCCESS
    except Exception as exc:
        job.status = ScrapeJob.Status.FAILED
        job.error_log += f"{exc}\n"
    finally:
        job.finished_at = timezone.now()
        db_write(lambda: job.save(update_fields=["status", "finished_at", "error_log"]))
        if JOB_THREADS.get(job_id) is current_thread:
            JOB_THREADS.pop(job_id, None)
        close_old_connections()


def run_scrape_job_source(job_id, slug):
    close_old_connections()
    job = ScrapeJob.objects.get(pk=job_id)
    job_source = ScrapeJobSource.objects.select_related("source").get(
        job_id=job_id, slug=slug
    )
    source = job_source.source
    run = None
    seen = []
    geocode_candidate_ids = set()
    stopped_by_block = False

    try:
        if slug in BLOCKED_SOURCE_SLUGS:
            job_source.status = ScrapeJobSource.Status.FAILED
            job_source.errors += 1
            append_source_log(job_source, f"Fuente bloqueada permanentemente: {slug}.")
            db_write(lambda: job_source.save(update_fields=["status", "errors"]))
            return
        job_source.status = ScrapeJobSource.Status.DISCOVERING
        job_source.started_at = timezone.now()
        db_write(lambda: job_source.save(update_fields=["status", "started_at"]))
        append_source_log(job_source, "Descubriendo URLs...")
        if using_sqlite():
            append_source_log(job_source, "Modo seguro SQLite activo: escrituras serializadas por cola.")
        adapter = get_adapter_compatible(
            slug,
            max_pages=job.max_pages,
            start_page=job.start_page,
            request_timeout=job.request_timeout_seconds,
            max_listings=job.max_listings,
            should_cancel=lambda: job_cancelled(job_id),
        )
        run = db_write(lambda: ScrapeRun.objects.create(source=source, status=ScrapeRun.Status.RUNNING))
        if slug == "argenprop":
            append_source_log(job_source, "Modo conservador Argenprop: delay 7s, corte temprano ante 403/CDN y tandas por pagina.")
            if job_source.workers > 1:
                append_source_log(job_source, "Advertencia: Argenprop funciona mejor con 1 worker para evitar bloqueos.")
            if job.start_page:
                append_source_log(job_source, f"Inicio de paginacion: pagina {job.start_page}.")
        if job_cancelled(job_id):
            job_source.status = ScrapeJobSource.Status.CANCELLED
            append_source_log(job_source, "Cancelacion solicitada antes de descubrir URLs.")
            return
        retry_source_urls = list(dict.fromkeys((job.retry_urls or {}).get(slug, [])))
        if retry_source_urls:
            discovered_urls = retry_source_urls
            adapter.discovery_stats = {
                "retry_urls": True,
                "urls_discovered": len(discovered_urls),
            }
            append_source_log(job_source, f"Reproceso selectivo: {len(discovered_urls)} URLs con error.")
        else:
            try:
                discovered_urls = []
                for discovered_url in adapter.discover():
                    if job_cancelled(job_id):
                        break
                    discovered_urls.append(discovered_url)
            except Exception as exc:
                if not is_source_block_error(exc):
                    raise
                stopped_by_block = True
                job_source.status = ScrapeJobSource.Status.PARTIAL
                job_source.errors += 1
                if run is not None:
                    run.errors += 1
                    run.error_log += f"Discovery bloqueado: {exc}\n"
                append_source_log(
                    job_source,
                    "Fuente detenida automaticamente por bloqueo 403/CDN durante discovery. No se marcan ausentes.",
                )
                if slug == "argenprop":
                    append_source_log(job_source, "Argenprop bloqueo la IP/CDN durante discovery; se detuvo para proteger la red. Proba mas tarde con 1 worker y tandas chicas.")
                else:
                    append_source_log(job_source, "Proba mas tarde, con otra red o con menos workers.")
                return
        job_source.total_discovered = len(discovered_urls)
        discovery_stats = getattr(adapter, "discovery_stats", {}) or {}
        if job_cancelled(job_id) or discovery_stats.get("cancelled"):
            job_source.status = ScrapeJobSource.Status.CANCELLED
            append_source_log(job_source, "Cancelacion solicitada durante discovery.")
            return
        if job.max_listings is not None:
            discovered_urls = discovered_urls[: job.max_listings]
        job_source.total_to_process = len(discovered_urls)
        job_source.status = ScrapeJobSource.Status.RUNNING
        db_write(lambda: job_source.save(update_fields=["total_discovered", "total_to_process", "status"]))
        declared_total = discovery_stats.get("declared_total")
        coverage_ratio = discovery_stats.get("coverage_ratio")
        coverage_complete = discovery_stats.get("coverage_complete", True)
        limited_by_max_listings = discovery_stats.get("limited_by_max_listings")
        limited_by_max_pages = discovery_stats.get("limited_by_max_pages")
        if declared_total:
            if discovery_stats.get("segmented"):
                append_source_log(
                    job_source,
                    f"Cobertura discovery segmentada: {discovery_stats.get('urls_discovered', job_source.total_discovered)}/{declared_total} "
                    f"({coverage_ratio}%) en {discovery_stats.get('segments_seen', '?')} segmentos y {discovery_stats.get('pages_seen', '?')} paginas.",
                )
            if limited_by_max_listings:
                append_source_log(
                    job_source,
                    f"Muestra limitada: discovery cortado en {job_source.total_discovered}/{declared_total} fichas declaradas tras {discovery_stats.get('pages_seen', '?')} paginas.",
                )
            elif not discovery_stats.get("segmented"):
                append_source_log(
                    job_source,
                    f"Cobertura discovery: {discovery_stats.get('urls_discovered', job_source.total_discovered)}/{declared_total} ({coverage_ratio}%) en {discovery_stats.get('pages_seen', '?')} paginas.",
                )
        elif limited_by_max_listings or limited_by_max_pages:
            append_source_log(
                job_source,
                f"Tanda limitada: discovery acotado a {discovery_stats.get('pages_seen', '?')} paginas y {job_source.total_discovered} fichas descubiertas.",
            )
        append_source_log(
            job_source,
            f"{job_source.total_discovered} descubiertas; {job_source.total_to_process} a procesar.",
        )

        if not discovered_urls:
            job_source.status = ScrapeJobSource.Status.SUCCESS
            return

        with ThreadPoolExecutor(max_workers=job_source.workers) as executor:
            urls = iter(discovered_urls)
            pending = {}
            cancel_logged = False
            block_errors_total = 0
            block_errors_consecutive = 0

            def mark_cancelling():
                nonlocal cancel_logged
                if not cancel_logged:
                    append_source_log(job_source, "Cancelacion solicitada; esperando tareas en curso...")
                    cancel_logged = True

            def block_threshold_reached():
                if slug == "argenprop":
                    if block_errors_consecutive >= 3 or block_errors_total >= 10:
                        return True
                    if job_source.processed >= 15 and block_errors_total / max(job_source.processed, 1) > 0.20:
                        return True
                    return False
                if block_errors_consecutive >= 5 or block_errors_total >= 20:
                    return True
                if job_source.processed >= 30 and block_errors_total / max(job_source.processed, 1) > 0.30:
                    return True
                return False

            def stop_for_block():
                nonlocal stopped_by_block
                if stopped_by_block:
                    return
                stopped_by_block = True
                append_source_log(
                    job_source,
                    "Fuente detenida automaticamente por bloqueo 403/CDN. No se marcan ausentes.",
                )
                if slug == "argenprop":
                    append_source_log(job_source, "Argenprop bloqueo la IP/CDN; se detuvo para proteger la red. Proba mas tarde con 1 worker y tandas chicas.")
                else:
                    append_source_log(job_source, "Proba mas tarde, con otra red o con menos workers.")

            def submit_next():
                if stopped_by_block:
                    return False
                if job_cancelled(job_id):
                    mark_cancelling()
                    return False
                if (
                    job.max_errors_per_source is not None
                    and job_source.errors >= job.max_errors_per_source
                ):
                    return False
                try:
                    next_url = next(urls)
                except StopIteration:
                    return False
                future = executor.submit(
                    parse_one_url,
                    slug,
                    job.max_pages,
                    job.start_page,
                    job.request_timeout_seconds,
                    next_url,
                )
                pending[future] = next_url
                return True

            for _ in range(job_source.workers):
                if not submit_next():
                    break

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    original_url = pending.pop(future)
                    if future.cancelled():
                        append_source_log(job_source, f"Cancelada antes de iniciar: {original_url}")
                        continue
                    data = None
                    url = original_url
                    elapsed = 0
                    cancelled = job_cancelled(job_id)
                    if cancelled:
                        mark_cancelling()
                    try:
                        url, data, elapsed = future.result()
                        job_source.current_url = url
                        if data is None:
                            job_source.skipped += 1
                            append_source_log(job_source, f"Omitida: {url}")
                        else:
                            listing, created = db_write(lambda: ingest_listing(source, data))
                            seen.append(listing.external_id)
                            if has_geocodable_address(listing.property) and not hasattr(listing.property, "location"):
                                geocode_candidate_ids.add(listing.property_id)
                            if created:
                                job_source.created += 1
                                run.created += 1
                            else:
                                job_source.updated += 1
                                run.updated += 1
                            append_source_log(
                                job_source,
                                f"OK {url} en {elapsed:.1f}s",
                            )
                            block_errors_consecutive = 0
                    except Exception as exc:
                        if is_listing_gone_error(exc):
                            job_source.current_url = url
                            job_source.skipped += 1
                            removed = db_write(
                                lambda: mark_listing_removed(
                                    source,
                                    url=getattr(exc, "url", None) or url,
                                    external_id=getattr(exc, "external_id", None),
                                )
                            )
                            suffix = "listing dado de baja" if removed else "no existia en DB"
                            append_source_log(
                                job_source,
                                f"Retirada: URL no disponible, {suffix}: {url}",
                            )
                            block_errors_consecutive = 0
                        else:
                            job_source.errors += 1
                            run.errors += 1
                            run.error_log += f"{url}: {exc}\n"
                            record_source_error(job_source, url, exc)
                            append_source_log(job_source, f"ERROR {url}: {exc}")
                            if is_source_block_error(exc):
                                block_errors_total += 1
                                block_errors_consecutive += 1
                            else:
                                block_errors_consecutive = 0
                    finally:
                        job_source.processed += 1
                        run.discovered = job_source.processed
                        db_write(
                            lambda: job_source.save(
                                update_fields=[
                                    "processed",
                                    "created",
                                    "updated",
                                    "skipped",
                                    "errors",
                                    "error_urls",
                                    "current_url",
                                    "logs",
                                ]
                            )
                        )
                        db_write(
                            lambda: run.save(
                                update_fields=[
                                    "discovered",
                                    "created",
                                    "updated",
                                    "errors",
                                    "error_log",
                                ]
                            )
                        )
                        if not cancelled and not stopped_by_block and block_threshold_reached():
                            stop_for_block()
                        if not cancelled and not stopped_by_block:
                            submit_next()
                        else:
                            for pending_future in list(pending):
                                if pending_future.cancel():
                                    pending.pop(pending_future, None)

        job.refresh_from_db()
        if job.cancel_requested:
            job_source.status = ScrapeJobSource.Status.CANCELLED
        elif stopped_by_block:
            job_source.status = ScrapeJobSource.Status.PARTIAL
        elif (
            job.max_errors_per_source is not None
            and job_source.errors >= job.max_errors_per_source
        ):
            job_source.status = ScrapeJobSource.Status.PARTIAL
            append_source_log(
                job_source,
                f"Detenida por alcanzar {job.max_errors_per_source} errores.",
            )
        elif job_source.errors:
            job_source.status = ScrapeJobSource.Status.PARTIAL
        elif not coverage_complete:
            job_source.status = ScrapeJobSource.Status.PARTIAL
            append_source_log(
                job_source,
                "Discovery incompleto frente al total declarado por el portal; no se marcan ausentes.",
            )
        elif (
            job.max_pages is None
            and job.max_listings is None
            and declared_total
            and not limited_by_max_listings
            and coverage_ratio is not None
            and coverage_ratio < 80
        ):
            job_source.status = ScrapeJobSource.Status.PARTIAL
            append_source_log(
                job_source,
                "Discovery incompleto frente al total declarado por el portal; revisar paginacion o bloqueo.",
            )
        else:
            job_source.status = ScrapeJobSource.Status.SUCCESS

        if (
            geocode_candidate_ids
            and not job.cancel_requested
            and not stopped_by_block
            and job.geocode_limit != 0
        ):
            geocode_source_candidates(job, job_source, geocode_candidate_ids)

        if (
            job.scrape_mode == ScrapeJob.Mode.COMPLETE
            and job.max_listings is None
            and job.mark_missing
            and not job.cancel_requested
            and not stopped_by_block
            and not retry_source_urls
            and job_source.status == ScrapeJobSource.Status.SUCCESS
        ):
            db_write(lambda: _mark_missing_atomic(source, seen))
        elif (
            job.scrape_mode == ScrapeJob.Mode.COMPLETE
            and job.max_listings is None
            and job.mark_missing
            and not job.cancel_requested
            and not stopped_by_block
            and not retry_source_urls
            and job_source.status != ScrapeJobSource.Status.SUCCESS
        ):
            append_source_log(job_source, "No se marcan ausentes porque la fuente no termino con cobertura completa.")
        else:
            append_source_log(job_source, "Modo liviano, prueba o muestra limitada: no se marcan ausentes.")
    except Exception as exc:
        job_source.status = ScrapeJobSource.Status.FAILED
        job_source.errors += 1
        if run is not None:
            run.errors += 1
            run.error_log += str(exc)
        append_source_log(job_source, f"Fallo general: {exc}")
    finally:
        now = timezone.now()
        job_source.finished_at = now
        db_write(lambda: job_source.save(update_fields=["status", "errors", "finished_at", "logs"]))
        if run is not None:
            run.status = ScrapeRun.Status.PARTIAL if run.errors else ScrapeRun.Status.SUCCESS
            if job_source.status == ScrapeJobSource.Status.FAILED:
                run.status = ScrapeRun.Status.FAILED
            run.finished_at = now
            db_write(lambda: run.save())
        close_old_connections()


def geocode_source_candidates(job, job_source, property_ids):
    limit = job.geocode_limit
    ids = list(dict.fromkeys(property_ids))
    if limit is not None:
        ids = ids[:limit]
    properties = list(
        Property.objects.filter(pk__in=ids, location__isnull=True).order_by("-last_seen_at")
    )
    properties = [item for item in properties if has_geocodable_address(item)]
    if not properties:
        return

    job_source.geocode_pending = len(properties)
    db_write(lambda: job_source.save(update_fields=["geocode_pending"]))
    append_source_log(
        job_source,
        f"Geocodificando {len(properties)} propiedades con direccion detectada en modo seguro Nominatim (1 hilo, rate limit global)...",
    )
    geocoder = Geocoder()
    started = monotonic()
    for index, property_obj in enumerate(properties, start=1):
        if job_cancelled(job.pk):
            append_source_log(job_source, "Geocodificacion detenida por cancelacion.")
            break
        try:
            job_source.current_url = f"propiedad #{property_obj.pk}"
            if geocoder.geocode_property(property_obj):
                job_source.geocoded += 1
            else:
                job_source.geocode_failed += 1
        except Exception as exc:
            job_source.geocode_failed += 1
            append_source_log(job_source, f"GEOCODE ERROR propiedad {property_obj.pk}: {exc}")
        finally:
            db_write(
                lambda: job_source.save(
                    update_fields=["geocoded", "geocode_failed", "current_url", "logs"]
                )
            )
            if index == 1 or index == len(properties) or index % 5 == 0:
                append_source_log(
                    job_source,
                    f"Geocodificacion progreso: {index}/{len(properties)} en {monotonic() - started:.1f}s.",
                )
    append_source_log(
        job_source,
        f"Geocodificacion: {job_source.geocoded} ubicadas, {job_source.geocode_failed} sin resultado/error.",
    )


def _mark_missing_atomic(source, seen):
    with transaction.atomic():
        mark_missing(source, seen)


def parse_one_url(slug, max_pages, start_page, request_timeout, url):
    close_old_connections()
    adapter = get_adapter_compatible(
        slug,
        max_pages=max_pages,
        start_page=start_page,
        request_timeout=request_timeout,
    )
    started = monotonic()
    try:
        return url, adapter.parse(url), monotonic() - started
    finally:
        close_old_connections()
