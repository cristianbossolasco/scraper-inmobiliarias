import copy
import json
import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from properties.models import DriveHistory, Listing, ListingImage, Property, PropertyLocation, Source
from properties.services.drive_history import MAX_GROUPS, MAX_PROPERTIES, MAX_PAYLOAD_BYTES, normalize_session, save_session


@override_settings(ROOT_URLCONF="config.mobile_urls", SECURE_SSL_REDIRECT=False)
class DriveHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="driver", password="test-password")
        self.other = get_user_model().objects.create_user(username="other-driver", password="test-password")
        self.client.force_login(self.user)
        self.url = "/api/recorrido/historial/"
        self.now = int(timezone.now().timestamp() * 1000)
        self.payload = {
            "version": 2, "sessionId": str(uuid.uuid4()), "status": "ended",
            "startedAt": self.now - 600000, "endedAt": self.now,
            "filters": {"propertyTypes": ["house"], "radiusM": 350, "bedroomsMin": 3, "priceMax": 120000},
            "points": [
                {"latitude": -34.59, "longitude": -58.64, "accuracy": 12, "timestamp": self.now - 599000},
                {"latitude": -34.591, "longitude": -58.64, "accuracy": 9, "timestamp": self.now - 589000},
            ],
            "distanceM": 9999,
            "encounters": {"-34.590000,-58.640000": {"firstSeenAt": self.now - 500000, "minDistanceM": 30, "propertyIds": [123]}},
            "favoritePropertyIds": [123],
            "details": {"123": {"id": 123, "address_text": "Ejemplo 123", "price_short": "USD 100k", "image_url": "https://example.com/front.jpg", "original_url": "https://example.com/123"}},
        }

    def post(self, payload=None, client=None):
        return (client or self.client).post(self.url, json.dumps(self.payload if payload is None else payload), content_type="application/json")

    def test_completed_trip_roundtrip_and_paged_summary_recomputes_distance(self):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        result = response.json()
        self.assertTrue(result["created"])
        self.assertAlmostEqual(result["distanceM"], 111.2, places=1)
        self.assertEqual(result["pointCount"], 2)
        self.assertEqual(result["encounterCount"], 1)
        self.assertEqual(result["favoriteCount"], 1)
        self.assertEqual(result["startedAt"], self.payload["startedAt"])
        detail = self.client.get(f'{self.url}{result["id"]}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["session"]["points"], self.payload["points"])
        self.assertEqual(detail.json()["session"]["filters"]["bedroomsMin"], 3)
        self.assertEqual(detail.json()["session"]["details"]["123"]["address_text"], "Ejemplo 123")
        listing = self.client.get(self.url).json()
        self.assertEqual(listing["count"], 1)
        self.assertIsNone(listing["next"])
        self.assertNotIn("session", listing["results"][0])
        self.assertNotIn("points", listing["results"][0])
        self.assertIn("no-store", detail["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_retry_is_idempotent_and_cannot_replace_saved_trip(self):
        first = self.post().json()
        changed = copy.deepcopy(self.payload)
        changed["points"] = []
        changed["details"]["123"]["address_text"] = "Changed"
        response = self.post(changed)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        self.assertEqual(response.json()["id"], first["id"])
        self.assertEqual(DriveHistory.objects.count(), 1)
        saved = DriveHistory.objects.get()
        self.assertEqual(saved.point_count, 2)
        self.assertEqual(saved.session["details"]["123"]["address_text"], "Ejemplo 123")

    def test_user_isolation_authentication_and_precise_delete(self):
        first = self.post().json()
        detail_url = f'{self.url}{first["id"]}/'
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).json()["results"], [])
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.assertEqual(self.client.delete(detail_url).status_code, 404)
        other_trip = self.post().json()
        self.assertNotEqual(first["id"], other_trip["id"])
        self.client.force_login(self.user)
        unrelated = Property.objects.create(fingerprint="favorite-kept", title="Keep", is_favorite=True)
        self.assertEqual(self.client.delete(detail_url).status_code, 204)
        self.assertEqual(DriveHistory.objects.filter(deleted_at__isnull=True).count(), 1)
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        unrelated.refresh_from_db()
        self.assertTrue(unrelated.is_favorite)
        anon = Client()
        self.assertEqual(anon.get(self.url).status_code, 401)
        self.assertEqual(anon.get(f'{self.url}{other_trip["id"]}/').status_code, 401)
        self.assertEqual(self.post(client=anon).status_code, 401)

    def test_deleted_trip_cannot_be_resurrected_by_lost_acknowledgement_retry(self):
        original = self.post().json()
        detail_url = f'{self.url}{original["id"]}/'
        self.assertEqual(self.client.delete(detail_url).status_code, 204)
        response = self.post()
        self.assertEqual(response.status_code, 410)
        self.assertTrue(response.json()["deleted"])
        self.assertEqual(response.json()["id"], original["id"])
        self.assertEqual(self.client.get(self.url).json()["count"], 0)
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        tombstone = DriveHistory.objects.get()
        self.assertIsNotNone(tombstone.deleted_at)
        self.assertEqual(tombstone.session, {})
        self.assertEqual(tombstone.filters, {})
        self.assertEqual(tombstone.started_at.timestamp(), 0)
        self.assertEqual(tombstone.ended_at.timestamp(), 0)
        self.assertEqual(tombstone.created_at.timestamp(), 0)
        self.assertEqual((tombstone.distance_m, tombstone.point_count, tombstone.encounter_count, tombstone.favorite_count), (0, 0, 0, 0))
        self.client.force_login(self.other)
        self.assertEqual(self.post().status_code, 201)

    def test_csrf_required_for_upload_and_delete(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(self.post(client=strict).status_code, 403)
        trip = self.post().json()
        self.assertEqual(strict.delete(f'{self.url}{trip["id"]}/').status_code, 403)
        self.assertTrue(DriveHistory.objects.exists())

    def test_bounded_and_malformed_payloads_do_not_write(self):
        changes = [
            {"status": "tracking"}, {"sessionId": ""}, {"sessionId": "x" * 101},
            {"startedAt": True}, {"startedAt": float("nan")}, {"endedAt": self.now - 700000},
            {"endedAt": self.now + 3600000}, {"startedAt": self.now - 8 * 86400000},
            {"points": [self.payload["points"][0]] * 1501}, {"points": "bad"},
            {"favoritePropertyIds": [True]}, {"favoritePropertyIds": [1] * (MAX_PROPERTIES + 1)},
            {"encounters": {str(i): {} for i in range(MAX_GROUPS + 1)}},
            {"filters": {"propertyTypes": ["castle"]}}, {"filters": {"radiusM": 100}},
            {"filters": {"priceMin": 300, "priceMax": 100}},
            {"details": {"__proto__": {}}}, {"distanceM": float("inf")},
        ]
        for changeset in changes:
            payload = {**self.payload, **changeset}
            with self.subTest(changeset=str(changeset)[:150]):
                self.assertEqual(self.post(payload).status_code, 400)
        malformed = [[], None, "text", {"sessionId": "x"}, {**self.payload, "points": [None]}]
        for payload in malformed:
            self.assertEqual(self.client.post(self.url, json.dumps(payload), content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(self.url, '{"points":', content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(self.url, b'\xff', content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(self.url, "x" * (MAX_PAYLOAD_BYTES + 1), content_type="application/json").status_code, 413)
        self.assertFalse(DriveHistory.objects.exists())

    def test_bad_coordinates_order_and_teleport_rejected(self):
        mutations = [("latitude", 91), ("longitude", -181), ("accuracy", -1),
                     ("timestamp", self.now - 700000), ("latitude", -20)]
        for field, value in mutations:
            payload = copy.deepcopy(self.payload)
            payload["points"][1][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(self.post(payload).status_code, 400)
        payload = copy.deepcopy(self.payload)
        payload["points"] = list(reversed(payload["points"]))
        self.assertEqual(self.post(payload).status_code, 400)

    def test_snapshot_urls_are_sanitized_and_private_fields_discarded(self):
        self.payload["details"]["123"].update({
            "image_url": "javascript:alert(1)", "original_url": "https://user:password@example.com/home",
            "personal_notes": "private", "is_favorite": True, "id": 999,
        })
        self.payload["details"]["999"] = {"address_text": "Not referenced"}
        result = self.post().json()
        snapshot = self.client.get(f'{self.url}{result["id"]}/').json()["session"]["details"]
        self.assertEqual(snapshot["123"]["image_url"], "")
        self.assertEqual(snapshot["123"]["original_url"], "")
        self.assertEqual(snapshot["123"]["id"], 123)
        self.assertNotIn("personal_notes", snapshot["123"])
        self.assertNotIn("is_favorite", snapshot["123"])
        self.assertNotIn("999", snapshot)

    def test_missing_encounter_snapshots_enriched_in_one_query_and_survive_property_deletion(self):
        source = Source.objects.create(name="Test", slug="history-test", base_url="https://example.com")
        properties = []
        for i in range(4):
            prop = Property.objects.create(fingerprint=f"hist-{i}", title="Casa", address=f"Calle {i}", currency="USD", price=100000, property_type="house")
            PropertyLocation.objects.create(property=prop, latitude=-34.59, longitude=-58.64)
            listing = Listing.objects.create(property=prop, source=source, external_id=str(i), url=f"https://example.com/{i}")
            ListingImage.objects.create(listing=listing, url=f"https://example.com/{i}.jpg")
            properties.append(prop)
        self.payload["encounters"] = {"place": {"firstSeenAt": self.now - 1000, "minDistanceM": 40, "propertyIds": [p.pk for p in properties]}}
        self.payload["favoritePropertyIds"] = []
        self.payload["details"] = {}
        with CaptureQueriesContext(connection) as queries:
            trip, _created = save_session(self.user, normalize_session(self.payload))
        property_queries = [query for query in queries if 'FROM "properties_property"' in query["sql"]]
        self.assertEqual(len(property_queries), 1)
        prop_id = str(properties[0].pk)
        self.assertEqual(trip.session["details"][prop_id]["address_text"], "Calle 0")
        self.assertEqual(trip.session["details"][prop_id]["original_url"], "https://example.com/0")
        self.assertEqual(trip.session["details"][prop_id]["image_url"], "https://example.com/0.jpg")
        properties[0].delete()
        trip.refresh_from_db()
        self.assertEqual(trip.session["details"][prop_id]["address_text"], "Calle 0")

    def test_legacy_ids_empty_trip_and_pagination(self):
        self.payload.update(sessionId="1778888888888-abcd", points=[], distanceM=0)
        self.assertEqual(self.post().status_code, 201)
        original = DriveHistory.objects.get()
        DriveHistory.objects.bulk_create([
            DriveHistory(owner=self.user, client_session_id=f"batch-{i}", started_at=original.started_at, ended_at=original.ended_at)
            for i in range(22)
        ])
        first = self.client.get(self.url).json()
        self.assertEqual(len(first["results"]), 20)
        second = self.client.get(first["next"]).json()
        self.assertEqual(len(second["results"]), 3)
        ids = [item["id"] for item in first["results"] + second["results"]]
        self.assertEqual(len(set(ids)), 23)
        self.assertIsNone(second["next"])
        self.assertEqual(self.client.get(self.url + "?page=0").status_code, 400)
        self.assertEqual(self.client.get(self.url + "?page=x").status_code, 400)
        self.assertEqual(self.client.get(self.url + "?page=99").json()["results"], [])

    def test_v3_wide_discovery_snapshot_keeps_observed_price_and_coordinates(self):
        source = Source.objects.create(name="Snapshot", slug="snapshot", base_url="https://example.com")
        prop = Property.objects.create(fingerprint="snapshot-price", title="Casa", address="Dirección manual 1500",
            currency="USD", price=200000, property_type="house", manual_overrides={"address": "Dirección manual 1500"})
        PropertyLocation.objects.create(property=prop, latitude=-34.58, longitude=-58.64)
        Listing.objects.create(property=prop, source=source, external_id="snapshot", url="https://example.com/snapshot")
        self.payload.update(version=3, favoritePropertyIds=[], details={str(prop.pk): {
            "id": prop.pk, "price_short": "USD 180k", "latitude": -34.585, "longitude": -58.645, "seen_before": False,
        }}, encounters={"place": {"firstSeenAt": self.now - 500000, "minDistanceM": 1250, "propertyIds": [prop.pk]}})
        self.payload["filters"]["radiusM"] = 1500
        response = self.post()
        self.assertEqual(response.status_code, 201)
        saved = self.client.get(f'{self.url}{response.json()["id"]}/').json()["session"]
        detail = saved["details"][str(prop.pk)]
        self.assertEqual(saved["version"], 3)
        self.assertEqual(saved["filters"]["radiusM"], 1500)
        self.assertEqual(detail["price_short"], "USD 180k")
        self.assertEqual(detail["latitude"], -34.585)
        self.assertFalse(detail["seen_before"])
        self.assertEqual(detail["original_url"], "https://example.com/snapshot")
        self.assertEqual(detail["address_text"], "Dirección manual 1500")
