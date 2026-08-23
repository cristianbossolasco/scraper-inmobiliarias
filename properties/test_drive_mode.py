import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from properties.models import Listing, Property, PropertyLocation, Source
from properties.services.drive_mode import (
    DriveModeValidationError,
    nearby_drive_properties,
    parse_drive_query,
)


class DriveModeServiceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            slug="drive-test",
            name="Drive Test",
            base_url="https://example.com",
        )

    def create_property(
        self,
        fingerprint,
        *,
        latitude=-34.59,
        longitude=-58.64,
        precision=PropertyLocation.Precision.EXACT,
        status=Property.Status.ACTIVE,
        hidden=False,
        active_listing=True,
        location_confidence=Property.LocationConfidence.HIGH,
        price=120000,
        provider="source-map",
        manually_corrected=False,
    ):
        property_obj = Property.objects.create(
            fingerprint=fingerprint,
            title=f"Casa {fingerprint}",
            property_type=Property.Type.HOUSE,
            operation="sale",
            status=status,
            currency="USD",
            price=price,
            bedrooms=3,
            covered_area=140,
            location_confidence=location_confidence,
            is_hidden=hidden,
        )
        PropertyLocation.objects.create(
            property=property_obj,
            latitude=latitude,
            longitude=longitude,
            precision=precision,
            provider=provider,
            confidence=0.9,
            manually_corrected=manually_corrected,
        )
        Listing.objects.create(
            source=self.source,
            property=property_obj,
            external_id=fingerprint,
            url=f"https://example.com/{fingerprint}",
            active=active_listing,
        )
        return property_obj

    def test_parse_drive_query_validates_bounds_radius_types_and_prices(self):
        parsed = parse_drive_query(
            {
                "latitude": -34.59,
                "longitude": -58.64,
                "radius_m": 350,
                "property_types": ["house"],
                "price_min": 90000,
                "price_max": 180000,
            }
        )
        self.assertEqual(parsed["radius_m"], 350)
        self.assertEqual(parsed["property_types"], ["house"])

        invalid_payloads = [
            {"latitude": 91, "longitude": -58.64},
            {"latitude": -34.59, "longitude": 181},
            {"latitude": -34.59, "longitude": -58.64, "radius_m": 100},
            {
                "latitude": -34.59,
                "longitude": -58.64,
                "property_types": ["castle"],
            },
            {
                "latitude": -34.59,
                "longitude": -58.64,
                "price_min": 200000,
                "price_max": 100000,
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(DriveModeValidationError):
                parse_drive_query(payload)

    def test_nearby_properties_filters_and_orders_eligible_results(self):
        farther = self.create_property(
            "farther",
            latitude=-34.591,
            longitude=-58.64,
            price=150000,
        )
        closer = self.create_property(
            "closer",
            latitude=-34.5902,
            longitude=-58.64,
            price=110000,
        )
        self.create_property("hidden", hidden=True)
        self.create_property("street", precision=PropertyLocation.Precision.STREET)
        self.create_property("inactive-listing", active_listing=False)
        self.create_property("invalid-price", price=1)
        self.create_property(
            "low-confidence",
            location_confidence=Property.LocationConfidence.LOW,
        )

        payload = nearby_drive_properties(
            {
                "latitude": -34.59,
                "longitude": -58.64,
                "radius_m": 350,
                "property_types": ["house"],
            }
        )

        ids = [item["id"] for item in payload["properties"]]
        self.assertEqual(ids, [closer.pk, farther.pk])
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["properties"][0]["price_short"], "USD 110k")
        self.assertEqual(payload["properties"][0]["location_reliability"], "published")
        self.assertLessEqual(
            payload["properties"][0]["distance_m"],
            payload["properties"][1]["distance_m"],
        )

    def test_nearby_properties_groups_shared_coordinates(self):
        first = self.create_property("shared-1", price=140000)
        second = self.create_property("shared-2", price=100000)

        payload = nearby_drive_properties(
            {"latitude": -34.59, "longitude": -58.64, "radius_m": 350}
        )

        items = {item["id"]: item for item in payload["properties"]}
        self.assertEqual(items[first.pk]["group_count"], 2)
        self.assertEqual(items[second.pk]["group_count"], 2)
        self.assertEqual(items[first.pk]["group_price_short"], "USD 100k")
        self.assertFalse(items[first.pk]["group_suspicious"])

    def test_manual_location_is_confirmed_even_without_property_confidence(self):
        property_obj = self.create_property(
            "manual",
            precision=PropertyLocation.Precision.MANUAL,
            provider="manual",
            manually_corrected=True,
            location_confidence=Property.LocationConfidence.UNKNOWN,
        )

        payload = nearby_drive_properties(
            {"latitude": -34.59, "longitude": -58.64, "radius_m": 350}
        )

        item = next(item for item in payload["properties"] if item["id"] == property_obj.pk)
        self.assertEqual(item["location_reliability"], "confirmed")


class CreateMobileUserCommandTests(TestCase):
    @patch(
        "properties.management.commands.create_mobile_user.getpass",
        side_effect=["MobileUser-9x!72Qp#", "MobileUser-9x!72Qp#"],
    )
    def test_command_creates_non_privileged_user(self, _getpass):
        output = StringIO()

        call_command(
            "create_mobile_user",
            username="mobile-command-user",
            stdout=output,
        )

        user = get_user_model().objects.get(username="mobile-command-user")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password("MobileUser-9x!72Qp#"))
        self.assertIn("creado sin permisos administrativos", output.getvalue())


@override_settings(
    ROOT_URLCONF="config.mobile_urls",
    LOGIN_URL="/accounts/login/",
    LOGIN_REDIRECT_URL="/recorrido/",
)
class MobileHostViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="mobile-user",
            password="a-long-test-password",
            is_staff=False,
            is_superuser=False,
        )
        self.source = Source.objects.create(
            slug="mobile-view-test",
            name="Mobile View Test",
            base_url="https://example.com",
        )
        self.property = Property.objects.create(
            fingerprint="mobile-view-property",
            title="Casa móvil",
            property_type=Property.Type.HOUSE,
            operation="sale",
            status=Property.Status.ACTIVE,
            currency="USD",
            price=125000,
            bedrooms=2,
            covered_area=100,
            location_confidence=Property.LocationConfidence.HIGH,
            manual_overrides={"address": "preserve"},
            data_manually_corrected_at=timezone.now() - timedelta(days=1),
            personal_notes="No modificar",
        )
        self.location = PropertyLocation.objects.create(
            property=self.property,
            latitude=-34.59,
            longitude=-58.64,
            precision=PropertyLocation.Precision.MANUAL,
            provider="manual",
            confidence=1,
            manually_corrected=True,
        )
        Listing.objects.create(
            source=self.source,
            property=self.property,
            external_id="mobile-view-property",
            url="https://example.com/mobile-view-property",
            active=True,
        )

    def test_mobile_routes_require_auth_and_admin_routes_do_not_exist(self):
        response = self.client.get("/recorrido/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        response = self.client.post(
            "/api/recorrido/cercanas/",
            data=json.dumps({"latitude": -34.59, "longitude": -58.64}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.assertEqual(self.client.get("/scraping/").status_code, 404)
        self.assertEqual(self.client.get("/export/properties.csv").status_code, 404)
        self.assertEqual(self.client.get("/salud/").json()["service"], "radar-mobile")

    def test_authenticated_user_can_open_drive_and_query_nearby(self):
        self.client.force_login(self.user)

        page = self.client.get("/recorrido/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Iniciar recorrido")
        self.assertContains(page, "drive-mode.js")
        self.assertIn("no-store", page["Cache-Control"])

        response = self.client.post(
            "/api/recorrido/cercanas/",
            data=json.dumps(
                {
                    "latitude": -34.59,
                    "longitude": -58.64,
                    "radius_m": 350,
                    "property_types": ["house"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["properties"][0]["id"], self.property.pk)
        self.assertNotIn("personal_notes", payload["properties"][0])
        self.assertNotIn("manual_overrides", payload["properties"][0])

    def test_favorite_endpoint_only_changes_favorite(self):
        self.client.force_login(self.user)
        original_corrected_at = self.property.data_manually_corrected_at

        response = self.client.post(
            f"/api/recorrido/propiedad/{self.property.pk}/favorito/",
            data=json.dumps({"is_favorite": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.property.refresh_from_db()
        self.location.refresh_from_db()
        self.assertTrue(self.property.is_favorite)
        self.assertEqual(self.property.manual_overrides, {"address": "preserve"})
        self.assertEqual(self.property.data_manually_corrected_at, original_corrected_at)
        self.assertEqual(self.property.personal_notes, "No modificar")
        self.assertTrue(self.location.manually_corrected)
        self.assertEqual(self.location.provider, "manual")
        self.assertEqual(self.location.precision, PropertyLocation.Precision.MANUAL)

    def test_invalid_payloads_return_400(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/recorrido/cercanas/",
            data=json.dumps(
                {"latitude": -34.59, "longitude": -58.64, "radius_m": 5000}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("radius_m", response.json()["error"])

        response = self.client.post(
            f"/api/recorrido/propiedad/{self.property.pk}/favorito/",
            data=json.dumps({"is_favorite": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
