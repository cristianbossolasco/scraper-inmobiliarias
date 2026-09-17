"""Owner-scoped field notebook and cross-trip memory for the mobile Radar."""

import base64
import binascii
import io
import json
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from django.core.exceptions import RequestDataTooBig
from django.core.paginator import EmptyPage, Paginator
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from properties.drive_views import _no_store, mobile_api_login_required
from properties.models import DriveFieldNote, DriveHistory
from properties.services.drive_history import SESSION_ID_RE, DriveHistoryValidationError, _number


TAGS = {"quiet", "noisy", "liked_block", "poor_front", "wrong_location"}
ACTIONS = {"none", "review", "revisit", "daylight", "contact"}


def _text(value, name, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise DriveHistoryValidationError(f"{name} no es válido.")
    return value.strip()


def _normalize(data):
    if not isinstance(data, dict):
        raise DriveHistoryValidationError("Nota inválida.")
    session_id = data.get("sessionId", "")
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise DriveHistoryValidationError("Recorrido inválido.")
    kind, action = data.get("kind"), data.get("action", "none")
    tags = data.get("tags", [])
    if not isinstance(kind, str) or not isinstance(action, str) or kind not in {"property", "place", "sign"} or action not in ACTIONS:
        raise DriveHistoryValidationError("Tipo de nota o pendiente inválido.")
    if not isinstance(tags, list) or len(tags) > 5 or any(not isinstance(tag, str) or tag not in TAGS for tag in tags):
        raise DriveHistoryValidationError("Observaciones inválidas.")
    if not isinstance(data.get("done", False), bool):
        raise DriveHistoryValidationError("Estado inválido.")
    property_id = data.get("propertyId")
    if property_id is not None:
        property_id = _number(property_id, "Propiedad", 1, 2147483647, integer=True)
    if kind == "property" and property_id is None:
        raise DriveHistoryValidationError("Falta la propiedad de la nota.")
    text = _text(data.get("text", ""), "Texto", 2000)
    if not text and not tags and action == "none" and kind != "sign":
        raise DriveHistoryValidationError("Agregá una observación o un pendiente.")
    return {
        "sessionId": session_id, "kind": kind, "propertyId": property_id,
        "title": _text(data.get("title", ""), "Título", 500), "text": text,
        "latitude": _number(data.get("latitude"), "Latitud", -90, 90),
        "longitude": _number(data.get("longitude"), "Longitud", -180, 180),
        "accuracy": None if data.get("accuracy") is None else _number(data["accuracy"], "Precisión", 0, 10000),
        "capturedAt": _number(data.get("capturedAt"), "Fecha", 1, timezone.now().timestamp() * 1000 + 300000, integer=True),
        "tags": list(dict.fromkeys(tags)), "action": action, "done": data.get("done", False),
    }


def _photo(value):
    if not isinstance(value, str) or not value.startswith("data:image/jpeg;base64,") or len(value) > 450000:
        raise DriveHistoryValidationError("La foto debe ser JPEG y ocupar menos de 330 KB.")
    try:
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format != "JPEG" or original.width * original.height > 4000000:
                    raise ValueError
                photo = ImageOps.exif_transpose(original).convert("RGB")
                photo.thumbnail((1280, 1280))
                result = io.BytesIO()
                # Re-encode without EXIF or other embedded metadata.
                photo.save(result, format="JPEG", quality=75)
                return result.getvalue()
    except (ValueError, OSError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise DriveHistoryValidationError("No se pudo leer la foto.") from None


def _serialize(note):
    return {"id": str(note.id), "revision": note.revision, "data": note.data,
            "photoUrl": f"/api/recorrido/notas/{note.id}/foto/" if note.has_photo else ""}


def _notes():
    from django.db.models import BooleanField, Case, Value, When
    return DriveFieldNote.objects.defer("photo").annotate(has_photo=Case(
        When(photo__isnull=False, then=Value(True)), default=Value(False), output_field=BooleanField(),
    ))


@require_GET
@mobile_api_login_required
def notebook_collection(request):
    queryset = _notes().filter(owner=request.user, deleted_at__isnull=True)
    if request.GET.get("session"):
        queryset = queryset.filter(client_session_id=request.GET["session"])
    try:
        number = int(request.GET.get("page", 1))
        if number < 1:
            raise ValueError
        page = Paginator(queryset, 50).page(number)
    except (ValueError, EmptyPage):
        return _no_store(JsonResponse({"error": "Página inválida."}, status=400))
    return _no_store(JsonResponse({"results": [_serialize(note) for note in page], "nextPage": number + 1 if page.has_next() else None}))


@require_http_methods(["PUT", "DELETE"])
@mobile_api_login_required
def notebook_detail(request, pk):
    note = _notes().filter(pk=pk, owner=request.user).first()
    if note is None and DriveFieldNote.objects.filter(pk=pk).exists():
        return _no_store(JsonResponse({"error": "Nota no encontrada."}, status=404))
    if note and note.deleted_at:
        return _no_store(JsonResponse({"error": "Nota eliminada."}, status=410))
    if request.method == "DELETE":
        if not note:
            return _no_store(JsonResponse({"error": "Nota no encontrada."}, status=404))
        DriveFieldNote.objects.filter(pk=pk, owner=request.user).update(
            deleted_at=timezone.now(), data={}, photo=None, client_session_id="",
        )
        return _no_store(HttpResponse(status=204))
    if request.content_type != "application/json":
        return _no_store(JsonResponse({"error": "Se requiere JSON."}, status=415))
    try:
        if len(request.body) > 500000:
            raise RequestDataTooBig
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise DriveHistoryValidationError("Nota inválida.")
        mutation_id = uuid.UUID(str(payload.get("mutationId", "")))
        revision = _number(payload.get("revision", 0), "Versión", 0, 2147483647, integer=True)
        if note and note.mutation_id == mutation_id:
            return _no_store(JsonResponse(_serialize(note)))
        data = _normalize(payload.get("data"))
        photo = _photo(payload["photoData"]) if payload.get("photoData") else None
        if DriveHistory.objects.filter(owner=request.user, client_session_id=data["sessionId"], deleted_at__isnull=False).exists():
            return _no_store(JsonResponse({"error": "El recorrido fue eliminado."}, status=410))
    except RequestDataTooBig:
        return _no_store(JsonResponse({"error": "La nota supera el tamaño permitido."}, status=413))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, DriveHistoryValidationError) else "Nota inválida."
        return _no_store(JsonResponse({"error": message}, status=400))
    with transaction.atomic():
        if note is None and revision == 0:
            note, created = DriveFieldNote.objects.get_or_create(pk=pk, defaults={
                "owner": request.user, "client_session_id": data["sessionId"], "data": data,
                "photo": photo, "mutation_id": mutation_id,
            })
            if created:
                note.has_photo = photo is not None
                return _no_store(JsonResponse(_serialize(note), status=201))
        if note is None or note.owner_id != request.user.pk or note.revision != revision:
            return _no_store(JsonResponse({"error": "La nota cambió en otro dispositivo. Actualizá el cuaderno antes de editar."}, status=409))
        updates = {"data": data, "revision": revision + 1, "mutation_id": mutation_id, "updated_at": timezone.now()}
        if photo is not None:
            updates["photo"] = photo
        if data["sessionId"] != note.client_session_id:
            return _no_store(JsonResponse({"error": "No se puede cambiar el recorrido de una nota."}, status=400))
        if not DriveFieldNote.objects.filter(pk=pk, owner=request.user, revision=revision, deleted_at__isnull=True).update(**updates):
            return _no_store(JsonResponse({"error": "La nota cambió. Actualizá el cuaderno."}, status=409))
    return _no_store(JsonResponse(_serialize(_notes().get(pk=pk))))


@require_GET
@mobile_api_login_required
def notebook_photo(request, pk):
    note = DriveFieldNote.objects.filter(pk=pk, owner=request.user, deleted_at__isnull=True).only("photo").first()
    if not note or not note.photo:
        return _no_store(HttpResponse(status=404))
    response = HttpResponse(bytes(note.photo), content_type="image/jpeg")
    response["X-Content-Type-Options"] = "nosniff"
    return _no_store(response)


@require_GET
@mobile_api_login_required
def drive_memory(request):
    trips = DriveHistory.objects.filter(owner=request.user, deleted_at__isnull=True)
    ids = set()
    # Project only encounter IDs, never entire snapshots or photos.
    for encounters in trips.values_list("session__encounters", flat=True).iterator(chunk_size=100):
        for entry in (encounters or {}).values():
            ids.update(entry.get("propertyIds", []))
    traces = []
    for points in trips.values_list("session__points", flat=True)[:10]:
        points = points or []
        if len(points) > 1:
            step = max(1, (len(points) + 198) // 199)
            sampled = points[::step]
            if sampled[-1] != points[-1]:
                sampled.append(points[-1])
            traces.append([[point["longitude"], point["latitude"]] for point in sampled])
    return _no_store(JsonResponse({"propertyIds": sorted(ids), "traces": traces, "traceLimit": 10}))
