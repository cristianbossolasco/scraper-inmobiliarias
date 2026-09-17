import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from properties.models import Listing, ListingImage, Property, PropertyLocation, Source
from properties.services.drive_mode import (
    DriveModeValidationError,
    _truncate_grouped_properties,
    drive_property_card,
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
        currency="USD",
        bedrooms=3,
        covered_area=140,
        land_area=300,
        property_type=Property.Type.HOUSE,
        listing_url=None,
        provider="source-map",
        manually_corrected=False,
    ):
        property_obj = Property.objects.create(
            fingerprint=fingerprint,
            title=f"Casa {fingerprint}",
            property_type=property_type,
            operation="sale",
            status=status,
            currency=currency,
            price=price,
            bedrooms=bedrooms,
            covered_area=covered_area,
            land_area=land_area,
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
            url=listing_url or f"https://example.com/{fingerprint}",
            active=active_listing,
        )
        return property_obj

    def test_extended_radius_includes_house_beyond_one_kilometre(self):
        distant = self.create_property("extended-radius", latitude=-34.5792)
        payload = {"latitude": -34.59, "longitude": -58.64, "radius_m": 1000}
        self.assertEqual(nearby_drive_properties(payload)["count"], 0)
        payload["radius_m"] = 1500
        self.assertEqual(nearby_drive_properties(payload)["properties"][0]["id"], distant.pk)
        payload["radius_m"] = 1501
        with self.assertRaises(DriveModeValidationError):
            parse_drive_query(payload)

    def test_parse_drive_query_validates_bounds_radius_types_and_prices(self):
        parsed = parse_drive_query(
            {
                "latitude": -34.59,
                "longitude": -58.64,
                "radius_m": 350,
                "property_types": ["house"],
                "price_min": 90000,
                "price_max": 180000,
                "bedrooms_min": 2,
                "covered_area_min_m2": 90,
                "land_area_min_m2": 250,
            }
        )
        self.assertEqual(parsed["radius_m"], 350)
        self.assertEqual(parsed["property_types"], ["house"])
        self.assertEqual(parsed["bedrooms_min"], 2)
        self.assertEqual(parsed["covered_area_min_m2"], 90)
        self.assertEqual(parsed["land_area_min_m2"], 250)

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
            {"latitude": -34.59, "longitude": -58.64, "bedrooms_min": 2.5},
            {"latitude": -34.59, "longitude": -58.64, "bedrooms_min": True},
            {"latitude": -34.59, "longitude": -58.64, "price_currency": "ARS"},
            {"latitude": -34.59, "longitude": -58.64, "land_area_min_m2": 100001},
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
        self.assertNotIn("location_reliability", payload["properties"][0])
        self.assertEqual(payload["properties"][0]["type_label"], "Casa")
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
        self.assertEqual(items[first.pk]["group_returned_count"], 2)
        self.assertFalse(items[first.pk]["group_truncated"])

    def test_nearby_properties_combines_all_mobile_filters(self):
        matching = self.create_property(
            "matching-filters",
            price=180000,
            bedrooms=3,
            covered_area=110,
            land_area=320,
        )
        self.create_property("too-cheap", price=80000)
        self.create_property("few-bedrooms", bedrooms=2)
        self.create_property("small-covered", covered_area=80)
        self.create_property("small-land", land_area=220)
        self.create_property("ars-price", currency="ARS", price=150000)

        with CaptureQueriesContext(connection) as captured:
            payload = nearby_drive_properties(
                {
                    "latitude": -34.59,
                    "longitude": -58.64,
                    "radius_m": 350,
                    "property_types": ["house"],
                    "price_min": 100000,
                    "price_max": 250000,
                    "bedrooms_min": 3,
                    "covered_area_min_m2": 100,
                    "land_area_min_m2": 300,
                }
            )

        self.assertEqual([item["id"] for item in payload["properties"]], [matching.pk])
        self.assertEqual(len(captured), 2)
        self.assertEqual(
            payload["applied_filters"],
            {
                "property_types": ["house"],
                "price_currency": "USD",
                "price_min": 100000.0,
                "price_max": 250000.0,
                "bedrooms_min": 3,
                "covered_area_min_m2": 100.0,
                "land_area_min_m2": 300.0,
            },
        )

    def test_group_aware_truncation_does_not_split_a_small_group(self):
        singles = [
            [{"id": index, "group_id": f"single-{index}", "group_count": 1}]
            for index in range(3)
        ]
        pair = [[
            {"id": 10, "group_id": "pair", "group_count": 2},
            {"id": 11, "group_id": "pair", "group_count": 2},
        ]]

        selected, truncated = _truncate_grouped_properties(singles + pair, limit=4)

        self.assertTrue(truncated)
        self.assertEqual([item["id"] for item in selected], [0, 1, 2])

    def test_card_manual_location_is_confirmed_even_without_property_confidence(self):
        property_obj = self.create_property(
            "manual",
            precision=PropertyLocation.Precision.MANUAL,
            provider="manual",
            manually_corrected=True,
            location_confidence=Property.LocationConfidence.UNKNOWN,
        )

        self.assertEqual(
            drive_property_card(property_obj.pk)["location_reliability"],
            "confirmed",
        )

    def test_card_resolves_relative_active_image_and_labels_address_honestly(self):
        property_obj = self.create_property(
            "card-relative",
            precision=PropertyLocation.Precision.MANUAL,
            provider="manual",
            manually_corrected=True,
        )
        property_obj.address = "Necochea 1350"
        property_obj.save(update_fields=["address"])
        listing = property_obj.listings.get()
        ListingImage.objects.create(
            listing=listing,
            url="/media/casa.jpg",
            position=0,
        )

        card = drive_property_card(property_obj.pk)

        self.assertEqual(card["image_url"], "https://example.com/media/casa.jpg")
        self.assertEqual(card["original_url"], listing.url)
        self.assertEqual(card["original_host"], "example.com")
        self.assertEqual(card["original_source_name"], "Drive Test")
        self.assertEqual(card["address_text"], "Necochea 1350")
        self.assertEqual(card["address_reliability"], "published")
        self.assertEqual(card["location_reliability"], "confirmed")

        property_obj.manual_overrides = {"address": "manual"}
        property_obj.save(update_fields=["manual_overrides"])
        self.assertEqual(
            drive_property_card(property_obj.pk)["address_reliability"],
            "confirmed",
        )

    def test_card_uses_active_https_image_only_and_has_constant_queries(self):
        property_obj = self.create_property("card-image-policy")
        active_listing = property_obj.listings.get()
        ListingImage.objects.create(
            listing=active_listing,
            url="http://example.com/insecure.jpg",
            position=0,
        )
        inactive_listing = Listing.objects.create(
            source=self.source,
            property=property_obj,
            external_id="card-image-policy-inactive",
            url="https://example.com/inactive",
            active=False,
        )
        ListingImage.objects.create(
            listing=inactive_listing,
            url="https://example.com/inactive.jpg",
            position=0,
        )

        with CaptureQueriesContext(connection) as captured:
            card = drive_property_card(property_obj.pk)

        self.assertLessEqual(len(captured), 2)
        self.assertEqual(card["image_url"], "")
        self.assertEqual(card["original_url"], "https://example.com/card-image-policy")

        ListingImage.objects.create(
            listing=active_listing,
            url="https://cdn.example.com/second.jpg#tracking",
            position=1,
        )
        self.assertEqual(
            drive_property_card(property_obj.pk)["image_url"],
            "https://cdn.example.com/second.jpg",
        )

    def test_card_returns_none_for_ineligible_property(self):
        property_obj = self.create_property("card-hidden", hidden=True)
        self.assertIsNone(drive_property_card(property_obj.pk))

    def test_card_never_exposes_an_insecure_original_url(self):
        property_obj = self.create_property(
            "card-http-original",
            listing_url="http://example.com/card-http-original",
        )

        card = drive_property_card(property_obj.pk)

        self.assertEqual(card["original_url"], "")
        self.assertEqual(card["original_host"], "")

    def test_benchmark_command_reports_read_only_api_metrics(self):
        self.create_property("benchmark-drive")
        output = StringIO()

        call_command(
            "benchmark_drive_mode",
            latitude=-34.59,
            longitude=-58.64,
            radius=350,
            iterations=2,
            stdout=output,
        )

        report = json.loads(output.getvalue())
        self.assertEqual(report["nearby"]["iterations"], 2)
        self.assertEqual(report["nearby"]["properties"], 1)
        self.assertEqual(report["nearby"]["queries_min"], 2)
        self.assertLess(report["nearby"]["gzip_bytes"], report["nearby"]["json_bytes"])


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
            address="Necochea 1350",
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
        self.listing = Listing.objects.create(
            source=self.source,
            property=self.property,
            external_id="mobile-view-property",
            url="https://example.com/mobile-view-property",
            active=True,
        )
        ListingImage.objects.create(
            listing=self.listing,
            url="/images/mobile-house.jpg",
            position=0,
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
        self.assertEqual(
            self.client.get(
                f"/api/recorrido/propiedad/{self.property.pk}/ficha/"
            ).status_code,
            401,
        )
        self.assertEqual(self.client.get("/salud/").json()["service"], "radar-mobile")

    def test_authenticated_user_can_open_drive_and_query_nearby(self):
        self.client.force_login(self.user)

        page = self.client.get("/recorrido/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Iniciar recorrido")
        self.assertContains(page, "Dormitorios mínimos")
        self.assertContains(page, "Precio máximo (USD)")
        self.assertContains(page, "Superficie cubierta mín.")
        self.assertContains(page, "Ver publicación original")
        self.assertContains(page, "drive-mode.js")
        self.assertContains(page, "drive-utils.js")
        self.assertContains(page, "vendor/maplibre/maplibre-gl.js")
        self.assertNotContains(page, "unpkg.com")
        self.assertIn("no-store", page["Cache-Control"])
        self.assertIn("script-src 'self'", page["Content-Security-Policy"])

        response = self.client.post(
            "/api/recorrido/cercanas/",
            data=json.dumps(
                {
                    "latitude": -34.59,
                    "longitude": -58.64,
                    "radius_m": 350,
                    "property_types": ["house"],
                    "price_min": 100000,
                    "price_max": 200000,
                    "bedrooms_min": 2,
                    "covered_area_min_m2": 90,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["applied_filters"]["bedrooms_min"], 2)
        self.assertEqual(payload["properties"][0]["id"], self.property.pk)
        self.assertNotIn("personal_notes", payload["properties"][0])
        self.assertNotIn("manual_overrides", payload["properties"][0])
        self.assertIn("drive;dur=", response["Server-Timing"])

        card_response = self.client.get(
            f"/api/recorrido/propiedad/{self.property.pk}/ficha/"
        )
        self.assertEqual(card_response.status_code, 200)
        self.assertIn("no-store", card_response["Cache-Control"])
        self.assertIn("serialize;dur=", card_response["Server-Timing"])
        card = card_response.json()
        self.assertEqual(card["address_text"], "Necochea 1350")
        self.assertEqual(card["address_reliability"], "confirmed")
        self.assertEqual(
            card["image_url"],
            "https://example.com/images/mobile-house.jpg",
        )
        self.assertEqual(
            card["original_url"],
            "https://example.com/mobile-view-property",
        )
        self.assertEqual(card["covered_area_m2"], 100.0)
        for internal_field in (
            "personal_notes",
            "manual_overrides",
            "description",
            "raw_data",
        ):
            self.assertNotIn(internal_field, card)

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
