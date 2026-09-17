import base64
import copy
import io
import json
import uuid

from PIL import Image
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from properties.models import DriveFieldNote, DriveHistory, Property


@override_settings(ROOT_URLCONF="config.mobile_urls", SECURE_SSL_REDIRECT=False)
class DriveNotebookTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("field-driver")
        self.other = get_user_model().objects.create_user("other-field-driver")
        self.client.force_login(self.user)
        self.id = uuid.uuid4()
        self.url = f"/api/recorrido/notas/{self.id}/"
        self.payload = {"revision": 0, "mutationId": str(uuid.uuid4()), "data": {
            "sessionId": "test-trip", "kind": "sign", "title": "Cartel encontrado", "propertyId": None,
            "text": "Consultar por esta casa", "tags": ["liked_block"], "action": "contact", "done": False,
            "latitude": -34.59, "longitude": -58.64, "accuracy": 8, "capturedAt": int(timezone.now().timestamp() * 1000),
        }}

    def put(self, payload=None, client=None):
        return (client or self.client).put(self.url, json.dumps(payload or self.payload), content_type="application/json")

    def test_note_photo_roundtrip_ownership_and_no_property_mutation(self):
        prop = Property.objects.create(fingerprint="field-unmodified", title="Casa", manual_overrides={"address": "Manual"}, is_favorite=True)
        before = Property.objects.filter(pk=prop.pk).values().get()
        photo = io.BytesIO()
        Image.new("RGB", (64, 48), "blue").save(photo, "JPEG")
        self.payload["photoData"] = "data:image/jpeg;base64," + base64.b64encode(photo.getvalue()).decode()
        created = self.put()
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["revision"], 1)
        self.assertNotIn("photoData", created.json())
        image_url = created.json()["photoUrl"]
        self.assertEqual(self.client.get(image_url).status_code, 200)
        self.assertEqual(self.client.get("/api/recorrido/notas/?session=test-trip").json()["results"][0]["data"]["text"], "Consultar por esta casa")
        self.assertEqual(Property.objects.filter(pk=prop.pk).values().get(), before)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(image_url).status_code, 404)
        self.assertEqual(self.client.get("/api/recorrido/notas/").json()["results"], [])
        self.assertEqual(self.put().status_code, 404)
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        self.assertEqual(Client().get(image_url).status_code, 401)
        self.assertEqual(self.put(client=Client()).status_code, 401)

    def test_retry_and_revision_conflict_keep_data_until_explicit_resolution(self):
        self.assertEqual(self.put().status_code, 201)
        self.assertEqual(self.put().status_code, 200)
        self.assertEqual(DriveFieldNote.objects.count(), 1)
        update = copy.deepcopy(self.payload)
        update.update(revision=1, mutationId=str(uuid.uuid4()))
        update["data"]["done"] = True
        self.assertEqual(self.put(update).json()["revision"], 2)
        self.assertEqual(self.put(update).json()["revision"], 2)
        stale = {**update, "mutationId": str(uuid.uuid4())}
        self.assertEqual(self.put(stale).status_code, 409)
        self.assertTrue(DriveFieldNote.objects.get().data["done"])

    def test_delete_erases_note_photo_and_late_retry_does_not_recreate_it(self):
        self.put()
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(self.put().status_code, 410)
        note = DriveFieldNote.objects.get()
        self.assertEqual(note.data, {})
        self.assertIsNone(note.photo)
        self.assertEqual(note.client_session_id, "")

    def test_invalid_note_photo_and_csrf_do_not_write(self):
        for changes in [{"latitude": 91}, {"longitude": float("nan")}, {"tags": ["unknown"]}, {"text": "x" * 2001}, {"done": "yes"}, {"kind": []}, {"action": {}}, {"kind": "property", "propertyId": None}, {"sessionId": "../private"}]:
            payload = copy.deepcopy(self.payload)
            payload["data"].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.put(payload).status_code, 400)
        self.assertEqual(self.put({**self.payload, "photoData": "data:image/jpeg;base64,eHl6"}).status_code, 400)
        self.assertEqual(self.client.put(self.url, "[]", content_type="application/json").status_code, 400)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(self.put(client=strict).status_code, 403)
        self.assertFalse(DriveFieldNote.objects.exists())

    def test_memory_is_owner_scoped_and_excludes_deleted_trips(self):
        now = timezone.now()
        for i, owner in enumerate([self.user, self.user, self.other]):
            DriveHistory.objects.create(owner=owner, client_session_id=str(i), started_at=now, ended_at=now,
                deleted_at=now if i == 1 else None,
                session={"encounters": {"a": {"propertyIds": [i + 1]}}, "points": [
                    {"longitude": -58.64, "latitude": -34.59}, {"longitude": -58.63, "latitude": -34.59}]})
        response = self.client.get("/api/recorrido/memoria/")
        self.assertEqual(response.json()["propertyIds"], [1])
        self.assertEqual(len(response.json()["traces"]), 1)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(Client().get("/api/recorrido/memoria/").status_code, 401)

    def test_deleting_trip_erases_its_notes_but_keeps_other_trips_and_owners(self):
        self.put()
        now = timezone.now()
        trip = DriveHistory.objects.create(owner=self.user, client_session_id="test-trip", started_at=now, ended_at=now)
        other = DriveFieldNote.objects.create(owner=self.other, client_session_id="test-trip", mutation_id=uuid.uuid4(), data={"text": "Keep"})
        self.assertEqual(self.client.delete(f"/api/recorrido/historial/{trip.pk}/").status_code, 204)
        self.assertEqual(DriveFieldNote.objects.get(pk=self.id).data, {})
        self.assertEqual(self.put().status_code, 410)
        other.refresh_from_db()
        self.assertEqual(other.data, {"text": "Keep"})
