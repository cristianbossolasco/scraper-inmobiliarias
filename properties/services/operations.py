import re
import threading
from io import StringIO
from time import monotonic

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from properties.models import OperationJob, OperationJobStep, Property, ScrapeJob, ScrapeJobSource
from properties.services.geocoding import Geocoder, geocodable_address_q, has_geocodable_address
from properties.services.scraping import (
    ActiveScrapeJobError,
    active_scrape_job,
    create_scrape_job,
    run_scrape_job,
    serialize_job as serialize_scrape_job,
)
from properties.services.security_scoring import (
    SECURITY_UPDATE_FIELDS,
    apply_security_score,
    load_security_points,
    load_security_zones,
    score_property_security,
)
from properties.services.location_intelligence import (
    apply_location_intelligence_score,
    load_location_zones,
    location_intelligence_values,
    score_property_location_intelligence,
)
from properties.services.zone_inference import apply_zone_inference, infer_property_zone


OPERATION_THREADS = {}
ACTIVE_OPERATION_STATUSES = {
    OperationJob.Status.PENDING,
    OperationJob.Status.RUNNING,
}
TERMINAL_OPERATION_STATUSES = {
    OperationJob.Status.SUCCESS,
    OperationJob.Status.PARTIAL,
    OperationJob.Status.FAILED,
    OperationJob.Status.CANCELLED,
    OperationJob.Status.INTERRUPTED,
}
TERMINAL_SCRAPE_STATUSES = {
    ScrapeJob.Status.SUCCESS,
    ScrapeJob.Status.PARTIAL,
    ScrapeJob.Status.FAILED,
    ScrapeJob.Status.CANCELLED,
    ScrapeJob.Status.INTERRUPTED,
}
ACTIVE_SCRAPE_SOURCE_STATUSES = {
    ScrapeJobSource.Status.PENDING,
    ScrapeJobSource.Status.DISCOVERING,
    ScrapeJobSource.Status.RUNNING,
}
RISKY_STEP_KINDS = {
    OperationJob.Kind.REPAIR_ADDRESSES,
    OperationJob.Kind.REPAIR_NEIGHBORHOODS,
    OperationJob.Kind.REPAIR_LOCALITIES,
    OperationJob.Kind.REPAIR_AGENCIES,
    OperationJob.Kind.REPAIR_METRICS,
    OperationJob.Kind.REPAIR_MERGED_LISTINGS,
    OperationJob.Kind.MERGE_PROPERTIES,
}
COMMAND_STEP_KINDS = {
    OperationJob.Kind.REPAIR_ADDRESSES,
    OperationJob.Kind.REPAIR_NEIGHBORHOODS,
    OperationJob.Kind.REPAIR_LOCALITIES,
    OperationJob.Kind.REPAIR_AGENCIES,
    OperationJob.Kind.REPAIR_METRICS,
    OperationJob.Kind.REPAIR_MERGED_LISTINGS,
    OperationJob.Kind.MERGE_PROPERTIES,
}


class ActiveOperationJobError(ValueError):
    def __init__(self, active_job_id):
        self.active_job_id = active_job_id
        super().__init__(f"Ya hay una operacion en curso: Job #{active_job_id}.")


def active_operation_job():
    job = OperationJob.objects.prefetch_related("steps").filter(status__in=ACTIVE_OPERATION_STATUSES).order_by(
        "-created_at"
    ).first()
    if not job:
        return None
    reconcile_operation_job(job)
    job.refresh_from_db()
    if job.status not in ACTIVE_OPERATION_STATUSES:
        return None
    return job


def operation_catalog():
    steps = [
        {
            "kind": OperationJob.Kind.SCRAPE,
            "label": "Scraping",
            "risk": "medium",
            "default_mode": OperationJob.Mode.APPLY,
            "supports_dry_run": True,
        },
        {
            "kind": OperationJob.Kind.GEOCODE,
            "label": "Geocodificacion",
            "risk": "medium",
            "default_mode": OperationJob.Mode.APPLY,
            "supports_dry_run": True,
        },
        {
            "kind": OperationJob.Kind.INFER_ZONES,
            "label": "Inferencia de zonas",
            "risk": "low",
            "default_mode": OperationJob.Mode.APPLY,
            "supports_dry_run": True,
        },
        {
            "kind": OperationJob.Kind.SCORE_SECURITY,
            "label": "Scoring de seguridad",
            "risk": "low",
            "default_mode": OperationJob.Mode.APPLY,
            "supports_dry_run": True,
        },
        {
            "kind": OperationJob.Kind.SCORE_LOCATION_INTELLIGENCE,
            "label": "Scoring territorial",
            "risk": "low",
            "default_mode": OperationJob.Mode.APPLY,
            "supports_dry_run": True,
        },
        *[
            {
                "kind": kind,
                "label": OperationJob.Kind(kind).label,
                "risk": "high",
                "default_mode": OperationJob.Mode.DRY_RUN,
                "supports_dry_run": True,
                "requires_dry_run_before_apply": True,
            }
            for kind in sorted(RISKY_STEP_KINDS)
        ],
    ]
    presets = [
        {
            "key": "safe_scrape",
            "label": "Scrape liviano seguro",
            "steps": [OperationJob.Kind.SCRAPE],
        },
        {
            "key": "complete_location",
            "label": "Completar ubicacion",
            "steps": [
                OperationJob.Kind.GEOCODE,
                OperationJob.Kind.INFER_ZONES,
                OperationJob.Kind.SCORE_SECURITY,
                OperationJob.Kind.SCORE_LOCATION_INTELLIGENCE,
            ],
        },
        {
            "key": "quality_audit",
            "label": "Auditar calidad",
            "steps": [
                OperationJob.Kind.REPAIR_ADDRESSES,
                OperationJob.Kind.REPAIR_NEIGHBORHOODS,
                OperationJob.Kind.REPAIR_LOCALITIES,
                OperationJob.Kind.REPAIR_AGENCIES,
                OperationJob.Kind.REPAIR_METRICS,
                OperationJob.Kind.REPAIR_MERGED_LISTINGS,
            ],
        },
        {
            "key": "full_maintenance",
            "label": "Mantenimiento completo",
            "steps": [
                OperationJob.Kind.SCRAPE,
                OperationJob.Kind.GEOCODE,
                OperationJob.Kind.INFER_ZONES,
                OperationJob.Kind.SCORE_SECURITY,
                OperationJob.Kind.SCORE_LOCATION_INTELLIGENCE,
                OperationJob.Kind.REPAIR_ADDRESSES,
                OperationJob.Kind.REPAIR_NEIGHBORHOODS,
                OperationJob.Kind.REPAIR_LOCALITIES,
                OperationJob.Kind.REPAIR_AGENCIES,
            ],
        },
    ]
    return {"steps": steps, "presets": presets}


def create_operation_job(
    *,
    kind=OperationJob.Kind.PIPELINE,
    mode=OperationJob.Mode.DRY_RUN,
    steps=None,
    scope=None,
    params=None,
    title="",
    enforce_single_active=False,
    source_job=None,
    allow_risky_apply=False,
):
    if kind not in OperationJob.Kind.values:
        raise ValueError("Tipo de operacion invalido.")
    if mode not in OperationJob.Mode.values:
        raise ValueError("Modo de operacion invalido.")
    steps = steps or [{"kind": kind, "mode": mode, "params": params or {}}]
    cleaned_steps = []
    for index, item in enumerate(steps, start=1):
        step_kind = item.get("kind")
        step_mode = item.get("mode") or mode
        if step_kind not in OperationJob.Kind.values:
            raise ValueError(f"Step invalido: {step_kind}")
        if step_kind == OperationJob.Kind.PIPELINE:
            raise ValueError("Un step no puede ser pipeline.")
        if step_mode not in OperationJob.Mode.values:
            raise ValueError(f"Modo invalido para {step_kind}.")
        if (
            step_kind in RISKY_STEP_KINDS
            and step_mode == OperationJob.Mode.APPLY
            and not allow_risky_apply
        ):
            raise ValueError(
                "Las reparaciones y merges requieren correr primero una simulacion."
            )
        cleaned_steps.append(
            {
                "order": index,
                "kind": step_kind,
                "mode": step_mode,
                "params": item.get("params") or {},
            }
        )

    with transaction.atomic():
        if enforce_single_active:
            active = active_operation_job()
            if active:
                raise ActiveOperationJobError(active.pk)
        job = OperationJob.objects.create(
            kind=kind,
            title=title or _default_job_title(kind, cleaned_steps),
            mode=mode,
            scope=scope or {},
            params=params or {},
            total_steps=len(cleaned_steps),
            source_job=source_job,
        )
        for item in cleaned_steps:
            OperationJobStep.objects.create(job=job, **item)
    return job


def retry_operation_job(original_job, enforce_single_active=False):
    steps = [
        {"kind": step.kind, "mode": step.mode, "params": step.params}
        for step in original_job.steps.all()
    ]
    return create_operation_job(
        kind=original_job.kind,
        mode=original_job.mode,
        steps=steps,
        scope=original_job.scope,
        params=original_job.params,
        title=original_job.title,
        enforce_single_active=enforce_single_active,
        source_job=original_job.source_job,
        allow_risky_apply=bool(original_job.source_job_id),
    )


def create_apply_from_dry_run_job(original_job, enforce_single_active=False):
    if original_job.mode != OperationJob.Mode.DRY_RUN:
        raise ValueError("Solo se puede aplicar desde una simulacion.")
    if original_job.status not in {
        OperationJob.Status.SUCCESS,
        OperationJob.Status.PARTIAL,
    }:
        raise ValueError("La simulacion debe estar finalizada antes de aplicar.")
    steps = [
        {
            "kind": step.kind,
            "mode": OperationJob.Mode.APPLY,
            "params": step.params,
        }
        for step in original_job.steps.all()
    ]
    return create_operation_job(
        kind=original_job.kind,
        mode=OperationJob.Mode.APPLY,
        steps=steps,
        scope=original_job.scope,
        params=original_job.params,
        title=f"Aplicar {original_job.title or original_job.get_kind_display()}",
        enforce_single_active=enforce_single_active,
        source_job=original_job,
        allow_risky_apply=True,
    )


def start_operation_job(job):
    thread = threading.Thread(target=run_operation_job, args=(job.pk,), daemon=True)
    OPERATION_THREADS[job.pk] = thread
    thread.start()
    return thread


def cancel_operation_job(job):
    job.cancel_requested = True
    job.save(update_fields=["cancel_requested"])
    for step in job.steps.all():
        scrape_job_id = (step.result_summary or {}).get("scrape_job_id")
        if scrape_job_id:
            ScrapeJob.objects.filter(
                pk=scrape_job_id,
            ).exclude(
                status__in={
                    ScrapeJob.Status.SUCCESS,
                    ScrapeJob.Status.FAILED,
                    ScrapeJob.Status.CANCELLED,
                }
            ).update(cancel_requested=True)
    return job


def mark_stale_operation_jobs():
    live_ids = {job_id for job_id, thread in OPERATION_THREADS.items() if thread.is_alive()}
    stale = (
        OperationJob.objects.filter(status=OperationJob.Status.RUNNING)
        .exclude(pk__in=live_ids)
    )
    now = timezone.now()
    for job in stale:
        if reconcile_operation_job(job):
            job.refresh_from_db()
            if job.status not in ACTIVE_OPERATION_STATUSES:
                continue
        job.status = OperationJob.Status.INTERRUPTED
        job.finished_at = job.finished_at or now
        job.logs += "Servidor reiniciado o thread no disponible.\n"
        job.save(update_fields=["status", "finished_at", "logs"])
        job.steps.filter(
            status__in={OperationJob.Status.PENDING, OperationJob.Status.RUNNING}
        ).update(status=OperationJob.Status.INTERRUPTED, finished_at=now)


def run_operation_job(job_id):
    close_old_connections()
    job = OperationJob.objects.prefetch_related("steps").get(pk=job_id)
    job.status = OperationJob.Status.RUNNING
    job.started_at = timezone.now()
    job.logs = _append_log_text(job.logs, "Operacion iniciada.")
    job.save(update_fields=["status", "started_at", "logs"])

    try:
        for step in job.steps.all().order_by("order"):
            if operation_cancelled(job.pk):
                _cancel_pending_step(step)
                continue
            run_operation_step(step.pk)
            _refresh_job_counters(job)
    except Exception as exc:
        job.status = OperationJob.Status.FAILED
        job.errors += 1
        job.logs = _append_log_text(job.logs, f"Fallo general: {exc}")
    finally:
        job.refresh_from_db()
        if job.status != OperationJob.Status.FAILED:
            statuses = list(job.steps.values_list("status", flat=True))
            if job.cancel_requested or any(
                status == OperationJob.Status.CANCELLED for status in statuses
            ):
                job.status = OperationJob.Status.CANCELLED
            elif any(status == OperationJob.Status.FAILED for status in statuses):
                job.status = OperationJob.Status.FAILED
            elif any(
                status in {OperationJob.Status.PARTIAL, OperationJob.Status.INTERRUPTED}
                for status in statuses
            ):
                job.status = OperationJob.Status.PARTIAL
            else:
                job.status = OperationJob.Status.SUCCESS
        job.finished_at = timezone.now()
        _refresh_job_counters(job, save=False, refresh=False)
        job.logs = _append_log_text(job.logs, f"Operacion finalizada: {job.get_status_display()}.")
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "completed_steps",
                "processed",
                "changed",
                "errors",
                "logs",
                "result_summary",
            ]
        )
        OPERATION_THREADS.pop(job_id, None)
        close_old_connections()


def run_operation_step(step_id):
    close_old_connections()
    step = OperationJobStep.objects.select_related("job").get(pk=step_id)
    step.status = OperationJob.Status.RUNNING
    step.started_at = timezone.now()
    step.logs = _append_log_text(step.logs, f"Step {step.get_kind_display()} iniciado.")
    step.save(update_fields=["status", "started_at", "logs"])
    started = monotonic()
    try:
        handler = STEP_HANDLERS.get(step.kind)
        if handler is None:
            raise ValueError(f"Step sin handler: {step.kind}")
        handler(step)
        step.refresh_from_db()
        if step.status == OperationJob.Status.RUNNING:
            step.status = (
                OperationJob.Status.PARTIAL
                if step.errors
                else OperationJob.Status.SUCCESS
            )
    except Exception as exc:
        step.status = OperationJob.Status.FAILED
        step.errors += 1
        step.error_log = _append_log_text(step.error_log, str(exc))
        step.logs = _append_log_text(step.logs, f"ERROR: {exc}")
    finally:
        step.finished_at = timezone.now()
        summary = dict(step.result_summary or {})
        summary["elapsed_seconds"] = round(monotonic() - started, 1)
        step.result_summary = summary
        step.logs = _append_log_text(step.logs, f"Step finalizado: {step.get_status_display()}.")
        step.save(
            update_fields=[
                "status",
                "finished_at",
                "processed",
                "changed",
                "skipped",
                "errors",
                "logs",
                "error_log",
                "result_summary",
            ]
        )
        close_old_connections()


def serialize_operation_job(job):
    job.refresh_from_db()
    steps = []
    for step in job.steps.all().order_by("order"):
        steps.append(
            {
                "id": step.pk,
                "order": step.order,
                "kind": step.kind,
                "kind_label": step.get_kind_display(),
                "mode": step.mode,
                "mode_label": step.get_mode_display(),
                "status": step.status,
                "status_label": step.get_status_display(),
                "params": step.params,
                "total": step.total,
                "processed": step.processed,
                "changed": step.changed,
                "skipped": step.skipped,
                "errors": step.errors,
                "logs": step.logs,
                "error_log": step.error_log,
                "result_summary": step.result_summary,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                "elapsed_seconds": elapsed_seconds(step.started_at, step.finished_at),
            }
        )
    return {
        "id": job.pk,
        "kind": job.kind,
        "kind_label": job.get_kind_display(),
        "title": job.title,
        "status": job.status,
        "status_label": job.get_status_display(),
        "mode": job.mode,
        "mode_label": job.get_mode_display(),
        "scope": job.scope,
        "params": job.params,
        "result_summary": job.result_summary,
        "total_steps": job.total_steps,
        "completed_steps": job.completed_steps,
        "processed": job.processed,
        "changed": job.changed,
        "errors": job.errors,
        "logs": job.logs,
        "cancel_requested": job.cancel_requested,
        "source_job_id": job.source_job_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_seconds": elapsed_seconds(job.started_at, job.finished_at),
        "steps": steps,
        "can_apply": job.mode == OperationJob.Mode.DRY_RUN
        and job.status in {OperationJob.Status.SUCCESS, OperationJob.Status.PARTIAL},
    }


def elapsed_seconds(started_at, finished_at=None):
    if not started_at:
        return 0
    return max(round(((finished_at or timezone.now()) - started_at).total_seconds()), 0)


def operation_cancelled(job_id):
    return OperationJob.objects.filter(pk=job_id, cancel_requested=True).exists()


def reconcile_operation_job(job):
    changed = False
    for step in job.steps.all():
        if step.kind != OperationJob.Kind.SCRAPE or step.status in TERMINAL_OPERATION_STATUSES:
            continue
        scrape_job_id = (step.result_summary or {}).get("scrape_job_id")
        if not scrape_job_id:
            continue
        scrape_job = (
            ScrapeJob.objects.prefetch_related("sources")
            .filter(pk=scrape_job_id)
            .first()
        )
        if not scrape_job or scrape_job.status not in TERMINAL_SCRAPE_STATUSES:
            continue
        if scrape_job.sources.filter(status__in=ACTIVE_SCRAPE_SOURCE_STATUSES).exists():
            continue

        payload = serialize_scrape_job(scrape_job)
        step.processed = sum(source["processed"] for source in payload["sources"])
        step.changed = sum(
            source["created"] + source["updated"] for source in payload["sources"]
        )
        step.errors = sum(source["errors"] for source in payload["sources"])
        step.result_summary = {
            "scrape_job_id": scrape_job.pk,
            "scrape_status": payload["status"],
            "sources": payload["sources"],
        }
        if job.cancel_requested or scrape_job.status == ScrapeJob.Status.CANCELLED:
            step.status = OperationJob.Status.CANCELLED
        elif scrape_job.status == ScrapeJob.Status.FAILED:
            step.status = OperationJob.Status.FAILED
        elif scrape_job.status in {ScrapeJob.Status.PARTIAL, ScrapeJob.Status.INTERRUPTED}:
            step.status = OperationJob.Status.PARTIAL
        else:
            step.status = OperationJob.Status.SUCCESS
        step.finished_at = scrape_job.finished_at or timezone.now()
        step.logs = _append_log_text(
            step.logs,
            f"Step reconciliado desde ScrapeJob #{scrape_job.pk}: {scrape_job.get_status_display()}.",
        )
        step.save(
            update_fields=[
                "processed",
                "changed",
                "errors",
                "result_summary",
                "status",
                "finished_at",
                "logs",
            ]
        )
        changed = True

    if changed:
        _refresh_job_counters(job)
        _finalize_operation_if_complete(job)
    return changed


def _finalize_operation_if_complete(job):
    job.refresh_from_db()
    steps = list(job.steps.all())
    if not steps or any(step.status not in TERMINAL_OPERATION_STATUSES for step in steps):
        return False
    statuses = [step.status for step in steps]
    if job.cancel_requested or OperationJob.Status.CANCELLED in statuses:
        job.status = OperationJob.Status.CANCELLED
    elif OperationJob.Status.FAILED in statuses:
        job.status = OperationJob.Status.FAILED
    elif any(
        status in {OperationJob.Status.PARTIAL, OperationJob.Status.INTERRUPTED}
        for status in statuses
    ):
        job.status = OperationJob.Status.PARTIAL
    else:
        job.status = OperationJob.Status.SUCCESS
    job.finished_at = job.finished_at or timezone.now()
    _refresh_job_counters(job, save=False, refresh=False)
    job.logs = _append_log_text(job.logs, f"Operacion finalizada: {job.get_status_display()}.")
    job.save(
        update_fields=[
            "status",
            "finished_at",
            "completed_steps",
            "processed",
            "changed",
            "errors",
            "logs",
            "result_summary",
        ]
    )
    return True


def _default_job_title(kind, steps):
    if kind != OperationJob.Kind.PIPELINE:
        return OperationJob.Kind(kind).label
    labels = [OperationJob.Kind(step["kind"]).label for step in steps[:3]]
    suffix = "" if len(steps) <= 3 else f" +{len(steps) - 3}"
    return ", ".join(labels) + suffix


def _append_log_text(current, message):
    timestamp = timezone.localtime().strftime("%H:%M:%S")
    return (current + f"[{timestamp}] {message}\n")[-12000:]


def _save_step(step, fields=None):
    default_fields = [
        "total",
        "processed",
        "changed",
        "skipped",
        "errors",
        "logs",
        "error_log",
        "result_summary",
    ]
    step.save(update_fields=fields or default_fields)


def _refresh_job_counters(job, save=True, refresh=True):
    if refresh:
        job.refresh_from_db()
    steps = list(job.steps.all())
    job.completed_steps = sum(
        1
        for step in steps
        if step.status
        in {
            OperationJob.Status.SUCCESS,
            OperationJob.Status.PARTIAL,
            OperationJob.Status.FAILED,
            OperationJob.Status.CANCELLED,
            OperationJob.Status.INTERRUPTED,
        }
    )
    job.processed = sum(step.processed for step in steps)
    job.changed = sum(step.changed for step in steps)
    job.errors = sum(step.errors for step in steps)
    job.result_summary = {
        "steps": len(steps),
        "processed": job.processed,
        "changed": job.changed,
        "errors": job.errors,
    }
    if save:
        job.save(
            update_fields=[
                "completed_steps",
                "processed",
                "changed",
                "errors",
                "result_summary",
            ]
        )


def _cancel_pending_step(step):
    step.status = OperationJob.Status.CANCELLED
    step.finished_at = timezone.now()
    step.logs = _append_log_text(step.logs, "Cancelado antes de iniciar.")
    step.save(update_fields=["status", "finished_at", "logs"])


def _params_list(params, *keys):
    for key in keys:
        value = params.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, list):
            return [item for item in value if item not in (None, "")]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [value]
    return []


def _optional_int(value, default=None):
    if value in (None, ""):
        return default
    return int(value)


def _optional_float(value, default=None):
    if value in (None, ""):
        return default
    return float(value)


def _bool_param(params, key, default=False):
    value = params.get(key, default)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "si", "on"}
    return bool(value)


def _limit_items(items, limit):
    if limit in (None, 0):
        return list(items)
    return list(items[:limit])


def _property_ids(params):
    return [int(value) for value in _params_list(params, "property_ids", "property_id")]


def _base_property_queryset(params, require_location=False):
    queryset = Property.objects.all().order_by("pk")
    if require_location:
        queryset = queryset.select_related("location").filter(location__isnull=False)
    else:
        queryset = queryset.select_related("location")
    source = params.get("source")
    if source:
        queryset = queryset.filter(listings__source__slug=source)
    ids = _property_ids(params)
    if ids:
        queryset = queryset.filter(pk__in=ids)
    return queryset.distinct()


def _run_scrape_step(step):
    params = step.params or {}
    sources = _params_list(params, "sources", "source")
    if not sources:
        raise ValueError("Seleccione al menos una fuente.")
    workers = params.get("workers") or {slug: 1 for slug in sources}
    step.total = len(sources)
    step.logs = _append_log_text(
        step.logs,
        "Scrape liviano: geocoding y marcado de ausentes quedan desactivados salvo parametria explicita.",
    )
    _save_step(step)
    if step.mode == OperationJob.Mode.DRY_RUN:
        step.processed = len(sources)
        step.result_summary = {
            "planned_sources": sources,
            "dry_run": True,
            "note": "No se hicieron requests en simulacion.",
        }
        _save_step(step)
        return
    if active_scrape_job():
        active = active_scrape_job()
        raise ActiveScrapeJobError(active.pk)
    scrape_job = create_scrape_job(
        sources,
        workers,
        max_pages=_optional_int(params.get("max_pages")),
        start_page=_optional_int(params.get("start_page")),
        max_listings=_optional_int(params.get("max_listings")),
        geocode_limit=_optional_int(params.get("geocode_limit"), 0),
        mark_missing=_bool_param(params, "mark_missing", False),
        scrape_mode=params.get("scrape_mode") or ScrapeJob.Mode.COMPLETE,
        request_timeout_seconds=_optional_int(params.get("request_timeout_seconds")),
        max_errors_per_source=_optional_int(params.get("max_errors_per_source")),
        runner=ScrapeJob.Runner.WEB,
        phases=params.get("phases"),
        reprocess_mode=params.get("reprocess_mode"),
        reprocess_stale_days=_optional_int(params.get("reprocess_stale_days"), 30),
        from_latest_discovery=_bool_param(params, "from_latest_discovery", False),
        enforce_single_active=True,
    )
    step.result_summary = {"scrape_job_id": scrape_job.pk}
    _save_step(step)
    run_scrape_job(scrape_job.pk)
    scrape_job.refresh_from_db()
    payload = serialize_scrape_job(scrape_job)
    step.processed = sum(source["processed"] for source in payload["sources"])
    step.changed = sum(source["created"] + source["updated"] for source in payload["sources"])
    step.errors = sum(source["errors"] for source in payload["sources"])
    step.result_summary = {
        "scrape_job_id": scrape_job.pk,
        "scrape_status": payload["status"],
        "sources": payload["sources"],
    }
    if payload["status"] == ScrapeJob.Status.FAILED:
        step.status = OperationJob.Status.FAILED
    elif payload["status"] in {
        ScrapeJob.Status.PARTIAL,
        ScrapeJob.Status.CANCELLED,
        ScrapeJob.Status.INTERRUPTED,
    }:
        step.status = OperationJob.Status.PARTIAL
    _save_step(step, fields=["processed", "changed", "errors", "result_summary", "status"])


def _run_geocode_step(step):
    params = step.params or {}
    limit = _optional_int(params.get("limit"), 25)
    force = _bool_param(params, "force")
    only_with_address = _bool_param(params, "only_with_address", True)
    cache_only = _bool_param(params, "cache_only", True)
    queryset = _base_property_queryset(params)
    if not force:
        queryset = queryset.filter(location__isnull=True)
    if only_with_address:
        queryset = queryset.filter(geocodable_address_q())
    properties = _limit_items(queryset.distinct().order_by("-last_seen_at"), limit)
    step.total = len(properties)
    step.logs = _append_log_text(
        step.logs,
        f"Candidatas: {len(properties)}; cache_only={cache_only}; force={force}.",
    )
    _save_step(step)
    if step.mode == OperationJob.Mode.DRY_RUN:
        step.processed = len(properties)
        step.result_summary = {"candidates": len(properties), "dry_run": True}
        _save_step(step)
        return

    geocoder = Geocoder()
    located = 0
    no_result = 0
    for property_obj in properties:
        if operation_cancelled(step.job_id):
            step.status = OperationJob.Status.CANCELLED
            break
        step.processed += 1
        try:
            if cache_only:
                location = geocoder.geocode_property_from_cache(property_obj, force=force)
            else:
                location = geocoder.geocode_property(property_obj, force=force)
            if location:
                located += 1
                step.changed += 1
            else:
                no_result += 1
                step.skipped += 1
        except Exception as exc:
            step.errors += 1
            step.error_log = _append_log_text(step.error_log, f"{property_obj.pk}: {exc}")
        if step.processed == step.total or step.processed % 10 == 0:
            step.logs = _append_log_text(
                step.logs,
                f"Geocoding: {step.processed}/{step.total}, ubicadas={located}, sin_resultado={no_result}.",
            )
            _save_step(step)
    step.result_summary = {
        "located": located,
        "no_result": no_result,
        "cache_only": cache_only,
    }
    _save_step(step)


def _run_zone_step(step):
    params = step.params or {}
    limit = _optional_int(params.get("limit"), 0)
    queryset = _base_property_queryset(params)
    properties = _limit_items(queryset.order_by("pk"), limit)
    step.total = len(properties)
    geocode_missing = _bool_param(params, "geocode_missing") and step.mode == OperationJob.Mode.APPLY
    geocoder = None if step.mode == OperationJob.Mode.APPLY else DryRunGeocoder()
    _save_step(step)
    stats = {"inferred": 0, "conflicts": 0, "needs_review": 0, "changed": 0}
    for property_obj in properties:
        if operation_cancelled(step.job_id):
            step.status = OperationJob.Status.CANCELLED
            break
        step.processed += 1
        try:
            result = infer_property_zone(
                property_obj,
                geojson_path=params.get("geojson") or None,
                max_distance_m=_optional_float(params.get("max_distance_m")),
                geocode_missing=geocode_missing,
                geocoder=geocoder,
            )
            if result.inferred_neighborhood:
                stats["inferred"] += 1
            if result.zone_conflict:
                stats["conflicts"] += 1
            if result.needs_review:
                stats["needs_review"] += 1
            if _zone_result_differs(property_obj, result):
                stats["changed"] += 1
                step.changed += 1
            if step.mode == OperationJob.Mode.APPLY:
                apply_zone_inference(property_obj, result)
        except Exception as exc:
            step.errors += 1
            step.error_log = _append_log_text(step.error_log, f"{property_obj.pk}: {exc}")
        if step.processed == step.total or step.processed % 25 == 0:
            _save_step(step)
    step.result_summary = stats
    _save_step(step)


def _zone_result_differs(property_obj, result):
    return any(
        [
            property_obj.inferred_neighborhood != result.inferred_neighborhood,
            property_obj.inferred_neighborhood_method != result.method,
            property_obj.inferred_neighborhood_distance_m != result.distance_m,
            property_obj.zone_conflict != result.zone_conflict,
            property_obj.zone_needs_review != result.needs_review,
        ]
    )


class DryRunGeocoder:
    def __init__(self):
        self.delegate = Geocoder()

    def build_query(self, property_obj):
        return self.delegate.build_query(property_obj)

    def geocode_property_from_cache(self, property_obj, force=False):
        return None

    def geocode_property(self, property_obj, force=False):
        return None


def _run_security_step(step):
    params = step.params or {}
    limit = _optional_int(params.get("limit"), 0)
    queryset = _base_property_queryset(params, require_location=True)
    if _bool_param(params, "only_missing"):
        queryset = queryset.filter(security_coverage_score__isnull=True)
    properties = _limit_items(queryset.order_by("pk"), limit)
    zones_dataset = load_security_zones(params.get("geojson") or None)
    points_dataset = load_security_points(params.get("points_geojson") or None)
    if not zones_dataset["configured"]:
        raise ValueError("No se encontraron zonas de seguridad GeoJSON.")
    step.total = len(properties)
    _save_step(step)
    matched = 0
    changed = 0
    now = timezone.now()
    for property_obj in properties:
        if operation_cancelled(step.job_id):
            step.status = OperationJob.Status.CANCELLED
            break
        step.processed += 1
        try:
            score = score_property_security(
                property_obj,
                zones=zones_dataset["features"],
                points=points_dataset["features"],
            )
            if score.matched:
                matched += 1
            old_values = _security_values(property_obj)
            apply_security_score(property_obj, score, commit=False)
            property_obj.security_scored_at = now
            if old_values != _security_values(property_obj):
                changed += 1
                step.changed += 1
            if step.mode == OperationJob.Mode.APPLY:
                property_obj.save(update_fields=SECURITY_UPDATE_FIELDS)
        except Exception as exc:
            step.errors += 1
            step.error_log = _append_log_text(step.error_log, f"{property_obj.pk}: {exc}")
        if step.processed == step.total or step.processed % 25 == 0:
            _save_step(step)
    step.result_summary = {"matched": matched, "changed": changed}
    _save_step(step)


def _security_values(property_obj):
    return {
        "coverage": property_obj.security_coverage_score,
        "risk": property_obj.security_risk_score,
        "level": property_obj.security_level,
        "zone": property_obj.security_zone_label,
        "source": property_obj.security_source,
    }


def _run_location_intelligence_step(step):
    params = step.params or {}
    limit = _optional_int(params.get("limit"), 0)
    queryset = _base_property_queryset(params)
    if _bool_param(params, "only_missing"):
        queryset = queryset.filter(location_intelligence__isnull=True)
    if not _bool_param(params, "force"):
        queryset = queryset.filter(
            Q(location__isnull=False)
            | Q(inferred_neighborhood__gt="")
            | Q(detected_neighborhood__gt="")
            | Q(neighborhood__gt="")
        )
    properties = _limit_items(
        queryset.select_related("location_intelligence").order_by("pk"),
        limit,
    )
    dataset = load_location_zones(params.get("geojson") or None)
    if not dataset["configured"]:
        raise ValueError("No se encontro GeoJSON integrado de inteligencia territorial.")
    step.total = len(properties)
    _save_step(step)
    matched = 0
    changed = 0
    for property_obj in properties:
        if operation_cancelled(step.job_id):
            step.status = OperationJob.Status.CANCELLED
            break
        step.processed += 1
        try:
            score = score_property_location_intelligence(
                property_obj,
                zones=dataset["features"],
                source_signature=dataset["signature"],
            )
            if score.matched:
                matched += 1
            old_values = location_intelligence_values(
                getattr(property_obj, "location_intelligence", None)
            )
            record = apply_location_intelligence_score(property_obj, score, commit=False)
            if old_values != location_intelligence_values(record):
                changed += 1
                step.changed += 1
            if step.mode == OperationJob.Mode.APPLY:
                apply_location_intelligence_score(property_obj, score, commit=True)
        except Exception as exc:
            step.errors += 1
            step.error_log = _append_log_text(step.error_log, f"{property_obj.pk}: {exc}")
        if step.processed == step.total or step.processed % 25 == 0:
            _save_step(step)
    step.result_summary = {"matched": matched, "changed": changed}
    _save_step(step)


def _run_command_step(step):
    command, options = _command_options(step.kind, step.params or {}, step.mode)
    stdout = StringIO()
    stderr = StringIO()
    try:
        call_command(command, stdout=stdout, stderr=stderr, **options)
    except CommandError as exc:
        output = stdout.getvalue()
        error_output = stderr.getvalue()
        step.logs = _append_log_text(step.logs, output.strip() or str(exc))
        step.error_log = _append_log_text(step.error_log, error_output.strip() or str(exc))
        step.errors += 1
        step.status = OperationJob.Status.FAILED
        _save_step(step, fields=["logs", "error_log", "errors", "status"])
        return
    output = stdout.getvalue()
    error_output = stderr.getvalue()
    if output.strip():
        step.logs = _append_log_text(step.logs, output.strip()[-8000:])
    if error_output.strip():
        step.error_log = _append_log_text(step.error_log, error_output.strip()[-4000:])
    step.changed = _extract_changed_count(output)
    step.processed = step.changed
    step.result_summary = {
        "command": command,
        "options": _public_options(options),
        "output_tail": output.strip()[-2000:],
    }
    _save_step(step)


def _command_options(kind, params, mode):
    dry_run = mode == OperationJob.Mode.DRY_RUN
    property_ids = _property_ids(params)
    sources = _params_list(params, "sources", "source")
    if kind == OperationJob.Kind.REPAIR_ADDRESSES:
        return "repair_addresses", {
            "source": sources[0] if sources else None,
            "property_id": property_ids,
            "dry_run": dry_run,
            "geocode": _bool_param(params, "geocode") and not dry_run,
        }
    if kind == OperationJob.Kind.REPAIR_NEIGHBORHOODS:
        return "repair_neighborhoods", {
            "source": sources[0] if sources else None,
            "property_id": property_ids,
            "dry_run": dry_run,
        }
    if kind == OperationJob.Kind.REPAIR_LOCALITIES:
        if len(property_ids) > 1:
            raise ValueError("repair_localities acepta un solo property_id por corrida.")
        return "repair_localities", {
            "property_id": property_ids[0] if property_ids else None,
            "dry_run": dry_run,
        }
    if kind == OperationJob.Kind.REPAIR_AGENCIES:
        return "repair_agencies", {"dry_run": dry_run}
    if kind == OperationJob.Kind.REPAIR_METRICS:
        if not sources:
            raise ValueError("repair_metrics requiere al menos una fuente.")
        return "repair_metrics", {
            "source": sources,
            "dry_run": dry_run,
            "max_listings": _optional_int(params.get("max_listings")),
            "property_id": property_ids,
            "timeout": _optional_int(params.get("timeout"), 20),
            "crawl_delay": _optional_float(params.get("crawl_delay")),
            "mark_non_sale": _bool_param(params, "mark_non_sale"),
            "mark_listing_pages": _bool_param(params, "mark_listing_pages"),
            "classify_only": _bool_param(params, "classify_only"),
        }
    if kind == OperationJob.Kind.REPAIR_MERGED_LISTINGS:
        return "repair_merged_listings", {
            "source": sources,
            "property_id": property_ids,
            "dry_run": dry_run,
            "audit_only": _bool_param(params, "audit_only") and dry_run,
            "max_properties": _optional_int(params.get("max_properties")),
            "max_listings_per_property": _optional_int(
                params.get("max_listings_per_property")
            ),
            "timeout": _optional_int(params.get("timeout"), 20),
        }
    if kind == OperationJob.Kind.MERGE_PROPERTIES:
        return "merge_properties", {
            "pair": _params_list(params, "pair", "pairs"),
            "component": _params_list(params, "component", "components"),
            "detect_url_tail_sources": ",".join(
                _params_list(params, "detect_url_tail_sources")
            ),
            "dry_run": dry_run,
        }
    raise ValueError(f"Step no soportado por comando: {kind}")


def _extract_changed_count(output):
    matches = re.findall(r"(\d+)\s+(?:cambios|propiedades|direcciones|localidades|publicaciones|componentes)", output)
    if not matches:
        return 0
    return int(matches[-1])


def _public_options(options):
    return {key: value for key, value in options.items() if value not in (None, [], "")}


STEP_HANDLERS = {
    OperationJob.Kind.SCRAPE: _run_scrape_step,
    OperationJob.Kind.GEOCODE: _run_geocode_step,
    OperationJob.Kind.INFER_ZONES: _run_zone_step,
    OperationJob.Kind.SCORE_SECURITY: _run_security_step,
    OperationJob.Kind.SCORE_LOCATION_INTELLIGENCE: _run_location_intelligence_step,
    **{kind: _run_command_step for kind in COMMAND_STEP_KINDS},
}
