import json
import re
from pathlib import Path
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase

from properties.models import (
    Agency,
    ListingSnapshot,
    Property,
    PropertyLocation,
    ScrapeJob,
    ScrapeJobSource,
    ScrapeRun,
    Source,
)
from properties.services.ingestion import ingest_listing, mark_missing
from properties.services.location_enrichment import clean_detected_address, enrich_location_data
from properties.services.agency_normalization import normalize_agency_name
from properties.services.data_quality import is_garage_like, is_rental_url, valid_price, valid_value
from properties.services.geocoding import Geocoder
from properties.services.normalization import (
    classify_address_precision,
    normalize_address,
    parse_decimal,
)
from properties.services.scraping import create_scrape_job, db_writer_snapshot, run_scrape_job
from properties.services.spatial import haversine_km, point_in_polygon
from properties.scrapers.argenprop import ArgenpropScraper
from properties.scrapers.base import ROBOTS_CACHE
from properties.scrapers.local_wordpress import (
    MiglieriniScraper,
    OdriozolaScraper,
    is_miglierini_detail_url,
    is_odriozola_detail_url,
)
from properties.scrapers.mapaprop import MapapropScraper
from properties.scrapers.mercadoprop import MercadoPropScraper
from properties.scrapers.pending_sources import (
    AnaliaFernandezScraper,
    FincasScraper,
    GuarnieriScraper,
    InmueblesClarinScraper,
    LopezCombaScraper,
    MarceloRussoScraper,
    MercadoLibreScraper,
    PatagonPropScraper,
    PaulaFossatiScraper,
    RemaxDataworkScraper,
    RiquelmeScraper,
    ZonapropScraper,
)
from properties.scrapers.registry import get_adapter, get_adapter_classes


FIXTURES = Path(__file__).resolve().parent / "test_fixtures"


def fixture_soup(name):
    return BeautifulSoup((FIXTURES / name).read_text(encoding="utf-8"), "lxml")


class NormalizationTests(TestCase):
    def test_argentine_address_normalization(self):
        self.assertEqual(
            normalize_address("Av. Gdor. Vergara 2.550, Hurlingham"),
            "avenida gobernador vergara 2 550 hurlingham",
        )

    def test_decimal_formats(self):
        self.assertEqual(parse_decimal("USD 169.000"), Decimal("169000"))
        self.assertEqual(parse_decimal("151,50 m²"), Decimal("151.50"))

    def test_precision_classification(self):
        self.assertEqual(classify_address_precision("Gurruchaga 1733"), "exact")
        self.assertEqual(classify_address_precision("Gurruchaga al 1700"), "street")
        self.assertEqual(classify_address_precision("Cañuelas y Villegas"), "intersection")
        self.assertEqual(classify_address_precision("Mascagni"), "street")


    def test_agency_name_normalization(self):
        self.assertEqual(
            normalize_agency_name(
                "AURELLANA DESARROLLOS INMOBILIARIOS 81000 m2 Operation: Venta Address: Vergara"
            ),
            "AURELLANA DESARROLLOS INMOBILIARIOS",
        )
        self.assertEqual(
            normalize_agency_name("RIQUELME Propiedades Operation: Venta Address: PIZURNO"),
            "RIQUELME Propiedades",
        )
        self.assertEqual(
            normalize_agency_name("Ubicación O'Higgins 1400 Características Cant. Baños: 3"),
            "",
        )

    def test_address_cleanup_stops_at_location_and_services(self):
        self.assertEqual(
            clean_detected_address(
                "Pérez Galdós al 1100 Ubicación Hurlingham Agua Corriente Sí Alumbrado publico Sí"
            ),
            "Pérez Galdós al 1100",
        )


class SpatialTests(TestCase):
    def test_haversine_and_polygon(self):
        distance = haversine_km(-34.60, -58.64, -34.61, -58.64)
        self.assertAlmostEqual(distance, 1.11, places=1)
        polygon = [
            [-58.70, -34.66],
            [-58.60, -34.66],
            [-58.60, -34.55],
            [-58.70, -34.55],
        ]
        self.assertTrue(point_in_polygon(-34.60, -58.64, polygon))
        self.assertFalse(point_in_polygon(-34.75, -58.64, polygon))


class IngestionTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            slug="test", name="Test", base_url="https://example.com"
        )
        self.data = {
            "external_id": "abc-1",
            "url": "https://example.com/abc-1",
            "title": "Casa en Hurlingham",
            "address": "Gurruchaga 1733",
            "locality": "Hurlingham",
            "currency": "USD",
            "price": "120.000",
            "bedrooms": 3,
            "covered_area": 120,
            "land_area": 300,
            "latitude": -34.587,
            "longitude": -58.638,
            "location_precision": "exact",
        }

    def test_reuses_listing_and_records_price_history(self):
        listing, created = ingest_listing(self.source, self.data)
        self.assertTrue(created)
        changed = dict(self.data, price="115.000")
        second, created = ingest_listing(self.source, changed)
        self.assertFalse(created)
        self.assertEqual(listing.pk, second.pk)
        self.assertEqual(ListingSnapshot.objects.count(), 2)
        second.property.refresh_from_db()
        self.assertEqual(second.property.price, Decimal("115000"))

    def test_manual_location_is_not_overwritten(self):
        listing, _ = ingest_listing(self.source, self.data)
        location = listing.property.location
        location.latitude = -34.59
        location.longitude = -58.65
        location.precision = PropertyLocation.Precision.MANUAL
        location.manually_corrected = True
        location.save()
        ingest_listing(
            self.source,
            dict(self.data, latitude=-34.61, longitude=-58.67),
        )
        location.refresh_from_db()
        self.assertEqual(location.latitude, -34.59)
        self.assertTrue(location.manually_corrected)

    def test_listing_removed_after_two_missing_runs(self):
        listing, _ = ingest_listing(self.source, self.data)
        mark_missing(self.source, [])
        listing.refresh_from_db()
        self.assertTrue(listing.active)
        mark_missing(self.source, [])
        listing.refresh_from_db()
        listing.property.refresh_from_db()
        self.assertFalse(listing.active)
        self.assertEqual(listing.property.status, Property.Status.REMOVED)

    def test_location_enrichment_detects_detail_evidence(self):
        listing, _ = ingest_listing(
            self.source,
            {
                "external_id": "abc-2",
                "url": "https://example.com/abc-2",
                "title": "Casa en venta",
                "description": "Ubicada en Parque Johnston, cerca de Vergara y Pedro Diaz.",
                "currency": "USD",
                "price": 99000,
                "raw_data": {"detail_text": "Hurlingham - Parque Johnston - Vergara"},
            },
        )
        property_obj = listing.property
        self.assertEqual(property_obj.detected_locality, "Hurlingham")
        self.assertEqual(property_obj.detected_neighborhood, "Parque Johnston")
        self.assertEqual(property_obj.location_confidence, Property.LocationConfidence.MEDIUM)
        self.assertIn("Vergara", property_obj.location_evidence["detected_references"])

    def test_location_enrichment_marks_contradictions(self):
        data = enrich_location_data(
            {
                "title": "Casa",
                "address": "Ocampo 1900",
                "locality": "Villa Tesei",
                "raw_data": {"detail_text": "Hurlingham, Gurruchaga 1733"},
            }
        )
        self.assertIn("Contradiccion", data["location_notes"])

    def test_geocoder_query_uses_detected_address(self):
        property_obj = Property.objects.create(
            fingerprint="geo-detected-1",
            title="Casa con direccion detectada",
            detected_address="Bizet 1900",
            detected_locality="Hurlingham",
        )
        query = Geocoder().build_query(property_obj)
        self.assertEqual(
            query,
            "Bizet 1900, Hurlingham, Partido de Hurlingham, Buenos Aires, Argentina",
        )

    @patch("properties.management.commands.geocode_pending.Geocoder")
    def test_geocode_pending_filters_by_source_and_address(self, geocoder_cls):
        other_source = Source.objects.create(
            slug="other", name="Other", base_url="https://other.example"
        )
        ingest_listing(self.source, dict(self.data, external_id="abc-geo", latitude=None, longitude=None))
        ingest_listing(
            other_source,
            {
                "external_id": "other-geo",
                "url": "https://other.example/geo",
                "title": "Otra casa",
                "currency": "USD",
                "price": 100000,
            },
        )
        geocoder_cls.return_value.geocode_property.return_value = True
        output = StringIO()
        call_command(
            "geocode_pending",
            "--source",
            "test",
            "--only-with-address",
            "--limit",
            "10",
            stdout=output,
        )
        self.assertEqual(geocoder_cls.return_value.geocode_property.call_count, 1)
        self.assertIn("1 propiedades geolocalizadas", output.getvalue())


class ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        source = Source.objects.create(
            slug="test", name="Test", base_url="https://example.com"
        )
        self.listing, _ = ingest_listing(
            source,
            {
                "external_id": "web-1",
                "url": "https://example.com/web-1",
                "title": "Casa con pileta",
                "description": "Parque, pileta y quincho",
                "address": "Ocampo 1900",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 150000,
                "bedrooms": 3,
                "land_area": 400,
                "latitude": -34.59,
                "longitude": -58.64,
                "location_precision": "exact",
            },
        )

    def test_search_and_geojson_filters(self):
        response = self.client.get("/", {"q": "pileta", "bedrooms_min": 3})
        self.assertContains(response, "Casa con pileta")
        response = self.client.get(
            "/api/propiedades/",
            {"radius_lat": -34.59, "radius_lng": -58.64, "radius_km": 1},
        )
        payload = response.json()
        self.assertEqual(len(payload["features"]), 1)

    def test_detail_links_preserve_return_to_and_show_original_links(self):
        query = "price_max=200000&sort=-last_seen"
        response = self.client.get("/", {"price_max": "200000", "sort": "-last_seen"})
        self.assertContains(response, f"/propiedad/{self.listing.property_id}/?return_to=")

        response = self.client.get(
            f"/propiedad/{self.listing.property_id}/",
            {"return_to": f"/?{query}"},
        )
        self.assertContains(response, 'href="/?price_max=200000&amp;sort=-last_seen"')
        self.assertContains(response, 'class="source-button"')
        self.assertContains(response, 'href="https://example.com/web-1"')

        response = self.client.get(
            f"/propiedad/{self.listing.property_id}/",
            {"return_to": "https://evil.example/"},
        )
        self.assertContains(response, 'href="/"')

    def test_polygon_filter(self):
        polygon = json.dumps(
            [
                [-58.65, -34.60],
                [-58.63, -34.60],
                [-58.63, -34.58],
                [-58.65, -34.58],
            ]
        )
        response = self.client.get("/api/propiedades/", {"polygon": polygon})
        self.assertEqual(len(response.json()["features"]), 1)

    def test_manual_location_endpoint(self):
        response = self.client.post(
            f"/api/propiedad/{self.listing.property_id}/ubicacion/",
            data=json.dumps({"latitude": -34.591, "longitude": -58.641}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.listing.property.location.refresh_from_db()
        self.assertEqual(
            self.listing.property.location.precision,
            PropertyLocation.Precision.MANUAL,
        )

    def test_property_state_notes_and_filters(self):
        property_id = self.listing.property_id
        response = self.client.post(
            f"/api/propiedad/{property_id}/estado/",
            data=json.dumps({"is_favorite": True, "reviewed": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.listing.property.refresh_from_db()
        self.assertTrue(self.listing.property.is_favorite)
        self.assertIsNotNone(self.listing.property.reviewed_at)

        response = self.client.post(
            f"/api/propiedad/{property_id}/nota/",
            data=json.dumps({"personal_notes": "Visitar el sabado"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.listing.property.refresh_from_db()
        self.assertEqual(self.listing.property.personal_notes, "Visitar el sabado")

        response = self.client.get("/", {"favorite": "1", "review_state": "reviewed"})
        self.assertContains(response, "Casa con pileta")

    def test_table_view_source_filter_and_quality_filter(self):
        response = self.client.get("/", {"view": "table", "source": str(self.listing.source_id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="property-table"')
        self.assertContains(response, "Casa con pileta")
        self.assertContains(response, "Test")

        response = self.client.get("/", {"quality_field": "surface", "quality_state": "present"})
        self.assertContains(response, "Casa con pileta")
        response = self.client.get("/", {"quality_field": "surface", "quality_state": "missing"})
        self.assertNotContains(response, "Casa con pileta")

    def test_card_shows_detected_address_without_coordinates(self):
        ingest_listing(
            self.listing.source,
            {
                "external_id": "web-2",
                "url": "https://example.com/web-2",
                "title": "Casa con direccion",
                "address": "Bizet 1900",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 110000,
            },
        )
        response = self.client.get("/")
        self.assertContains(response, "Direccion detectada")

    def test_agency_filter_excludes_orphan_agencies(self):
        Agency.objects.create(name="Ubicación enorme sin publicaciones")
        response = self.client.get("/")
        self.assertNotContains(response, "Ubicación enorme sin publicaciones")

    def test_hidden_properties_are_excluded_by_default(self):
        property_obj = self.listing.property
        property_obj.is_hidden = True
        property_obj.save(update_fields=["is_hidden"])
        response = self.client.get("/")
        self.assertNotContains(response, "Casa con pileta")
        response = self.client.get("/", {"show_hidden": "1"})
        self.assertContains(response, "Casa con pileta")

    def test_exports_and_stats(self):
        response = self.client.get("/export/properties.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("propiedades.csv", response["Content-Disposition"])
        self.assertContains(response, "Casa con pileta")

        response = self.client.get("/export/properties.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheet", response["Content-Type"])

        response = self.client.get("/estadisticas/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard de mercado")
        self.assertContains(response, "Mediana precio/m2")
        self.assertContains(response, "quality_field=surface")
        chart_data = json.loads(
            BeautifulSoup(response.content, "lxml").find(id="chart-data").string
        )
        self.assertIn("url", chart_data["by_locality"][0])
        self.assertIn("price_buckets", chart_data)

    def test_stats_exclude_metric_outliers(self):
        source = Source.objects.create(
            slug="outlier", name="Outlier", base_url="https://outlier.example"
        )
        ingest_listing(
            source,
            {
                "external_id": "bad",
                "url": "https://outlier.example/bad",
                "title": "Casa codigo roto",
                "locality": "Hurlingham",
                "currency": "USD",
                "price": 120000,
                "bedrooms": 4319,
            },
        )
        response = self.client.get("/estadisticas/")
        self.assertContains(response, "Datos a revisar")
        self.assertNotContains(response, "2161")

    def test_scraping_api_creates_status_and_cancels_job(self):
        with patch("properties.views.start_scrape_job") as starter:
            response = self.client.post(
                "/api/scraping/jobs/",
                data=json.dumps(
                    {
                        "sources": ["mapaprop"],
                        "workers": {"mapaprop": 2},
                        "max_pages": 1,
                        "max_listings": 3,
                        "scrape_mode": "trial",
                        "request_timeout_seconds": 12,
                        "max_errors_per_source": 2,
                        "geocode_limit": 7,
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201)
        starter.assert_called_once()
        job = ScrapeJob.objects.get()
        self.assertEqual(job.worker_config["mapaprop"], 2)
        self.assertEqual(job.scrape_mode, ScrapeJob.Mode.TRIAL)
        self.assertEqual(job.request_timeout_seconds, 12)
        self.assertEqual(job.max_errors_per_source, 2)
        self.assertEqual(job.geocode_limit, 7)
        source_progress = ScrapeJobSource.objects.get(job=job, slug="mapaprop")
        self.assertEqual(source_progress.workers, 2)

        response = self.client.get(f"/api/scraping/jobs/{job.pk}/")
        self.assertEqual(response.json()["sources"][0]["slug"], "mapaprop")

        response = self.client.post(f"/api/scraping/jobs/{job.pk}/cancel/")
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertTrue(job.cancel_requested)

    def test_running_job_without_live_thread_is_marked_interrupted(self):
        job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.RUNNING,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
        )
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        ScrapeJobSource.objects.create(
            job=job,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            status=ScrapeJobSource.Status.RUNNING,
        )
        response = self.client.get(f"/api/scraping/jobs/{job.pk}/")
        self.assertEqual(response.json()["status"], ScrapeJob.Status.INTERRUPTED)

    def test_finished_job_with_pending_sources_is_marked_partial(self):
        job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.SUCCESS,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
        )
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        ScrapeJobSource.objects.create(
            job=job,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            status=ScrapeJobSource.Status.PENDING,
        )
        response = self.client.get(f"/api/scraping/jobs/{job.pk}/")
        payload = response.json()
        self.assertEqual(payload["status"], ScrapeJob.Status.PARTIAL)
        self.assertEqual(payload["sources"][0]["status"], ScrapeJobSource.Status.INTERRUPTED)

    def test_scraping_api_retries_finished_job_with_same_parameters(self):
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        original = ScrapeJob.objects.create(
            status=ScrapeJob.Status.PARTIAL,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 4},
            scrape_mode=ScrapeJob.Mode.TRIAL,
            max_pages=2,
            max_listings=5,
            request_timeout_seconds=17,
            max_errors_per_source=3,
            geocode_limit=9,
        )
        ScrapeJobSource.objects.create(
            job=original,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            workers=4,
            status=ScrapeJobSource.Status.PARTIAL,
        )
        with patch("properties.views.start_scrape_job") as starter:
            response = self.client.post(f"/api/scraping/jobs/{original.pk}/retry/")

        self.assertEqual(response.status_code, 201)
        starter.assert_called_once()
        retried = ScrapeJob.objects.exclude(pk=original.pk).get()
        self.assertEqual(retried.selected_sources, ["mapaprop"])
        self.assertEqual(retried.worker_config, {"mapaprop": 4})
        self.assertEqual(retried.scrape_mode, ScrapeJob.Mode.TRIAL)
        self.assertEqual(retried.max_pages, 2)
        self.assertEqual(retried.max_listings, 5)
        self.assertEqual(retried.request_timeout_seconds, 17)
        self.assertEqual(retried.max_errors_per_source, 3)
        self.assertEqual(retried.geocode_limit, 9)


class ScraperParserTests(TestCase):
    def parse_with_fixture(self, scraper_cls, fixture_name, url):
        scraper = scraper_cls()
        scraper.soup = lambda parsed_url: fixture_soup(fixture_name)
        return scraper.parse(url)

    def test_argenprop_parser_fixture(self):
        data = self.parse_with_fixture(
            ArgenpropScraper,
            "argenprop_detail.html",
            "https://www.argenprop.com/casa-en-venta-en-hurlingham-4-ambientes--19660272",
        )
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("98000"))
        self.assertEqual(data["address"], "Basilio Delleva 1500")
        self.assertEqual(data["agency"], "Adriana Dato Inmobiliaria")
        self.assertEqual(data["bedrooms"], 1)
        self.assertNotIn("latitude", data)

    def test_argenprop_labeled_metrics_override_json_ld(self):
        data = self.parse_with_fixture(
            ArgenpropScraper,
            "argenprop_labeled_metrics_detail.html",
            "https://www.argenprop.com/casa-en-venta-en-hurlingham-4-ambientes--19806714",
        )
        self.assertEqual(data["address"], "Maestra Gonzalez de Hecht 1000")
        self.assertEqual(data["rooms"], 4)
        self.assertEqual(data["bedrooms"], 2)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["garages"], 1)
        self.assertEqual(data["covered_area"], Decimal("160"))
        self.assertEqual(data["land_area"], Decimal("220"))

    def test_argenprop_discovery_uses_public_max_page_beyond_10(self):
        scraper = ArgenpropScraper()

        def fake_soup(url):
            page_match = re.search(r"pagina-(\d+)", url)
            page = int(page_match.group(1)) if page_match else 1
            next_link = '<a href="/casas/venta/hurlingham?pagina-12">12</a>' if page == 1 else ""
            return BeautifulSoup(
                f"""
                <html><body>
                  {next_link}
                  <a href="/casa-en-venta-en-hurlingham--100{page}">Casa {page}</a>
                </body></html>
                """,
                "lxml",
            )

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(len(urls), 12)
        self.assertTrue(urls[-1].endswith("--10012"))

    def test_argenprop_discovery_respects_max_pages(self):
        scraper = ArgenpropScraper(max_pages=2)
        scraper.soup = lambda url: BeautifulSoup(
            """
            <html><body>
              <a href="/casas/venta/hurlingham?pagina-12">12</a>
              <a href="/casa-en-venta-en-hurlingham--100">Casa</a>
            </body></html>
            """,
            "lxml",
        )
        self.assertEqual(len(list(scraper.discover())), 1)
        self.assertTrue(scraper.discovery_stats["limited_by_max_pages"])
        self.assertIsNone(scraper.discovery_stats["coverage_ratio"])

    def test_argenprop_limited_batch_starts_at_requested_page(self):
        scraper = ArgenpropScraper(max_pages=3, max_listings=60, start_page=4)
        calls = []

        def fake_soup(url):
            calls.append(url)
            page_match = re.search(r"pagina-(\d+)", url)
            page = int(page_match.group(1)) if page_match else 1
            return BeautifulSoup(
                f"""
                <html><body>
                  <a href="/casa-en-venta-en-hurlingham--10{page}01">Casa {page} A</a>
                  <a href="/casa-en-venta-en-hurlingham--10{page}02">Casa {page} B</a>
                </body></html>
                """,
                "lxml",
            )

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(len(urls), 6)
        self.assertEqual(
            calls,
            [
                "https://www.argenprop.com/casas/venta/hurlingham?pagina-4",
                "https://www.argenprop.com/casas/venta/hurlingham?pagina-5",
                "https://www.argenprop.com/casas/venta/hurlingham?pagina-6",
            ],
        )
        self.assertTrue(scraper.discovery_stats["limited_by_max_pages"])
        self.assertIsNone(scraper.discovery_stats["declared_total"])
        self.assertIsNone(scraper.discovery_stats["coverage_ratio"])

    def test_argenprop_discovery_respects_max_listings(self):
        scraper = ArgenpropScraper(max_listings=3)
        calls = []

        def fake_soup(url):
            calls.append(url)
            return BeautifulSoup(
                """
                <html><body>
                  <h1>786 Casas en Venta en Hurlingham</h1>
                  <a href="/casas/venta/hurlingham?pagina-40">40</a>
                  <a href="/casa-en-venta-en-hurlingham--1001">Casa 1</a>
                  <a href="/casa-en-venta-en-hurlingham--1002">Casa 2</a>
                  <a href="/casa-en-venta-en-hurlingham--1003">Casa 3</a>
                  <a href="/casa-en-venta-en-hurlingham--1004">Casa 4</a>
                </body></html>
                """,
                "lxml",
            )

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(len(urls), 3)
        self.assertEqual(len(calls), 1)
        self.assertTrue(scraper.discovery_stats["limited_by_max_listings"])

    def test_robots_txt_is_cached_across_scraper_instances(self):
        ROBOTS_CACHE.clear()

        class FakeResponse:
            ok = True
            text = "User-agent: *\nAllow: /\n"

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.calls = 0

            def get(self, url, timeout=None):
                self.calls += 1
                return FakeResponse()

        first_session = FakeSession()
        second_session = FakeSession()
        first = ArgenpropScraper(session=first_session)
        second = ArgenpropScraper(session=second_session)

        self.assertTrue(first.allowed("https://www.argenprop.com/casas/venta/hurlingham"))
        self.assertTrue(second.allowed("https://www.argenprop.com/casas/venta/hurlingham?pagina-2"))
        self.assertEqual(first_session.calls, 1)
        self.assertEqual(second_session.calls, 0)
        ROBOTS_CACHE.clear()

    def test_mapaprop_highlights_parser(self):
        data = self.parse_with_fixture(
            MapapropScraper,
            "mapaprop_highlights_detail.html",
            "https://www.mapaprop.com/en/property/venta-de-terreno-en-hurlingham-1324-2612979/hash",
        )
        self.assertEqual(data["property_type"], Property.Type.LAND)
        self.assertEqual(data["total_area"], Decimal("81000"))
        self.assertEqual(data["land_area"], Decimal("81000"))
        self.assertEqual(data["covered_area"], Decimal("15000"))
        self.assertEqual(data["building_floors"], 1)
        self.assertEqual(data["garages"], 1)

    def test_mercadoprop_parser_fixture(self):
        data = self.parse_with_fixture(
            MercadoPropScraper,
            "mercadoprop_detail.html",
            "https://www.mercadoprop.net/ar/casas/venta-casa-monoambiente-hurlingham-141362",
        )
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("300000"))
        self.assertEqual(data["agency"], "NOR Broker Propiedades")
        self.assertEqual(data["latitude"], -34.590339)
        self.assertEqual(data["longitude"], -58.638686)

    def test_local_parsers_fixture(self):
        miglierini = self.parse_with_fixture(
            MiglieriniScraper,
            "miglierini_detail.html",
            "https://www.miglieriniprop.com/propiedad/chalet-4-amb-a-reciclar-parque-johnston/",
        )
        odriozola = self.parse_with_fixture(
            OdriozolaScraper,
            "odriozola_detail.html",
            "https://odriozolapropiedades.com.ar/inmobiliaria/propiedades/casa-3",
        )
        self.assertEqual(miglierini["agency"], "Miglierini Propiedades")
        self.assertEqual(odriozola["latitude"], -34.583046168811)

    def test_local_url_filters_exclude_categories(self):
        self.assertTrue(
            is_miglierini_detail_url(
                "https://www.miglieriniprop.com/propiedad/chalet-4-amb-a-reciclar-parque-johnston/"
            )
        )
        self.assertFalse(
            is_miglierini_detail_url(
                "https://www.miglieriniprop.com/propiedad-tipo/casa-chalet/"
            )
        )

    def test_pending_phase_one_scrapers_parse_common_fixture(self):
        for scraper_cls in (
            AnaliaFernandezScraper,
            LopezCombaScraper,
            RiquelmeScraper,
            FincasScraper,
            GuarnieriScraper,
        ):
            data = self.parse_with_fixture(
                scraper_cls,
                "pending_detail.html",
                f"{scraper_cls.definition.base_url}/propiedad/casa-hurlingham-123",
            )
            self.assertEqual(data["currency"], "USD")
            self.assertEqual(data["price"], Decimal("150000"))
            self.assertEqual(data["locality"], "Hurlingham")
            self.assertEqual(data["bedrooms"], 3)
            self.assertEqual(data["covered_area"], Decimal("120"))
            self.assertEqual(data["land_area"], Decimal("400"))
            self.assertEqual(data["agency"], scraper_cls.definition.name)

    def test_analia_fernandez_address_stops_before_location_metadata(self):
        data = self.parse_with_fixture(
            AnaliaFernandezScraper,
            "analia_fernandez_address_detail.html",
            "https://www.fernandezpropiedades.com.ar/p/7872839-Casa-en-Venta-en-Hurlingham-Pérez-Galdós-al-1100",
        )
        self.assertEqual(data["address"], "Pérez Galdós al 1100")

    def test_analia_fernandez_structured_tables(self):
        data = self.parse_with_fixture(
            AnaliaFernandezScraper,
            "analia_fernandez_full_detail.html",
            "https://www.fernandezpropiedades.com.ar/p/4743235-Casa-en-Venta-en-Hurlingham-Diego-Carabajal-al-500",
        )
        self.assertEqual(data["address"], "Diego Carabajal al 500")
        self.assertEqual(data["rooms"], 4)
        self.assertEqual(data["bedrooms"], 3)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["toilets"], 1)
        self.assertEqual(data["garages"], 2)
        self.assertEqual(data["age_years"], 0)
        self.assertEqual(data["building_floors"], 2)
        self.assertEqual(data["land_area"], Decimal("213.44"))
        self.assertEqual(data["covered_area"], Decimal("154.64"))
        self.assertEqual(data["uncovered_area"], Decimal("52.9"))
        self.assertEqual(data["semicovered_area"], Decimal("6"))
        self.assertEqual(data["front_width"], Decimal("8"))
        self.assertEqual(data["lot_depth"], Decimal("20"))

    def test_marcelo_russo_parser_fixture(self):
        data = self.parse_with_fixture(
            MarceloRussoScraper,
            "marcelo_russo_detail.html",
            "https://marcelorussoprop.com.ar/property/4289-ciudad-tesei/",
        )
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("79000"))
        self.assertEqual(data["locality"], "Villa Tesei")
        self.assertIsNone(data["rooms"])
        self.assertEqual(data["bedrooms"], 3)
        self.assertEqual(data["covered_area"], Decimal("85"))
        self.assertEqual(data["land_area"], Decimal("95"))

    def test_fincas_specific_metric_parser_fixtures(self):
        fraccion = self.parse_with_fixture(
            FincasScraper,
            "fincas_fraccion_detail.html",
            "https://www.haurie.argencasas.com/propiedad-fraccion-venta-hurlingham-301-1009",
        )
        quinta = self.parse_with_fixture(
            FincasScraper,
            "fincas_quinta_detail.html",
            "https://www.haurie.argencasas.com/propiedad-quinta-venta-hurlingham-301-1019",
        )
        self.assertEqual(fraccion["covered_area"], Decimal("180"))
        self.assertEqual(fraccion["total_area"], Decimal("18000"))
        self.assertEqual(quinta["rooms"], 10)
        self.assertEqual(quinta["bedrooms"], 5)
        self.assertEqual(quinta["bathrooms"], Decimal("3"))
        self.assertEqual(quinta["covered_area"], Decimal("440"))
        self.assertEqual(quinta["total_area"], Decimal("6388"))
        self.assertEqual(quinta["raw_data"]["free_area"], "5500")

    def test_guarnieri_specific_metric_parser_fixture(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_chalet_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/chalet-2-plantas-7-amb-parque-johnston-hurlingham-centro",
        )
        self.assertEqual(data["price"], Decimal("300000"))
        self.assertEqual(data["covered_area"], Decimal("360"))
        self.assertEqual(data["land_area"], Decimal("499"))
        self.assertEqual(data["bedrooms"], 4)
        self.assertEqual(data["bathrooms"], Decimal("3"))
        self.assertEqual(data["garages"], 1)
        self.assertEqual(data["locality"], "Hurlingham")
        self.assertEqual(data["neighborhood"], "Parque Johnston")

    def test_guarnieri_rental_ignores_suggested_property_prices(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_rental_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/depto-3amb-a-estrenar-william-morris",
        )
        self.assertEqual(data["operation"], "rent")
        self.assertEqual(data["currency"], "ARS")
        self.assertEqual(data["price"], Decimal("1050000"))
        self.assertEqual(data["covered_area"], Decimal("58"))
        self.assertEqual(data["bedrooms"], 2)

    def test_guarnieri_multi_unit_listing_uses_cheapest_unit_and_keeps_evidence(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_multi_unit_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/dptos-2-amb-y-3-amb-con-cocheras-opcionales-hurlingham",
        )
        self.assertEqual(data["source_status"], "multi_unit")
        self.assertEqual(data["price"], Decimal("84500"))
        self.assertEqual(data["rooms"], 2)
        self.assertIsNone(data["garages"])
        self.assertIsNone(data["covered_area"])
        self.assertEqual(data["total_area"], Decimal("71.37"))
        self.assertEqual(data["raw_data"]["unit_count"], 3)
        self.assertEqual(data["raw_data"]["unit_offers"][0]["price"], "151000")

    def test_guarnieri_table_metrics_override_suggestions(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_william_morris_table_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/casa-3amb-william-morris",
        )
        self.assertEqual(data["price"], Decimal("80000"))
        self.assertEqual(data["covered_area"], Decimal("115"))
        self.assertEqual(data["land_area"], Decimal("322"))
        self.assertEqual(data["rooms"], 3)
        self.assertEqual(data["bedrooms"], 2)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["garages"], 2)

    def test_guarnieri_garage_does_not_take_surface_value(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_garage_metric_block_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/exclusiva-propiedad-5-ambientes-hurlingham",
        )
        self.assertEqual(data["covered_area"], Decimal("90"))
        self.assertEqual(data["land_area"], Decimal("200"))
        self.assertEqual(data["bedrooms"], 3)
        self.assertEqual(data["bathrooms"], Decimal("3"))
        self.assertEqual(data["garages"], 1)

    def test_paula_fossati_lot_dimensions_become_area(self):
        data = self.parse_with_fixture(
            PaulaFossatiScraper,
            "paula_fossati_lote_detail.html",
            "https://paulafossati.com.ar/site/properties/494112/venta-de-importante-lote-en-villa-tesei",
        )
        self.assertEqual(data["operation"], "sale")
        self.assertEqual(data["land_area"], Decimal("375.0"))
        self.assertEqual(data["total_area"], Decimal("375.0"))
        self.assertEqual(data["raw_data"]["front_meters"], "10")
        self.assertEqual(data["raw_data"]["depth_meters"], "37.5")

    def test_paula_fossati_detail_table_preserves_address_and_surfaces(self):
        data = self.parse_with_fixture(
            PaulaFossatiScraper,
            "paula_fossati_full_detail.html",
            "https://paulafossati.com.ar/site/properties/494112/venta-de-importante-lote-en-villa-tesei",
        )
        self.assertEqual(data["address"], "VERAGUA 4905")
        self.assertEqual(data["neighborhood"], "Villa Santos Tesei")
        self.assertEqual(data["locality"], "Hurlingham")
        self.assertEqual(data["garages"], 1)
        self.assertEqual(data["covered_area"], Decimal("40"))
        self.assertEqual(data["uncovered_area"], Decimal("325"))
        self.assertEqual(data["front_width"], Decimal("10"))
        self.assertEqual(data["lot_depth"], Decimal("37.5"))

    def test_zonaprop_discovery_excludes_listings_and_rentals(self):
        scraper = ZonapropScraper()
        scraper.soup = lambda parsed_url: fixture_soup("zonaprop_listing.html")
        self.assertEqual(
            list(scraper.discover()),
            [
                "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-57923940.html"
            ],
        )

    def test_data_quality_rules(self):
        property_obj = Property(
            property_type=Property.Type.HOUSE,
            title="Casa",
            bedrooms=4319,
            price=Decimal("120000"),
            currency="USD",
        )
        self.assertIsNone(valid_value(property_obj, "bedrooms"))
        self.assertEqual(valid_price(property_obj), Decimal("120000"))

    def test_garage_quality_range_does_not_match_house_with_garage(self):
        house = Property(
            property_type=Property.Type.HOUSE,
            title="4279 - Hurlingham",
            description="Casa 4 ambientes con cochera y jardin",
            covered_area=Decimal("200"),
            land_area=Decimal("300"),
        )
        garage = Property(
            property_type=Property.Type.OTHER,
            title="Venta de cocheras en Hurlingham",
            covered_area=Decimal("12"),
            total_area=Decimal("12"),
        )
        self.assertFalse(is_garage_like(house))
        self.assertTrue(is_garage_like(garage))
        self.assertEqual(valid_value(house, "covered_area"), Decimal("200"))
        self.assertEqual(valid_value(house, "land_area"), Decimal("300"))

    def test_rental_url_detection(self):
        self.assertTrue(
            is_rental_url(
                "https://www.zonaprop.com.ar/propiedades/clasificado/alcldein-deposito-en-alquiler-en-hurlingham-57923949.html"
            )
        )

    def test_pending_scraper_discovery_filters_detail_links(self):
        for scraper_cls in (AnaliaFernandezScraper, LopezCombaScraper):
            scraper = scraper_cls()
            scraper.soup = lambda parsed_url: fixture_soup("pending_listing.html")
            urls = list(scraper.discover())
            self.assertIn(f"{scraper.definition.base_url}/p/123-Casa-en-Venta-en-Hurlingham-Test", urls)
            self.assertNotIn(f"{scraper.definition.base_url}/propiedad-tipo/casas", urls)
        scraper = RiquelmeScraper()
        scraper.soup = lambda parsed_url: fixture_soup("pending_listing.html")
        urls = list(scraper.discover())
        self.assertIn(
            f"{scraper.definition.base_url}/propiedad/casa-hurlingham-789",
            urls,
        )

    def test_marcelo_russo_discovery_filters_property_links(self):
        scraper = MarceloRussoScraper()
        scraper.soup = lambda parsed_url: fixture_soup("marcelo_russo_listing.html")
        self.assertEqual(
            list(scraper.discover()),
            ["https://marcelorussoprop.com.ar/property/3963-hurlingham/"],
        )

    def test_phase_two_portal_parsers_capture_agency_and_metrics(self):
        for scraper_cls in (
            InmueblesClarinScraper,
            PatagonPropScraper,
            ZonapropScraper,
        ):
            data = self.parse_with_fixture(
                scraper_cls,
                "portal_detail.html",
                f"{scraper_cls.definition.base_url}/casa-en-venta-en-hurlingham--11598328",
            )
            self.assertEqual(data["currency"], "USD")
            self.assertEqual(data["price"], Decimal("190000"))
            self.assertEqual(data["locality"], "Hurlingham")
            self.assertEqual(data["bedrooms"], 3)
            self.assertEqual(data["covered_area"], Decimal("160"))

    def test_inmuebles_clarin_captures_garage_and_land(self):
        data = self.parse_with_fixture(
            InmueblesClarinScraper,
            "portal_detail.html",
            "https://www.inmuebles.clarin.com/casa-en-venta-en-hurlingham-4-ambientes--18167379",
        )
        self.assertEqual(data["garages"], 1)
        self.assertEqual(data["covered_area"], Decimal("160"))
        self.assertEqual(data["land_area"], Decimal("390"))

    def test_patagonprop_structured_fields_and_suspicious_price(self):
        data = self.parse_with_fixture(
            PatagonPropScraper,
            "patagonprop_detail.html",
            "https://patagonprop.com/propiedad/buenos-aires-hurlingham-venta-saturnino-salas-405-72-184/hash",
        )
        self.assertEqual(data["address"], "Saturnino Salas 405")
        self.assertEqual(data["locality"], "Villa Tesei")
        self.assertEqual(data["property_type"], Property.Type.HOUSE)
        self.assertEqual(data["bedrooms"], 3)
        self.assertEqual(data["bathrooms"], Decimal("2"))
        self.assertEqual(data["total_area"], Decimal("350"))
        self.assertEqual(data["source_status"], "price_age_review")

    def test_remax_datawork_does_not_ingest_category_pages(self):
        scraper = RemaxDataworkScraper()
        scraper.soup = lambda parsed_url: fixture_soup("remax_category_page.html")
        self.assertEqual(list(scraper.discover()), [])
        self.assertIsNone(
            scraper.parse(
                "https://remaxdatawork.com.ar/propiedades-en-venta-buenos-aires/casas-en-venta-ramos-mejia/"
            )
        )

    def test_mercadolibre_api_scraper_discovers_and_parses_items(self):
        scraper = MercadoLibreScraper()

        def fake_api(url, **params):
            if url.endswith("/sites/MLA/search"):
                return {
                    "results": [
                        {
                            "id": "MLA123",
                            "title": "Casa en venta en Hurlingham",
                            "permalink": "https://casa.mercadolibre.com.ar/MLA-123",
                        }
                    ]
                }
            return {
                "id": "MLA123",
                "title": "Casa en venta en Hurlingham",
                "permalink": "https://casa.mercadolibre.com.ar/MLA-123",
                "currency_id": "USD",
                "price": 180000,
                "seller_id": 55,
                "location": {
                    "address_line": "Ocampo 1900",
                    "city": {"name": "Hurlingham"},
                    "latitude": -34.59,
                    "longitude": -58.64,
                },
                "attributes": [
                    {"id": "BEDROOMS", "value_name": "3"},
                    {"id": "FULL_BATHROOMS", "value_name": "2"},
                    {"id": "COVERED_AREA", "value_name": "120 m²"},
                    {"id": "TOTAL_AREA", "value_name": "400 m²"},
                ],
                "pictures": [{"secure_url": "https://img.example/1.jpg"}],
            }

        scraper.api_get = fake_api
        scraper.max_pages = 1
        urls = list(scraper.discover())
        self.assertEqual(urls, ["https://api.mercadolibre.com/items/MLA123"])
        data = scraper.parse(urls[0])
        self.assertEqual(data["external_id"], "MLA123")
        self.assertEqual(data["price"], Decimal("180000"))
        self.assertEqual(data["bedrooms"], 3)
        self.assertEqual(data["latitude"], -34.59)
        self.assertEqual(data["agency"], "MercadoLibre seller 55")

    def test_pending_sources_are_registered_disabled_by_default(self):
        slugs = {adapter.definition.slug for adapter in get_adapter_classes()}
        for slug in {
            "analia-fernandez",
            "marcelo-russo",
            "lopez-comba",
            "riquelme",
            "fincas",
            "guarnieri",
            "inmuebles-clarin",
            "patagonprop",
            "mercadolibre",
            "zonaprop",
        }:
            self.assertIn(slug, slugs)
            self.assertFalse(get_adapter(slug).definition.enabled)
        self.assertTrue(
            is_odriozola_detail_url(
                "https://odriozolapropiedades.com.ar/inmobiliaria/propiedades/casa-3"
            )
        )
        self.assertFalse(
            is_odriozola_detail_url(
                "https://odriozolapropiedades.com.ar/inmobiliaria/tipo-de-propiedad/casas-chalets"
            )
        )


class ScrapeCommandTests(TransactionTestCase):
    def test_max_listings_limits_processed_urls(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None):
                self.max_pages = max_pages

            def discover(self):
                return ["https://example.com/1", "https://example.com/2", "https://example.com/3"]

            def parse(self, url):
                number = url.rsplit("/", 1)[-1]
                return {
                    "external_id": number,
                    "url": url,
                    "title": f"Casa {number}",
                    "address": f"Calle {number} 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        output = StringIO()
        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            call_command("scrape", "--source", "fake", "--max-listings", "2", stdout=output)

        self.assertEqual(Source.objects.get(slug="fake").listings.count(), 2)
        self.assertEqual(ScrapeRun.objects.get(source__slug="fake").discovered, 2)
        self.assertIn("[Fake Source]", output.getvalue())
        self.assertIn("2/2 procesadas", output.getvalue())
        self.assertIn("3 URLs descubiertas; 2 a procesar", output.getvalue())
        self.assertIn("no se marcan ausentes", output.getvalue())

    def test_early_source_failure_does_not_leave_pending_or_success_job(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None):
                self.max_pages = max_pages

            def discover(self):
                raise RuntimeError("database is locked")

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            job = create_scrape_job(["fake"], {"fake": 1})
            run_scrape_job(job.pk)

        job.refresh_from_db()
        source = job.sources.get(slug="fake")
        self.assertEqual(job.status, ScrapeJob.Status.PARTIAL)
        self.assertEqual(source.status, ScrapeJobSource.Status.FAILED)
        self.assertIn("database is locked", source.logs)

    def test_sqlite_writer_queue_serializes_job_writes(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None, request_timeout=None):
                self.max_pages = max_pages

            def discover(self):
                return [f"https://example.com/{index}" for index in range(8)]

            def parse(self, url):
                number = url.rsplit("/", 1)[-1]
                return {
                    "external_id": number,
                    "url": url,
                    "title": f"Casa {number}",
                    "address": f"Calle {number} 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            job = create_scrape_job(["fake"], {"fake": 6})
            run_scrape_job(job.pk)

        job.refresh_from_db()
        stats = db_writer_snapshot()
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertEqual(Source.objects.get(slug="fake").listings.count(), 8)
        self.assertGreaterEqual(stats["queued"], stats["completed"])
        self.assertGreater(stats["completed"], 0)

    def test_declared_total_gap_marks_source_partial(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None, request_timeout=None):
                self.max_pages = max_pages
                self.discovery_stats = {
                    "declared_total": 100,
                    "pages_seen": 4,
                    "urls_discovered": 10,
                    "coverage_ratio": 10.0,
                }

            def discover(self):
                return [f"https://example.com/{index}" for index in range(10)]

            def parse(self, url):
                number = url.rsplit("/", 1)[-1]
                return {
                    "external_id": number,
                    "url": url,
                    "title": f"Casa {number}",
                    "address": f"Calle {number} 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            job = create_scrape_job(["fake"], {"fake": 2})
            run_scrape_job(job.pk)

        source = job.sources.get(slug="fake")
        self.assertEqual(source.status, ScrapeJobSource.Status.PARTIAL)
        self.assertIn("Cobertura discovery: 10/100", source.logs)

    def test_cancel_during_discovery_stops_before_processing(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None, request_timeout=None, max_listings=None, should_cancel=None):
                self.should_cancel = should_cancel or (lambda: False)

            def discover(self):
                ScrapeJob.objects.update(cancel_requested=True)
                yield "https://example.com/1"

            def parse(self, url):
                raise AssertionError("No deberia procesar fichas despues de cancelar.")

        with patch("properties.services.scraping.get_adapter", side_effect=lambda *args, **kwargs: FakeAdapter(**kwargs)):
            job = create_scrape_job(["fake"], {"fake": 2})
            run_scrape_job(job.pk)

        job.refresh_from_db()
        source = job.sources.get(slug="fake")
        self.assertEqual(job.status, ScrapeJob.Status.CANCELLED)
        self.assertEqual(source.status, ScrapeJobSource.Status.CANCELLED)
        self.assertEqual(Source.objects.get(slug="fake").listings.count(), 0)
        self.assertIn("Cancelacion solicitada durante discovery", source.logs)

    def test_blocked_discovery_stops_source_without_marking_missing(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None, request_timeout=None, max_listings=None, should_cancel=None):
                pass

            def discover(self):
                raise RuntimeError("403 Client Error: Forbidden - CloudFront Request blocked")

            def parse(self, url):
                raise AssertionError("No deberia procesar fichas despues de un bloqueo en discovery.")

        with patch("properties.services.scraping.get_adapter", side_effect=lambda *args, **kwargs: FakeAdapter(**kwargs)):
            job = create_scrape_job(["fake"], {"fake": 2})
            run_scrape_job(job.pk)

        job.refresh_from_db()
        source = job.sources.get(slug="fake")
        self.assertEqual(job.status, ScrapeJob.Status.PARTIAL)
        self.assertEqual(source.status, ScrapeJobSource.Status.PARTIAL)
        self.assertIn("bloqueo 403/CDN durante discovery", source.logs)
        self.assertIn("No se marcan ausentes", source.logs)

    def test_consecutive_403_errors_trip_source_circuit_breaker(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None, request_timeout=None, max_listings=None, should_cancel=None):
                pass

            def discover(self):
                return [f"https://example.com/{index}" for index in range(20)]

            def parse(self, url):
                raise RuntimeError(f"403 Client Error: Forbidden for url: {url}")

        with patch("properties.services.scraping.get_adapter", side_effect=lambda *args, **kwargs: FakeAdapter(**kwargs)):
            job = create_scrape_job(["fake"], {"fake": 1})
            run_scrape_job(job.pk)

        source = job.sources.get(slug="fake")
        self.assertEqual(source.status, ScrapeJobSource.Status.PARTIAL)
        self.assertEqual(source.processed, 5)
        self.assertIn("Fuente detenida automaticamente por bloqueo 403/CDN", source.logs)
        self.assertIn("No se marcan ausentes", source.logs)

    def test_trial_mode_defaults_to_three_listings_and_skips_missing(self):
        Path(".scrape.lock").unlink(missing_ok=True)

        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None):
                self.max_pages = max_pages

            def discover(self):
                return [
                    "https://example.com/1",
                    "https://example.com/2",
                    "https://example.com/3",
                    "https://example.com/4",
                ]

            def parse(self, url):
                number = url.rsplit("/", 1)[-1]
                return {
                    "external_id": number,
                    "url": url,
                    "title": f"Casa {number}",
                    "address": f"Calle {number} 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            job = create_scrape_job(
                ["fake"],
                {"fake": 1},
                scrape_mode=ScrapeJob.Mode.TRIAL,
            )
            run_scrape_job(job.pk)

        job.refresh_from_db()
        source = job.sources.get(slug="fake")
        self.assertEqual(job.max_listings, 3)
        self.assertEqual(source.total_to_process, 3)
        self.assertIn("no se marcan ausentes", source.logs)
