import json
from datetime import datetime, timezone as datetime_timezone

from django.core.exceptions import RequestDataTooBig
from django.core.paginator import EmptyPage, Paginator
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from properties.drive_views import _no_store, mobile_api_login_required
from properties.models import DriveFieldNote, DriveHistory
from properties.services.drive_history import (
    MAX_PAYLOAD_BYTES,
    DriveHistoryValidationError,
    history_summary,
    normalize_session,
    save_session,
)


@require_http_methods(["GET", "POST"])
@mobile_api_login_required
def drive_history_collection(request):
    if request.method == "GET":
        try:
            page_number = int(request.GET.get("page", "1"))
        except (ValueError, TypeError):
            return _no_store(JsonResponse({"error": "Página inválida."}, status=400))
        if page_number < 1:
            return _no_store(JsonResponse({"error": "Página inválida."}, status=400))
        paginator = Paginator(DriveHistory.objects.filter(owner=request.user, deleted_at__isnull=True).defer("session"), 20)
        try:
            page = paginator.page(page_number)
        except EmptyPage:
            return _no_store(JsonResponse({"results": [], "count": paginator.count, "next": None}))
        return _no_store(JsonResponse({
            "results": [history_summary(trip) for trip in page],
            "count": paginator.count,
            "next": f'{reverse("drive-history")}?page={page.next_page_number()}' if page.has_next() else None,
        }))
    if request.content_type != "application/json":
        return _no_store(JsonResponse({"error": "Se requiere JSON."}, status=415))
    try:
        body = request.body
        if len(body) > MAX_PAYLOAD_BYTES:
            raise RequestDataTooBig
        payload = json.loads(body)
        session = normalize_session(payload)
    except RequestDataTooBig:
        return _no_store(JsonResponse({"error": "El recorrido supera el tamaño permitido."}, status=413))
    except DriveHistoryValidationError as exc:
        return _no_store(JsonResponse({"error": str(exc)}, status=400))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _no_store(JsonResponse({"error": "JSON inválido."}, status=400))
    trip, created = save_session(request.user, session)
    if trip.deleted_at is not None:
        return _no_store(JsonResponse({
            "error": "Este recorrido ya fue eliminado del historial.",
            "id": str(trip.id), "sessionId": trip.client_session_id, "deleted": True,
        }, status=410))
    return _no_store(JsonResponse({**history_summary(trip), "created": created}, status=201 if created else 200))


@require_http_methods(["GET", "DELETE"])
@mobile_api_login_required
def drive_history_detail(request, pk):
    trip = DriveHistory.objects.filter(owner=request.user, pk=pk, deleted_at__isnull=True).first()
    if trip is None:
        return _no_store(JsonResponse({"error": "Recorrido no encontrado."}, status=404))
    if request.method == "DELETE":
        # Keep only an opaque session tombstone so a delayed phone retry cannot
        # recreate a trip that the user explicitly deleted. Remove the location
        # data, snapshots, filters, dates and metrics from the completed trip.
        epoch = datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)
        with transaction.atomic():
            DriveHistory.objects.filter(pk=trip.pk, owner=request.user).update(
                deleted_at=timezone.now(), session={}, filters={},
                started_at=epoch, ended_at=epoch, created_at=epoch,
                distance_m=0, point_count=0, encounter_count=0, favorite_count=0,
            )
            DriveFieldNote.objects.filter(owner=request.user, client_session_id=trip.client_session_id).update(
                deleted_at=timezone.now(), data={}, photo=None, client_session_id="",
            )
        return _no_store(HttpResponse(status=204))
    return _no_store(JsonResponse({**history_summary(trip), "session": trip.session}))
