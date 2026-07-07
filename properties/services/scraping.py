import threading
import queue
import random
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import timedelta
from time import monotonic, sleep

from django.conf import settings
from django.db import OperationalError, close_old_connections, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from properties.models import (
    Listing,
    ListingIdentity,
    Property,
    ScrapeJob,
    ScrapeJobListing,
    ScrapeJobSource,
    ScrapeRun,
    Source,
)
from properties.scrapers import get_adapter, get_adapter_classes
from properties.scrapers.parsing import external_id_from_url
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
PIPELINE_PHASES = [
    ScrapeJob.Phase.DISCOVER,
    ScrapeJob.Phase.PROCESS_NEW,
    ScrapeJob.Phase.REPROCESS_EXISTING,
]
DISCOVERY_PROGRESS_EVERY = 10
DISCOVERY_PROGRESS_SECONDS = 5


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
    slugs = [
        adapter.definition.slug
        for adapter in adapters
        if adapter.definition.slug not in BLOCKED_SOURCE_SLUGS
    ]
    last_runs = {}
    for job_source in (
        ScrapeJobSource.objects.select_related("job")
        .filter(slug__in=slugs, started_at__isnull=False)
        .order_by("slug", "-started_at", "-id")
    ):
        if job_source.slug not in last_runs:
            last_runs[job_source.slug] = serialize_job_source(job_source, job=job_source.job, include_logs=False)
    return [
        {
            "slug": adapter.definition.slug,
            "name": adapter.definition.name,
            "enabled": adapter.definition.enabled,
            "crawl_delay": adapter.definition.crawl_delay,
            "notes": adapter.definition.notes,
            "last_run": last_runs.get(adapter.definition.slug),
            "last_runs_by_phase": source_last_runs_by_phase(adapter.definition.slug),
        }
        for adapter in adapters
        if adapter.definition.slug not in BLOCKED_SOURCE_SLUGS
    ]


def normalize_phases(phases, default=None):
    if phases in (None, "", []):
        return list(default if default is not None else PIPELINE_PHASES)
    if isinstance(phases, str):
        phases = [phases]
    valid = set(ScrapeJob.Phase.values)
    cleaned = []
    aliases = {"process-new": ScrapeJob.Phase.PROCESS_NEW, "reprocess-existing": ScrapeJob.Phase.REPROCESS_EXISTING}
    for phase in phases:
        phase = aliases.get(str(phase), str(phase))
        if phase not in valid:
            raise ValueError(f"Fase de scraping invalida: {phase}")
        if phase not in cleaned:
            cleaned.append(phase)
    return cleaned


def job_phases(job):
    # Empty phases means legacy-compatible full scrape: discover + parse every discovered detail.
    return normalize_phases(job.phases, default=PIPELINE_PHASES)


def phases_need_latest_discovery(phases):
    return (
        ScrapeJob.Phase.DISCOVER not in phases
        and any(
            phase in phases
            for phase in (ScrapeJob.Phase.PROCESS_NEW, ScrapeJob.Phase.REPROCESS_EXISTING)
        )
    )


def source_last_runs_by_phase(slug):
    output = {}
    candidates = (
        ScrapeJobSource.objects.select_related("job")
        .filter(slug=slug, finished_at__isnull=False)
        .order_by("-finished_at", "-id")[:80]
    )
    for job_source in candidates:
        phases = job_phases(job_source.job)
        for phase in ScrapeJob.Phase.values:
            if phase in output or phase not in phases:
                continue
            if phase == ScrapeJob.Phase.DISCOVER and not job_source.discovery_finished_at:
                continue
            if phase in {ScrapeJob.Phase.PROCESS_NEW, ScrapeJob.Phase.REPROCESS_EXISTING} and not job_source.processing_finished_at:
                continue
            output[phase] = serialize_job_source(job_source, job=job_source.job, include_logs=False)
        if len(output) == len(ScrapeJob.Phase.values):
            break
    return output


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


def _snapshot_new_out_of_phase(job):
    phases = job_phases(job) if job else []
    return (
        ScrapeJob.Phase.REPROCESS_EXISTING in phases
        and ScrapeJob.Phase.PROCESS_NEW not in phases
    )


def discovery_snapshot_counts(source, job=None):
    counts = {
        "discovery_seen": source.total_discovered or 0,
        "discovery_new": 0,
        "discovery_existing": 0,
        "snapshot_out_of_phase": 0,
    }
    job = job or getattr(source, "job", None)
    should_count_snapshot = (
        source.status == ScrapeJobSource.Status.DISCOVERING
        or (source.total_discovered and source.total_to_process == 0 and source.processed == 0)
    )
    if not should_count_snapshot or not source.pk:
        return counts
    snapshot_counts = source.snapshot_listings.aggregate(
        seen=Count("id"),
        existing=Count("id", filter=Q(listing__isnull=False)),
    )
    snapshot_seen = snapshot_counts["seen"] or 0
    seen = max(snapshot_seen, counts["discovery_seen"])
    existing = snapshot_counts["existing"] or 0
    if not snapshot_seen:
        return counts
    new = max(snapshot_seen - existing, 0)
    out_of_phase = new if _snapshot_new_out_of_phase(job) else 0
    counts.update(
        {
            "discovery_seen": seen,
            "discovery_new": 0 if out_of_phase else new,
            "discovery_existing": existing,
            "snapshot_out_of_phase": out_of_phase,
        }
    )
    return counts


def discovery_reference(source):
    reference = {
        "discovery_reference_total": None,
        "discovery_reference_source": "",
        "discovery_reference_finished_at": None,
    }
    if source.status != ScrapeJobSource.Status.DISCOVERING or not source.pk:
        return reference

    candidates = (
        ScrapeJobSource.objects.select_related("job")
        .filter(
            source_id=source.source_id,
            discovery_finished_at__isnull=False,
            total_discovered__gt=0,
        )
        .exclude(job_id=source.job_id)
    )
    preferred = (
        candidates.filter(
            status=ScrapeJobSource.Status.SUCCESS,
            job__max_pages__isnull=True,
            job__start_page__isnull=True,
            job__max_listings__isnull=True,
        )
        .order_by("-discovery_finished_at", "-id")
        .first()
    )
    fallback = candidates.order_by("-discovery_finished_at", "-id").first()
    selected = preferred or fallback
    if not selected:
        return reference

    reference.update(
        {
            "discovery_reference_total": selected.total_discovered,
            "discovery_reference_source": "last_discovery",
            "discovery_reference_finished_at": selected.discovery_finished_at.isoformat(),
        }
    )
    return reference


def source_loading_snapshot(source, job=None):
    job = job or getattr(source, "job", None)
    if not job or not source.pk:
        return False
    return (
        job.from_latest_discovery
        and phases_need_latest_discovery(job_phases(job))
        and source.status in {ScrapeJobSource.Status.DISCOVERING, ScrapeJobSource.Status.RUNNING}
        and not source.processing_started_at
        and not source.finished_at
    )


def serialize_job_source(source, job=None, include_logs=True):
    job = job or getattr(source, "job", None)
    percent = 0
    if source.total_to_process:
        percent = round((source.processed / source.total_to_process) * 100, 1)
    elif source.total_discovered and source.status in {
        ScrapeJobSource.Status.SUCCESS,
        ScrapeJobSource.Status.PARTIAL,
        ScrapeJobSource.Status.CANCELLED,
        ScrapeJobSource.Status.INTERRUPTED,
    }:
        percent = 100
    payload = {
        "job_id": source.job_id,
        "slug": source.slug,
        "name": source.name,
        "status": source.status,
        "status_label": source.get_status_display(),
        "workers": source.workers,
        "total_discovered": source.total_discovered,
        **discovery_snapshot_counts(source, job),
        **discovery_reference(source),
        "loading_snapshot": source_loading_snapshot(source, job),
        "total_to_process": source.total_to_process,
        "processed": source.processed,
        "created": source.created,
        "updated": source.updated,
        "skipped": source.skipped,
        "errors": source.errors,
        "geocode_pending": source.geocode_pending,
        "geocoded": source.geocoded,
        "geocode_failed": source.geocode_failed,
        "current_url": source.current_url if include_logs else "",
        "error_urls": (source.error_urls or []) if include_logs else [],
        "logs": source.logs if include_logs else "",
        "percent": percent,
        "started_at": source.started_at.isoformat() if source.started_at else None,
        "finished_at": source.finished_at.isoformat() if source.finished_at else None,
        "discovery_started_at": source.discovery_started_at.isoformat() if source.discovery_started_at else None,
        "discovery_finished_at": source.discovery_finished_at.isoformat() if source.discovery_finished_at else None,
        "processing_started_at": source.processing_started_at.isoformat() if source.processing_started_at else None,
        "processing_finished_at": source.processing_finished_at.isoformat() if source.processing_finished_at else None,
        "geocoding_started_at": source.geocoding_started_at.isoformat() if source.geocoding_started_at else None,
        "geocoding_finished_at": source.geocoding_finished_at.isoformat() if source.geocoding_finished_at else None,
        "elapsed_seconds": elapsed_seconds(source.started_at, source.finished_at),
        "discovery_seconds": elapsed_seconds(source.discovery_started_at, source.discovery_finished_at),
        "processing_seconds": elapsed_seconds(source.processing_started_at, source.processing_finished_at),
        "geocoding_seconds": elapsed_seconds(source.geocoding_started_at, source.geocoding_finished_at),
    }
    if job is not None:
        payload.update(
            {
                "job_status": job.status,
                "job_status_label": job.get_status_display(),
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
                "phases": job_phases(job),
                "reprocess_mode": job.reprocess_mode,
                "reprocess_mode_label": job.get_reprocess_mode_display(),
                "reprocess_stale_days": job.reprocess_stale_days,
                "from_latest_discovery": job.from_latest_discovery,
            }
        )
    return payload


def record_source_error(job_source, url, exc):
    entry = {
        "url": url,
        "error": str(exc),
        "timestamp": timezone.localtime().isoformat(),
    }
    errors = list(job_source.error_urls or [])
    errors.append(entry)
    job_source.error_urls = errors[-200:]


def finish_source_phase(job_source, phase, finished_at=None):
    started_field = f"{phase}_started_at"
    finished_field = f"{phase}_finished_at"
    if getattr(job_source, started_field) and not getattr(job_source, finished_field):
        setattr(job_source, finished_field, finished_at or timezone.now())
        return True
    return False


def serialize_job(job):
    job.refresh_from_db()
    sources = [
        serialize_job_source(source, job=job)
        for source in job.sources.select_related("source").order_by("name")
    ]
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
        "phases": job_phases(job),
        "reprocess_mode": job.reprocess_mode,
        "reprocess_mode_label": job.get_reprocess_mode_display(),
        "reprocess_stale_days": job.reprocess_stale_days,
        "from_latest_discovery": job.from_latest_discovery,
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
    phases=None,
    reprocess_mode=None,
    reprocess_stale_days=30,
    from_latest_discovery=False,
    enforce_single_active=False,
):
    if scrape_mode not in ScrapeJob.Mode.values:
        raise ValueError("Modo de scraping invalido.")
    if reprocess_mode is not None and reprocess_mode not in ScrapeJob.ReprocessMode.values:
        raise ValueError("Modo de reproceso invalido.")
    if scrape_mode == ScrapeJob.Mode.TRIAL and max_listings is None and not retry_urls:
        max_listings = 3
    cleaned_phases = normalize_phases(phases, default=[])
    if phases_need_latest_discovery(cleaned_phases):
        from_latest_discovery = True
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
            phases=cleaned_phases,
            reprocess_mode=reprocess_mode or ScrapeJob.ReprocessMode.ALL,
            reprocess_stale_days=reprocess_stale_days or 30,
            from_latest_discovery=from_latest_discovery,
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
        phases=original_job.phases,
        reprocess_mode=original_job.reprocess_mode,
        reprocess_stale_days=original_job.reprocess_stale_days,
        from_latest_discovery=original_job.from_latest_discovery,
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
        phases=PIPELINE_PHASES,
        reprocess_mode=ScrapeJob.ReprocessMode.ALL,
        reprocess_stale_days=original_job.reprocess_stale_days,
        enforce_single_active=enforce_single_active,
    )


def start_scrape_job(job):
    thread = threading.Thread(target=run_scrape_job, args=(job.pk,), daemon=True)
    JOB_THREADS[job.pk] = thread
    thread.start()
    return thread


def job_cancelled(job_id):
    return ScrapeJob.objects.filter(pk=job_id, cancel_requested=True).exists()


def discovery_identity_resolver(adapter):
    return getattr(adapter, "discovery_external_id_from_url", None)


def listing_incomplete(listing):
    property_obj = listing.property
    return not (
        property_obj.price
        and (property_obj.covered_area or property_obj.total_area or property_obj.land_area)
        and (property_obj.address or property_obj.detected_address or property_obj.neighborhood)
    )


def _active_listing_for_external_id(source, external_id):
    if not external_id:
        return None
    return (
        Listing.objects.select_related("property")
        .filter(source=source, external_id=str(external_id), active=True)
        .first()
    )


def _active_listing_for_url(source, url):
    if not url:
        return None
    return (
        Listing.objects.select_related("property")
        .filter(source=source, url=url, active=True)
        .first()
    )


def _active_listing_from_identity_url(source, url):
    if not url:
        return None
    external_ids = list(
        ListingIdentity.objects.filter(source=source, url=url)
        .order_by("-last_seen_at")
        .values_list("external_id", flat=True)[:5]
    )
    for external_id in external_ids:
        listing = _active_listing_for_external_id(source, external_id)
        if listing:
            return listing
    return None


def _canonical_snapshot_identity(source, external_id, url):
    external_id = str(external_id)
    listing = _active_listing_for_external_id(source, external_id)
    if listing:
        return external_id, listing

    listing = _active_listing_for_url(source, url)
    if listing:
        return listing.external_id, listing

    listing = _active_listing_from_identity_url(source, url)
    if listing:
        return listing.external_id, listing

    return external_id, None


def _snapshot_item_status(source, external_id, listing=None):
    if listing is None:
        listing = _active_listing_for_external_id(source, external_id)
    if listing:
        return ScrapeJobListing.Status.EXISTING_PENDING, listing
    return ScrapeJobListing.Status.NEW_PENDING, None


def _save_discovered_snapshot_item(job_source, external_id, url, position, listing=None):
    now = timezone.now()
    status, listing = _snapshot_item_status(job_source.source, external_id, listing=listing)

    def operation():
        ListingIdentity.objects.update_or_create(
            source=job_source.source,
            external_id=external_id,
            defaults={
                "url": url,
                "last_seen_at": now,
                "last_seen_reason": "discovery",
            },
        )
        ScrapeJobListing.objects.update_or_create(
            job_source=job_source,
            external_id=external_id,
            defaults={
                "source": job_source.source,
                "listing": listing,
                "url": url,
                "position": position,
                "status": status,
                "discovered_at": now,
                "processed_at": None,
                "error": "",
            },
        )

    db_write(operation)
    return listing is not None


class DiscoverySnapshotRecorder:
    def __init__(
        self,
        job_source,
        log_progress=False,
        identity_resolver=None,
        progress_label="Discovery parcial",
        new_count_label="nuevas",
    ):
        self.job_source = job_source
        self.log_progress = log_progress
        self.identity_resolver = identity_resolver or external_id_from_url
        self.progress_label = progress_label
        self.new_count_label = new_count_label
        self.seen_external_ids = set()
        self.stats = {"urls": 0, "new": 0, "existing": 0}
        self.last_flush_at = monotonic()

    def record(self, url, external_id=None):
        external_id = external_id or self.identity_resolver(url)
        if not external_id:
            return False
        external_id, listing = _canonical_snapshot_identity(self.job_source.source, external_id, url)
        if external_id in self.seen_external_ids:
            return False
        self.seen_external_ids.add(external_id)
        position = len(self.seen_external_ids)
        is_existing = _save_discovered_snapshot_item(
            job_source=self.job_source,
            external_id=external_id,
            url=url,
            position=position,
            listing=listing,
        )
        self.stats["urls"] += 1
        if is_existing:
            self.stats["existing"] += 1
        else:
            self.stats["new"] += 1
        self.job_source.total_discovered = self.stats["urls"]
        self.job_source.current_url = url
        if self._should_flush():
            self.flush(log_progress=True)
        return True

    def _should_flush(self):
        if not self.log_progress or not self.stats["urls"]:
            return False
        return (
            self.stats["urls"] == 1
            or self.stats["urls"] % DISCOVERY_PROGRESS_EVERY == 0
            or monotonic() - self.last_flush_at >= DISCOVERY_PROGRESS_SECONDS
        )

    def _append_progress_log(self):
        timestamp = timezone.localtime().strftime("%H:%M:%S")
        self.job_source.logs = (
            self.job_source.logs
            + f"[{timestamp}] {self.progress_label}: {self.stats['urls']} URLs "
            + f"({self.stats['new']} {self.new_count_label}, {self.stats['existing']} existentes).\n"
        )[-8000:]

    def flush(self, log_progress=False):
        if not self.stats["urls"]:
            return
        update_fields = ["total_discovered", "current_url"]
        if log_progress:
            self._append_progress_log()
            update_fields.append("logs")
        db_write(lambda: self.job_source.save(update_fields=update_fields))
        self.last_flush_at = monotonic()

    def finish(self):
        self.flush()
        return dict(self.stats)


def _record_discovered_snapshot(
    job_source,
    urls,
    identity_resolver=None,
    log_progress=False,
    progress_label="Discovery parcial",
    new_count_label="nuevas",
):
    recorder = DiscoverySnapshotRecorder(
        job_source,
        identity_resolver=identity_resolver,
        log_progress=log_progress,
        progress_label=progress_label,
        new_count_label=new_count_label,
    )
    for item in urls:
        external_id = None
        url = item
        if isinstance(item, (list, tuple)):
            external_id, url = item
        recorder.record(url, external_id=external_id)
    return recorder.finish()


def _copy_latest_discovery(job_source, identity_resolver=None, log_progress=False):
    latest = (
        ScrapeJobSource.objects.select_related("job", "source")
        .filter(
            source=job_source.source,
            discovery_finished_at__isnull=False,
            status=ScrapeJobSource.Status.SUCCESS,
            job__max_pages__isnull=True,
            job__start_page__isnull=True,
            job__max_listings__isnull=True,
        )
        .exclude(pk=job_source.pk)
        .order_by("-discovery_finished_at", "-id")
        .first()
    )
    if not latest:
        raise ValueError(f"No hay discovery completo previo para {job_source.slug}.")
    snapshot_items = list(
        latest.snapshot_listings.order_by("position").values_list("external_id", "url")
    )
    if not snapshot_items and latest.total_discovered:
        raise ValueError(f"El ultimo discovery de {job_source.slug} no tiene snapshot persistido.")
    stats = _record_discovered_snapshot(
        job_source,
        snapshot_items,
        identity_resolver=identity_resolver,
        log_progress=log_progress,
        progress_label="Snapshot local",
        new_count_label="fuera de fase" if _snapshot_new_out_of_phase(job_source.job) else "nuevas",
    )
    append_source_log(job_source, f"Snapshot copiado desde ScrapeJob #{latest.job_id}: {stats['urls']} URLs.")
    return stats


def _reprocess_queryset(job, job_source):
    queryset = job_source.snapshot_listings.select_related("listing__property").filter(
        listing__isnull=False,
        listing__active=True,
        status__in=[
            ScrapeJobListing.Status.EXISTING_PENDING,
            ScrapeJobListing.Status.SKIPPED_EXISTING,
            ScrapeJobListing.Status.ERROR,
        ],
    )
    if job.max_listings is not None:
        queryset = queryset.filter(position__lte=job.max_listings)
    if job.reprocess_mode == ScrapeJob.ReprocessMode.ALL:
        return list(queryset.order_by("position", "id"))
    if job.reprocess_mode == ScrapeJob.ReprocessMode.STALE:
        cutoff = timezone.now() - timedelta(days=job.reprocess_stale_days or 30)
        return list(queryset.filter(listing__last_seen_at__lt=cutoff).order_by("position", "id"))
    return [item for item in queryset.order_by("position", "id") if listing_incomplete(item.listing)]


def _planned_total(job, job_source):
    phases = job_phases(job)
    if ScrapeJob.Phase.DISCOVER in phases and not any(
        phase in phases for phase in (ScrapeJob.Phase.PROCESS_NEW, ScrapeJob.Phase.REPROCESS_EXISTING)
    ):
        return 0
    total = 0
    if ScrapeJob.Phase.PROCESS_NEW in phases:
        process_new_queryset = job_source.snapshot_listings
        if job.max_listings is not None:
            process_new_queryset = process_new_queryset.filter(position__lte=job.max_listings)
        total += process_new_queryset.filter(
            Q(status=ScrapeJobListing.Status.NEW_PENDING)
            | Q(status=ScrapeJobListing.Status.ERROR, listing__isnull=True)
        ).count()
        if ScrapeJob.Phase.REPROCESS_EXISTING not in phases:
            total += process_new_queryset.filter(
                status=ScrapeJobListing.Status.EXISTING_PENDING,
                listing__isnull=False,
            ).count()
    if ScrapeJob.Phase.REPROCESS_EXISTING in phases:
        total += len(_reprocess_queryset(job, job_source))
    return total


def _snapshot_summary_text(job, job_source, snapshot_stats, loading_snapshot):
    snapshot_label = (
        f"{job_source.total_discovered} URLs del snapshot"
        if loading_snapshot
        else f"{job_source.total_discovered} descubiertas"
    )
    if _snapshot_new_out_of_phase(job):
        return (
            f"{snapshot_label}: {snapshot_stats['existing']} existentes, "
            f"{snapshot_stats['new']} fuera de fase; "
            f"{job_source.total_to_process} acciones planificadas."
        )
    return (
        f"{snapshot_label}: {snapshot_stats['new']} nuevas y {snapshot_stats['existing']} existentes; "
        f"{job_source.total_to_process} acciones planificadas."
    )


def _snapshot_external_ids(job_source):
    return list(
        job_source.snapshot_listings.order_by()
        .values_list("external_id", flat=True)
        .distinct()
    )


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
    phases = job_phases(job)
    ran_process_new = False
    coverage_complete = True
    declared_total = None
    limited_by_max_listings = False
    retry_source_urls = []
    loading_snapshot = False

    try:
        if slug in BLOCKED_SOURCE_SLUGS:
            job_source.status = ScrapeJobSource.Status.FAILED
            job_source.errors += 1
            append_source_log(job_source, f"Fuente bloqueada permanentemente: {slug}.")
            db_write(lambda: job_source.save(update_fields=["status", "errors"]))
            return
        phase_started_at = timezone.now()
        retry_source_urls = list(dict.fromkeys((job.retry_urls or {}).get(slug, [])))
        loading_snapshot = (
            not retry_source_urls
            and phases_need_latest_discovery(phases)
            and job.from_latest_discovery
        )
        job_source.status = ScrapeJobSource.Status.RUNNING if loading_snapshot else ScrapeJobSource.Status.DISCOVERING
        job_source.started_at = phase_started_at
        update_fields = ["status", "started_at"]
        if not loading_snapshot:
            job_source.discovery_started_at = phase_started_at
            update_fields.append("discovery_started_at")
        db_write(lambda: job_source.save(update_fields=update_fields))
        if loading_snapshot:
            append_source_log(job_source, "Cargando snapshot previo...")
        else:
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
        identity_resolver = discovery_identity_resolver(adapter)
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
        if retry_source_urls:
            snapshot_stats = _record_discovered_snapshot(
                job_source,
                retry_source_urls,
                identity_resolver=identity_resolver,
            )
            adapter.discovery_stats = {"retry_urls": True, "urls_discovered": snapshot_stats["urls"]}
            append_source_log(job_source, f"Reproceso selectivo: {snapshot_stats['urls']} URLs con error.")
        elif ScrapeJob.Phase.DISCOVER in phases:
            recorder = DiscoverySnapshotRecorder(
                job_source,
                log_progress=True,
                identity_resolver=identity_resolver,
                new_count_label="fuera de fase" if _snapshot_new_out_of_phase(job) else "nuevas",
            )
            try:
                for discovered_url in adapter.discover():
                    if job_cancelled(job_id):
                        break
                    recorder.record(discovered_url)
            except Exception as exc:
                snapshot_stats = recorder.finish()
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
            snapshot_stats = recorder.finish()
        elif job.from_latest_discovery:
            snapshot_stats = _copy_latest_discovery(
                job_source,
                identity_resolver=identity_resolver,
                log_progress=loading_snapshot,
            )
        else:
            raise ValueError("Para procesar sin discovery use --from-latest-discovery.")

        discovery_stats = getattr(adapter, "discovery_stats", {}) or {}
        if job_cancelled(job_id) or discovery_stats.get("cancelled"):
            job_source.status = ScrapeJobSource.Status.CANCELLED
            append_source_log(
                job_source,
                "Cancelacion solicitada durante carga de snapshot."
                if loading_snapshot
                else "Cancelacion solicitada durante discovery.",
            )
            return

        job_source.total_discovered = snapshot_stats["urls"]
        declared_total = discovery_stats.get("declared_total")
        coverage_ratio = discovery_stats.get("coverage_ratio")
        coverage_complete = discovery_stats.get("coverage_complete", True)
        limited_by_max_listings = discovery_stats.get("limited_by_max_listings")
        limited_by_max_pages = discovery_stats.get("limited_by_max_pages")
        if not loading_snapshot:
            finish_source_phase(job_source, "discovery")
        job_source.total_to_process = _planned_total(job, job_source)
        should_process = any(
            phase in phases for phase in (ScrapeJob.Phase.PROCESS_NEW, ScrapeJob.Phase.REPROCESS_EXISTING)
        )
        job_source.status = ScrapeJobSource.Status.RUNNING if should_process and job_source.total_to_process else ScrapeJobSource.Status.SUCCESS
        if should_process and job_source.total_to_process:
            job_source.processing_started_at = timezone.now()
        save_fields = [
            "total_discovered",
            "total_to_process",
            "status",
            "processing_started_at",
        ]
        if not loading_snapshot:
            save_fields.append("discovery_finished_at")
        db_write(lambda: job_source.save(update_fields=save_fields))

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
            if (
                discovery_stats.get("broad_declared_total")
                and job_source.total_discovered < declared_total
            ):
                append_source_log(
                    job_source,
                    f"Portal declara {declared_total}, API entrego {job_source.total_discovered} fichas materializables.",
                )
        elif limited_by_max_listings or limited_by_max_pages:
            append_source_log(
                job_source,
                f"Tanda limitada: discovery acotado a {discovery_stats.get('pages_seen', '?')} paginas y {job_source.total_discovered} fichas descubiertas.",
            )
        append_source_log(
            job_source,
            _snapshot_summary_text(job, job_source, snapshot_stats, loading_snapshot),
        )

        def process_items(items, phase_label):
            nonlocal stopped_by_block
            items = list(items)
            if not items:
                return
            append_source_log(job_source, f"{phase_label}: {len(items)} URLs.")
            with ThreadPoolExecutor(max_workers=job_source.workers) as executor:
                queue_items = iter(items)
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
                    append_source_log(job_source, "Fuente detenida automaticamente por bloqueo 403/CDN. No se marcan ausentes.")
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
                    if job.max_errors_per_source is not None and job_source.errors >= job.max_errors_per_source:
                        return False
                    try:
                        item = next(queue_items)
                    except StopIteration:
                        return False
                    future = executor.submit(
                        parse_one_url,
                        slug,
                        job.max_pages,
                        job.start_page,
                        job.request_timeout_seconds,
                        item.url,
                    )
                    pending[future] = item
                    return True

                for _ in range(job_source.workers):
                    if not submit_next():
                        break

                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        item = pending.pop(future)
                        if future.cancelled():
                            append_source_log(job_source, f"Cancelada antes de iniciar: {item.url}")
                            continue
                        url = item.url
                        cancelled = job_cancelled(job_id)
                        if cancelled:
                            mark_cancelling()
                        try:
                            url, data, elapsed = future.result()
                            job_source.current_url = url
                            if data is None:
                                item.status = ScrapeJobListing.Status.SKIPPED_EXISTING
                                item.processed_at = timezone.now()
                                item.error = ""
                                job_source.skipped += 1
                                append_source_log(job_source, f"Omitida: {url}")
                            else:
                                if item.listing_id and data.get("external_id") != item.external_id:
                                    parsed_external_id = data.get("external_id")
                                    data = dict(data)
                                    raw_data = dict(data.get("raw_data") or {})
                                    raw_data.setdefault("parsed_external_id", parsed_external_id)
                                    raw_data["canonical_snapshot_external_id"] = item.external_id
                                    data["external_id"] = item.external_id
                                    data["raw_data"] = raw_data
                                listing, created = db_write(lambda: ingest_listing(source, data))
                                item.status = ScrapeJobListing.Status.PROCESSED
                                item.listing = listing
                                item.processed_at = timezone.now()
                                item.error = ""
                                seen.append(listing.external_id)
                                if has_geocodable_address(listing.property) and not hasattr(listing.property, "location"):
                                    geocode_candidate_ids.add(listing.property_id)
                                if created:
                                    job_source.created += 1
                                    run.created += 1
                                else:
                                    job_source.updated += 1
                                    run.updated += 1
                                append_source_log(job_source, f"OK {url} en {elapsed:.1f}s")
                                block_errors_consecutive = 0
                        except Exception as exc:
                            item.processed_at = timezone.now()
                            if is_listing_gone_error(exc):
                                job_source.current_url = url
                                item.status = ScrapeJobListing.Status.GONE
                                item.error = ""
                                job_source.skipped += 1
                                removed = db_write(
                                    lambda: mark_listing_removed(
                                        source,
                                        url=getattr(exc, "url", None) or url,
                                        external_id=getattr(exc, "external_id", None) or item.external_id,
                                    )
                                )
                                if removed:
                                    item.listing = removed
                                suffix = "listing dado de baja" if removed else "no existia en DB"
                                append_source_log(job_source, f"Retirada: URL no disponible, {suffix}: {url}")
                                block_errors_consecutive = 0
                            else:
                                item.status = ScrapeJobListing.Status.ERROR
                                item.error = str(exc)
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
                            db_write(
                                lambda item=item: item.save(
                                    update_fields=["status", "listing", "processed_at", "error"]
                                )
                            )
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
                                    update_fields=["discovered", "created", "updated", "errors", "error_log"]
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

        if ScrapeJob.Phase.PROCESS_NEW in phases:
            ran_process_new = True
            new_items = job_source.snapshot_listings.filter(
                Q(status=ScrapeJobListing.Status.NEW_PENDING)
                | Q(status=ScrapeJobListing.Status.ERROR, listing__isnull=True)
            ).order_by("position", "id")
            if job.max_listings is not None:
                new_items = new_items.filter(position__lte=job.max_listings)
            process_items(new_items, "Procesando nuevas")
            if ScrapeJob.Phase.REPROCESS_EXISTING not in phases:
                existing_queryset = job_source.snapshot_listings.filter(
                    status=ScrapeJobListing.Status.EXISTING_PENDING,
                    listing__isnull=False,
                )
                if job.max_listings is not None:
                    existing_queryset = existing_queryset.filter(position__lte=job.max_listings)
                existing_items = list(existing_queryset.order_by("position", "id"))
                now = timezone.now()
                for item in existing_items:
                    item.status = ScrapeJobListing.Status.SKIPPED_EXISTING
                    item.processed_at = now
                    item.error = ""
                if existing_items:
                    db_write(lambda: ScrapeJobListing.objects.bulk_update(existing_items, ["status", "processed_at", "error"]))
                    job_source.processed += len(existing_items)
                    job_source.skipped += len(existing_items)
                    run.discovered = job_source.processed
                    append_source_log(job_source, f"Existentes omitidas por fase: {len(existing_items)}.")
                    db_write(lambda: job_source.save(update_fields=["processed", "skipped", "logs"]))
                    db_write(lambda: run.save(update_fields=["discovered"]))

        if ScrapeJob.Phase.REPROCESS_EXISTING in phases:
            process_items(_reprocess_queryset(job, job_source), "Reprocesando existentes")

        if finish_source_phase(job_source, "processing"):
            db_write(lambda: job_source.save(update_fields=["processing_finished_at"]))

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
            and ran_process_new
            and job_source.status == ScrapeJobSource.Status.SUCCESS
        ):
            db_write(lambda: _mark_missing_atomic(source, _snapshot_external_ids(job_source)))
        elif (
            job.scrape_mode == ScrapeJob.Mode.COMPLETE
            and job.max_listings is None
            and job.mark_missing
            and not job.cancel_requested
            and not stopped_by_block
            and not retry_source_urls
            and ran_process_new
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
        phase_fields = []
        for phase in ("discovery", "processing", "geocoding"):
            if finish_source_phase(job_source, phase, now):
                phase_fields.append(f"{phase}_finished_at")
        db_write(
            lambda: job_source.save(
                update_fields=[
                    "status",
                    "errors",
                    "finished_at",
                    "logs",
                    *phase_fields,
                ]
            )
        )
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
    job_source.geocoding_started_at = timezone.now()
    db_write(lambda: job_source.save(update_fields=["geocode_pending", "geocoding_started_at"]))
    append_source_log(
        job_source,
        f"Geocodificando {len(properties)} propiedades con direccion detectada en modo seguro Nominatim (1 hilo, rate limit global)...",
    )
    geocoder = Geocoder()
    started = monotonic()
    try:
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
    finally:
        finish_source_phase(job_source, "geocoding")
        db_write(lambda: job_source.save(update_fields=["geocoding_finished_at", "logs"]))


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
