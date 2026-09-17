import json
import time
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from properties.models import Property
from properties.services.drive_mode import (
    DriveModeValidationError,
    drive_property_card,
    nearby_drive_properties,
)


def _no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def _timed_json_response(payload, service_started_at, *, status=200):
    service_ms = (time.perf_counter() - service_started_at) * 1000
    serialize_started_at = time.perf_counter()
    response = JsonResponse(payload, status=status)
    serialize_ms = (time.perf_counter() - serialize_started_at) * 1000
    response["Server-Timing"] = (
        f"drive;dur={service_ms:.1f};desc=Radar, "
        f"serialize;dur={serialize_ms:.1f};desc=JSON"
    )
    return _no_store(response)


def mobile_api_login_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _no_store(
                JsonResponse({"error": "Autenticacion requerida."}, status=401)
            )
        return view_func(request, *args, **kwargs)

    return wrapped


def mobile_home(request):
    return redirect("drive-mode")


@login_required
@ensure_csrf_cookie
def drive_mode(request):
    map_config = {
        "tile_url": settings.MAP_TILE_URL,
        "attribution": settings.MAP_ATTRIBUTION,
        "bounds": settings.HURLINGHAM_BOUNDS,
        "center": [-58.641, -34.606],
        "zoom": 12,
        "user_id": request.user.pk,
    }
    response = render(
        request,
        "properties/drive.html",
        {
            "map_config": map_config,
            "property_types": Property.Type.choices,
        },
    )
    response["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https:; "
        "font-src 'self' data:; "
        "worker-src 'self' blob:; "
        "child-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    return _no_store(response)


@require_POST
@mobile_api_login_required
def nearby_drive_properties_api(request):
    if len(request.body) > 16384:
        return _no_store(JsonResponse({"error": "Payload demasiado grande."}, status=413))
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _no_store(JsonResponse({"error": "JSON invalido."}, status=400))
    try:
        service_started_at = time.perf_counter()
        result = nearby_drive_properties(payload)
    except DriveModeValidationError as exc:
        return _no_store(JsonResponse({"error": str(exc)}, status=400))
    result["generated_at"] = timezone.now().isoformat()
    return _timed_json_response(result, service_started_at)


@require_GET
@mobile_api_login_required
def drive_property_card_api(request, pk):
    service_started_at = time.perf_counter()
    result = drive_property_card(pk)
    if result is None:
        return _no_store(JsonResponse({"error": "Propiedad no disponible."}, status=404))
    return _timed_json_response(result, service_started_at)


@require_POST
@mobile_api_login_required
def drive_favorite_api(request, pk):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _no_store(JsonResponse({"error": "JSON invalido."}, status=400))
    is_favorite = payload.get("is_favorite") if isinstance(payload, dict) else None
    if not isinstance(is_favorite, bool):
        return _no_store(
            JsonResponse({"error": "is_favorite debe ser booleano."}, status=400)
        )
    property_obj = get_object_or_404(
        Property,
        pk=pk,
        operation="sale",
        status=Property.Status.ACTIVE,
        is_hidden=False,
    )
    property_obj.is_favorite = is_favorite
    property_obj.save(update_fields=["is_favorite"])
    return _no_store(
        JsonResponse(
            {
                "ok": True,
                "id": property_obj.pk,
                "is_favorite": property_obj.is_favorite,
            }
        )
    )


@require_GET
def mobile_health(request):
    return _no_store(JsonResponse({"ok": True, "service": "radar-mobile"}))
