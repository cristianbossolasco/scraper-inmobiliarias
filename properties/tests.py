import json
import re
from pathlib import Path
from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase

from properties.models import (
    Agency,
    GeocodeCache,
    Listing,
    ListingSnapshot,
    Property,
    PropertyLocation,
    ScrapeJob,
    ScrapeJobSource,
    ScrapeRun,
    Source,
)
from properties.services.ingestion import ingest_listing, mark_listing_removed, mark_missing
from properties.services.location_enrichment import clean_detected_address, enrich_location_data
from properties.services.agency_normalization import normalize_agency_name
from properties.services.data_quality import is_garage_like, is_rental_url, valid_price, valid_value
from properties.services.geocoding import Geocoder
from properties.services.normalization import (
    address_alias_variants,
    build_fingerprint,
    classify_address_precision,
    is_plausible_property_address,
    normalize_address,
    normalize_neighborhood_name,
    normalize_street_number_address,
    parse_decimal,
)
from properties.services.scraping import ActiveScrapeJobError, create_scrape_job, db_writer_snapshot, run_scrape_job
from properties.services.spatial import haversine_km, point_in_polygon
from properties.services.zone_inference import (
    apply_zone_inference,
    infer_property_zone,
    infer_zone_for_point,
    load_zone_index,
)
from properties.scrapers.argenprop import ArgenpropScraper
from properties.scrapers.base import ROBOTS_CACHE
from properties.scrapers.argencasas import ArgencasasScraper
from properties.scrapers.local_wordpress import (
    MiglieriniScraper,
    OdriozolaScraper,
    is_miglierini_detail_url,
    is_odriozola_detail_url,
)
from properties.scrapers.local_sites import AliagaScraper, BecerraScraper
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
    RemaxArgentinaScraper,
    RemaxDataworkScraper,
    RiquelmeScraper,
    Century21Scraper,
    ZonapropScraper,
)
from properties.scrapers.paginated import declared_total_from_text, max_page_from_markup
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
        self.assertEqual(
            normalize_address("Profesor Castagna al 4800"),
            "profesor castagna 4800",
        )
        self.assertEqual(
            normalize_street_number_address("Albariños al 1700"),
            "Albariños 1700",
        )

    def test_address_geocoding_cleanup_and_aliases(self):
        self.assertEqual(normalize_street_number_address("Rossini al 2000"), "Rossini 2000")
        self.assertEqual(normalize_street_number_address("solis al 2.800"), "solis 2800")
        self.assertEqual(normalize_street_number_address("GRANADA 500, Piso 0"), "GRANADA 500")
        self.assertEqual(normalize_street_number_address("Bonorino 634 , Piso 1"), "Bonorino 634")
        self.assertIn("José de Andonaegui 2600", address_alias_variants("J De Andonaegui 2600"))
        self.assertIn("Esteban Bonorino 634", address_alias_variants("Bonorino 634"))
        self.assertIn("Eduardo Acevedo 329", address_alias_variants("Acevedo Eduardo 329"))
        self.assertIn("Juan Díaz de Solís 700", address_alias_variants("Solis 700"))

    def test_decimal_formats(self):
        self.assertEqual(parse_decimal("USD 169.000"), Decimal("169000"))
        self.assertEqual(parse_decimal("151,50 m²"), Decimal("151.50"))

    def test_precision_classification(self):
        self.assertEqual(classify_address_precision("Gurruchaga 1733"), "exact")
        self.assertEqual(classify_address_precision("Gurruchaga al 1700"), "exact")
        self.assertEqual(classify_address_precision("Profesor Castagna al 4800"), "exact")
        self.assertEqual(classify_address_precision("Cañuelas y Villegas"), "intersection")
        self.assertEqual(classify_address_precision("Mascagni"), "street")


    def test_invalid_addresses_and_neighborhood_aliases(self):
        self.assertFalse(is_plausible_property_address("Ciudad: Hurlingham"))
        self.assertFalse(
            is_plausible_property_address(
                "Contacto Buscador de propiedades Click para llamar ahora"
            )
        )
        self.assertTrue(is_plausible_property_address("Uspallata, Hurlingham"))
        self.assertEqual(normalize_neighborhood_name("Barrio Ingles"), "Barrio Ingl\u00e9s")
        self.assertEqual(normalize_neighborhood_name("Ingl\u00e9s"), "Barrio Ingl\u00e9s")
        self.assertEqual(normalize_neighborhood_name("Morris"), "William C. Morris")
        self.assertEqual(normalize_neighborhood_name("5 esquinas, Hurlingham Centro"), "5 esquinas")
        self.assertEqual(
            normalize_neighborhood_name(
                "de perfil familiar, con accesos cercanos. Consultanos para conocer mas"
            ),
            "",
        )

    def test_fingerprint_falls_back_to_listing_identity_for_bad_address(self):
        source = Source(slug="guarnieri", name="Guarnieri", base_url="https://example.com")
        first = build_fingerprint(
            {
                "external_id": "casa-a",
                "url": "https://example.com/casa-a",
                "title": "Casa A",
                "address": "Ciudad: Hurlingham",
                "locality": "Hurlingham",
            },
            source=source,
        )
        second = build_fingerprint(
            {
                "external_id": "casa-b",
                "url": "https://example.com/casa-b",
                "title": "Casa B",
                "address": "Ciudad: Hurlingham",
                "locality": "Hurlingham",
            },
            source=source,
        )
        self.assertNotEqual(first, second)

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
            "Pérez Galdós 1100",
        )
        self.assertEqual(
            clean_detected_address("Dirección: Profesor Castagna al 4800"),
            "Profesor Castagna 4800",
        )
        self.assertEqual(
            clean_detected_address(
                "Las Araucarias 1900 Hurlingham barrio los troncos, Partido de Hurlingham, Buenos Aires"
            ),
            "Las Araucarias 1900",
        )
        self.assertEqual(
            clean_detected_address(
                "Acevedo Eduardo 329, Hurlingham, Partido de Hurlingham, Buenos Aires, 1686S, Argentina"
            ),
            "Acevedo Eduardo 329",
        )
        self.assertEqual(
            clean_detected_address("Valentín Alsina 2243 - Barrio Cartero"),
            "Valentín Alsina 2243",
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


class ZoneInferenceTests(TestCase):
    def _geojson_path(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "zones.geojson"

        def polygon(name, ring):
            return {
                "type": "Feature",
                "properties": {
                    "@id": "relation/100",
                    "name": name,
                    "type": "boundary",
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }

        def line(relation_id, name, coords):
            return {
                "type": "Feature",
                "properties": {
                    "@id": f"way/{relation_id}-{len(coords)}",
                    "@relations": [
                        {
                            "role": "",
                            "rel": relation_id,
                            "reltags": {
                                "admin_level": "9",
                                "boundary": "administrative",
                                "name": name,
                                "type": "boundary",
                            },
                        }
                    ],
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }

        direct_ring = [
            [-58.6410, -34.6010],
            [-58.6400, -34.6010],
            [-58.6400, -34.6000],
            [-58.6410, -34.6000],
            [-58.6410, -34.6010],
        ]
        relation_ring = [
            [-58.6430, -34.6030],
            [-58.6420, -34.6030],
            [-58.6420, -34.6020],
            [-58.6430, -34.6020],
            [-58.6430, -34.6030],
        ]
        features = [
            polygon("Barrio Ingles", direct_ring),
            line(200, "Cartero", [relation_ring[0], relation_ring[1]]),
            line(200, "Cartero", [relation_ring[1], relation_ring[2]]),
            line(200, "Cartero", [relation_ring[2], relation_ring[3]]),
            line(200, "Cartero", [relation_ring[3], relation_ring[4]]),
            line(300, "Incompleto", [[-58.645, -34.605], [-58.644, -34.605]]),
        ]
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": features}),
            encoding="utf-8",
        )
        return path

    def test_zone_point_strict_boundary_nearest_and_no_match(self):
        path = self._geojson_path()

        inside = infer_zone_for_point(-34.6005, -58.6405, path, max_distance_m=100)
        self.assertEqual(inside["zone"], "Barrio Ingl\u00e9s")
        self.assertEqual(inside["method"], "polygon")

        boundary = infer_zone_for_point(-34.6000, -58.6405, path, max_distance_m=100)
        self.assertEqual(boundary["zone"], "Barrio Ingl\u00e9s")
        self.assertEqual(boundary["method"], "polygon")

        nearby = infer_zone_for_point(-34.5997, -58.6405, path, max_distance_m=100)
        self.assertEqual(nearby["zone"], "Barrio Ingl\u00e9s")
        self.assertEqual(nearby["method"], "nearest")

        far = infer_zone_for_point(-34.5980, -58.6405, path, max_distance_m=20)
        self.assertEqual(far["zone"], "")
        self.assertEqual(far["method"], "no_match")

    def test_zone_loader_rebuilds_closed_osm_relation_and_reports_incomplete(self):
        index = load_zone_index(self._geojson_path())
        names = {polygon.name for polygon in index.polygons}
        self.assertIn("Barrio Cartero", names)
        self.assertIn("300", index.skipped_relations)

        result = infer_zone_for_point(-34.6025, -58.6425, self._geojson_path())
        self.assertEqual(result["zone"], "Barrio Cartero")

    def test_inference_uses_cached_geocode_without_external_call(self):
        path = self._geojson_path()
        property_obj = Property.objects.create(
            fingerprint="zone-cache-1",
            title="Casa con cache",
            address="Test 123",
            locality="Hurlingham",
        )
        query = Geocoder().build_query(property_obj)
        GeocodeCache.objects.create(
            query=query,
            latitude=-34.6005,
            longitude=-58.6405,
            precision="exact",
            confidence=0.8,
            provider_payload={},
        )

        result = infer_property_zone(property_obj, geojson_path=path)

        self.assertEqual(result.inferred_neighborhood, "Barrio Ingl\u00e9s")
        self.assertEqual(result.geocoding_status, "cache_hit")
        property_obj.refresh_from_db()
        self.assertTrue(hasattr(property_obj, "location"))

    def test_inference_does_not_call_external_geocoder_without_flag(self):
        class CacheOnlyGeocoder:
            external_called = False

            def build_query(self, property_obj):
                return "Sin cache 123, Hurlingham, Buenos Aires, Argentina"

            def geocode_property_from_cache(self, property_obj):
                return None

            def geocode_property(self, property_obj):
                self.external_called = True
                raise AssertionError("external geocoder should not be called")

        geocoder = CacheOnlyGeocoder()
        property_obj = Property.objects.create(
            fingerprint="zone-cache-miss",
            title="Casa sin cache",
            address="Sin cache 123",
            locality="Hurlingham",
        )

        result = infer_property_zone(
            property_obj,
            geojson_path=self._geojson_path(),
            geocoder=geocoder,
            geocode_missing=False,
        )

        self.assertEqual(result.geocoding_status, "cache_miss")
        self.assertEqual(result.method, "no_coordinates")
        self.assertFalse(geocoder.external_called)

    def test_inference_preserves_source_zone_and_marks_conflict(self):
        property_obj = Property.objects.create(
            fingerprint="zone-conflict",
            title="Casa con conflicto",
            neighborhood="Villa Club",
        )
        PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.6005,
            longitude=-58.6405,
            precision=PropertyLocation.Precision.EXACT,
            provider="source",
            confidence=1,
        )

        result = infer_property_zone(property_obj, geojson_path=self._geojson_path())
        apply_zone_inference(property_obj, result)

        property_obj.refresh_from_db()
        self.assertEqual(property_obj.neighborhood, "Villa Club")
        self.assertEqual(property_obj.inferred_neighborhood, "Barrio Ingl\u00e9s")
        self.assertTrue(property_obj.zone_conflict)

    def test_infer_zones_command_dry_run_and_apply(self):
        property_obj = Property.objects.create(
            fingerprint="zone-command",
            title="Casa comando",
        )
        PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.6005,
            longitude=-58.6405,
            precision=PropertyLocation.Precision.EXACT,
            provider="source",
            confidence=1,
        )
        path = self._geojson_path()

        output = StringIO()
        call_command(
            "infer_zones",
            "--dry-run",
            "--property-id",
            str(property_obj.pk),
            "--geojson",
            str(path),
            stdout=output,
        )
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.inferred_neighborhood, "")
        self.assertIn("dry-run", output.getvalue())

        call_command(
            "infer_zones",
            "--apply",
            "--property-id",
            str(property_obj.pk),
            "--geojson",
            str(path),
            stdout=StringIO(),
        )
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.inferred_neighborhood, "Barrio Ingl\u00e9s")


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

    def test_ingestion_normalizes_street_number_addresses(self):
        listing, _ = ingest_listing(
            self.source,
            dict(
                self.data,
                external_id="abc-street-number",
                url="https://example.com/abc-street-number",
                address="Profesor Castagna al 4800",
                latitude=None,
                longitude=None,
            ),
        )
        listing.property.refresh_from_db()
        self.assertEqual(listing.property.address, "Profesor Castagna 4800")
        self.assertEqual(listing.property.detected_address, "Profesor Castagna 4800")
        self.assertEqual(listing.property.normalized_address, "profesor castagna 4800")

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

    def test_mark_listing_removed_deactivates_only_source_listing(self):
        listing, _ = ingest_listing(self.source, self.data)
        removed = mark_listing_removed(self.source, url=listing.url)
        self.assertEqual(removed.pk, listing.pk)

        listing.refresh_from_db()
        listing.property.refresh_from_db()
        self.assertFalse(listing.active)
        self.assertEqual(listing.source_status, "removed")
        self.assertEqual(listing.missing_runs, 2)
        self.assertEqual(listing.property.status, Property.Status.REMOVED)

    def test_mark_listing_removed_keeps_property_active_with_other_listing(self):
        other_source = Source.objects.create(
            slug="other-active", name="Other Active", base_url="https://other.example"
        )
        listing, _ = ingest_listing(self.source, self.data)
        other_listing, _ = ingest_listing(
            other_source,
            dict(
                self.data,
                external_id="abc-other-active",
                url="https://other.example/abc",
            ),
        )
        self.assertEqual(listing.property_id, other_listing.property_id)

        mark_listing_removed(self.source, url=listing.url)

        listing.refresh_from_db()
        other_listing.refresh_from_db()
        listing.property.refresh_from_db()
        self.assertFalse(listing.active)
        self.assertTrue(other_listing.active)
        self.assertEqual(listing.property.status, Property.Status.ACTIVE)

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
            "Bizet 1900, Hurlingham, Buenos Aires, Argentina",
        )

    def test_geocoder_query_normalizes_street_number_address(self):
        property_obj = Property.objects.create(
            fingerprint="geo-street-number",
            title="Casa con direccion al",
            address="Profesor Castagna al 4800",
            locality="Hurlingham",
        )
        self.assertEqual(
            Geocoder().build_query(property_obj),
            "Profesor Castagna 4800, Hurlingham, Buenos Aires, Argentina",
        )

    def test_geocoder_candidates_use_aliases_and_canonical_locality(self):
        property_obj = Property.objects.create(
            fingerprint="geo-candidates",
            title="Local con alias",
            address="NECOCHEA 1300",
            locality="Barrio Ingles",
        )
        candidates = Geocoder().query_candidates(property_obj)
        self.assertIn(
            "NECOCHEA 1300, Hurlingham, Buenos Aires, Argentina",
            candidates,
        )
        self.assertIn(
            "General Mariano Necochea 1300, Hurlingham, Buenos Aires, Argentina",
            candidates,
        )

    def test_geocoder_candidates_clean_embedded_barrio_and_postal_suffixes(self):
        araucarias = Property.objects.create(
            fingerprint="geo-candidates-araucarias",
            title="Casa Los Troncos",
            address="Las Araucarias 1900 Hurlingham barrio los troncos, Partido de Hurlingham, Buenos Aires",
            locality="Hurlingham",
        )
        self.assertEqual(
            Geocoder().build_query(araucarias),
            "Las Araucarias 1900, Hurlingham, Buenos Aires, Argentina",
        )

        acevedo = Property.objects.create(
            fingerprint="geo-candidates-acevedo",
            title="Casa Villa Club",
            address="Acevedo Eduardo 329, Hurlingham, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            locality="William C. Morris",
        )
        candidates = Geocoder().query_candidates(acevedo)
        self.assertIn(
            "Eduardo Acevedo 329, William C. Morris, Buenos Aires, Argentina",
            candidates,
        )
        self.assertNotIn("1686S", " ".join(candidates))

    def test_geocoder_uses_clean_alias_cache_after_negative_old_query(self):
        property_obj = Property.objects.create(
            fingerprint="geo-alias-cache",
            title="Casa con alias",
            address="J De Andonaegui 2600",
            locality="Hurlingham",
        )
        GeocodeCache.objects.create(
            query="J De Andonaegui 2600, Hurlingham, Buenos Aires, Argentina",
            latitude=None,
            longitude=None,
            precision="",
            confidence=0,
        )
        GeocodeCache.objects.create(
            query="José de Andonaegui 2600, Hurlingham, Buenos Aires, Argentina",
            latitude=-34.588,
            longitude=-58.666,
            precision="exact",
            confidence=0.8,
        )

        location = Geocoder().geocode_property_from_cache(property_obj)

        self.assertIsNotNone(location)
        self.assertAlmostEqual(location.latitude, -34.588)
        self.assertEqual(location.provider, "nominatim")

    def test_geocoder_local_reference_fallback(self):
        reference = Property.objects.create(
            fingerprint="geo-local-ref-source",
            title="Referencia",
            address="Rossini 2000",
            locality="Hurlingham",
        )
        PropertyLocation.objects.create(
            property=reference,
            latitude=-34.59,
            longitude=-58.64,
            precision=PropertyLocation.Precision.EXACT,
            provider="source",
            confidence=1,
        )
        property_obj = Property.objects.create(
            fingerprint="geo-local-ref-target",
            title="Objetivo",
            address="Rossini 2010",
            locality="Hurlingham",
        )

        location = Geocoder().geocode_property_from_cache(property_obj)

        self.assertIsNotNone(location)
        self.assertEqual(location.provider, "local_reference")
        self.assertEqual(location.precision, PropertyLocation.Precision.EXACT)
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.location_evidence["local_reference"]["property_id"], reference.pk)

    def test_geocoder_local_reference_ignores_outside_target_references(self):
        outside = Property.objects.create(
            fingerprint="geo-local-ref-outside",
            title="Referencia fuera",
            address="Solis 700",
            locality="Hurlingham",
        )
        PropertyLocation.objects.create(
            property=outside,
            latitude=-34.77,
            longitude=-58.39,
            precision=PropertyLocation.Precision.EXACT,
            provider="nominatim",
            confidence=0.5,
            outside_target=True,
        )
        inside = Property.objects.create(
            fingerprint="geo-local-ref-inside",
            title="Referencia adentro",
            address="Juan Díaz de Solís 700",
            locality="Hurlingham",
        )
        PropertyLocation.objects.create(
            property=inside,
            latitude=-34.595,
            longitude=-58.631,
            precision=PropertyLocation.Precision.EXACT,
            provider="nominatim",
            confidence=0.8,
        )
        property_obj = Property.objects.create(
            fingerprint="geo-local-ref-solis-target",
            title="Objetivo",
            address="Solis 700",
            locality="Hurlingham",
        )

        location = Geocoder().geocode_property_from_cache(property_obj)

        self.assertIsNotNone(location)
        self.assertAlmostEqual(location.latitude, -34.595)
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.location_evidence["local_reference"]["property_id"], inside.pk)

    def test_geocoder_force_refreshes_non_manual_location(self):
        property_obj = Property.objects.create(
            fingerprint="geo-force-1",
            title="Casa con ubicacion vieja",
            address="Profesor Castagna al 4800",
            locality="Hurlingham",
        )
        PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.5,
            longitude=-58.5,
            precision=PropertyLocation.Precision.EXACT,
            provider="source",
            confidence=1,
        )
        query = "Profesor Castagna 4800, Hurlingham, Buenos Aires, Argentina"
        GeocodeCache.objects.create(
            query=query,
            latitude=-34.596,
            longitude=-58.654,
            precision="exact",
            confidence=0.8,
            provider_payload={},
        )

        location = Geocoder().geocode_property(property_obj, force=True)

        self.assertEqual(location.query, query)
        self.assertEqual(location.provider, "nominatim")
        self.assertAlmostEqual(location.latitude, -34.596)

    def test_geocoder_force_preserves_manual_location(self):
        property_obj = Property.objects.create(
            fingerprint="geo-manual-1",
            title="Casa con ubicacion manual",
            address="Profesor Castagna al 4800",
            locality="Hurlingham",
        )
        manual_location = PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.5,
            longitude=-58.5,
            precision=PropertyLocation.Precision.MANUAL,
            provider="manual",
            confidence=1,
            manually_corrected=True,
        )

        location = Geocoder().geocode_property(property_obj, force=True)

        self.assertEqual(location.pk, manual_location.pk)
        self.assertEqual(location.provider, "manual")

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

    @patch("properties.management.commands.repair_addresses.Geocoder")
    def test_repair_addresses_normalizes_and_geocodes_changed_only(self, geocoder_cls):
        changed = Property.objects.create(
            fingerprint="repair-address-1",
            title="Casa con direccion al",
            address="Profesor Castagna al 4800",
            normalized_address="profesor castagna al 4800",
            detected_address="Profesor Castagna al 4800",
            locality="Hurlingham",
        )
        Property.objects.create(
            fingerprint="repair-address-2",
            title="Casa sin cambio",
            address="Ocampo 1900",
            normalized_address="ocampo 1900",
            detected_address="Ocampo 1900",
            locality="Hurlingham",
        )
        piso = Property.objects.create(
            fingerprint="repair-address-piso",
            title="Casa con piso",
            address="GRANADA 500, Piso 0",
            normalized_address="granada 500 piso 0",
            detected_address="GRANADA 500, Piso 0",
            locality="Hurlingham",
        )
        rossini = Property.objects.create(
            id=2987,
            fingerprint="repair-address-known-rossini",
            title="Casa sin direccion Becerra",
            address="",
            normalized_address="",
            detected_address="",
            locality="Hurlingham",
        )
        geocoder_cls.return_value.geocode_property.return_value = True

        output = StringIO()
        call_command("repair_addresses", "--dry-run", stdout=output)
        changed.refresh_from_db()
        self.assertEqual(changed.address, "Profesor Castagna al 4800")
        self.assertIn("3 direcciones corregidas (dry-run)", output.getvalue())
        self.assertFalse(geocoder_cls.return_value.geocode_property.called)

        output = StringIO()
        call_command("repair_addresses", "--geocode", stdout=output)
        changed.refresh_from_db()
        piso.refresh_from_db()
        rossini.refresh_from_db()
        self.assertEqual(changed.address, "Profesor Castagna 4800")
        self.assertEqual(changed.detected_address, "Profesor Castagna 4800")
        self.assertEqual(changed.normalized_address, "profesor castagna 4800")
        self.assertEqual(piso.address, "GRANADA 500")
        self.assertEqual(piso.detected_address, "GRANADA 500")
        self.assertEqual(rossini.address, "Rossini 2000")
        self.assertEqual(rossini.detected_address, "Rossini 2000")
        self.assertEqual(geocoder_cls.return_value.geocode_property.call_count, 3)
        self.assertIn("3 propiedades geolocalizadas", output.getvalue())

    @patch("properties.management.commands.repair_addresses.Geocoder")
    def test_repair_addresses_applies_curated_guarnieri_corrections(self, geocoder_cls):
        Property.objects.create(
            id=4409,
            fingerprint="repair-address-araucarias",
            title="Chalet Los Troncos",
            address="Las Araucarias 1900 Hurlingham barrio los troncos, Partido de Hurlingham, Buenos Aires",
            detected_address="Las Araucarias 1900 Hurlingham barrio los troncos, Partido de Hurlingham, Buenos Aires",
            locality="Hurlingham",
        )
        Property.objects.create(
            id=4548,
            fingerprint="repair-address-solis",
            title="Complejo Solis",
            address="Juan Diaz de Solís, William C. Morris, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            detected_address="Juan Diaz de Solís, William C. Morris, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            locality="Hurlingham",
        )
        Property.objects.create(
            id=4571,
            fingerprint="repair-address-acevedo",
            title="Casa Villa Club",
            address="Acevedo Eduardo 329, Hurlingham, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            detected_address="Acevedo Eduardo 329, Hurlingham, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            locality="Hurlingham",
            neighborhood="Villa Club",
        )
        geocoder_cls.return_value.geocode_property.return_value = True

        output = StringIO()
        call_command(
            "repair_addresses",
            "--property-id",
            "4409",
            "--property-id",
            "4548",
            "--property-id",
            "4571",
            "--geocode",
            stdout=output,
        )

        araucarias = Property.objects.get(pk=4409)
        solis = Property.objects.get(pk=4548)
        acevedo = Property.objects.get(pk=4571)
        self.assertEqual(araucarias.address, "Las Araucarias 1900")
        self.assertEqual(araucarias.neighborhood, "Los Troncos")
        self.assertEqual(solis.address, "Juan Díaz de Solís 1686")
        self.assertEqual(solis.locality, "William C. Morris")
        self.assertEqual(acevedo.address, "Eduardo Acevedo 329")
        self.assertEqual(acevedo.locality, "William C. Morris")
        self.assertEqual(geocoder_cls.return_value.geocode_property.call_count, 3)

    def test_repair_addresses_preserves_location_when_only_metadata_changes(self):
        property_obj = Property.objects.create(
            fingerprint="repair-address-preserve-pin",
            title="Casa con codigo postal pegado",
            address="Bizet 2663, Hurlingham, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            detected_address="Bizet 2663, Hurlingham, Partido de Hurlingham, Buenos Aires, 1686S, Argentina",
            locality="Hurlingham",
        )
        location = PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.57,
            longitude=-58.64,
            precision=PropertyLocation.Precision.EXACT,
            provider="nominatim",
            confidence=0.8,
        )

        output = StringIO()
        call_command("repair_addresses", "--property-id", str(property_obj.pk), stdout=output)

        property_obj.refresh_from_db()
        self.assertEqual(property_obj.address, "Bizet 2663")
        self.assertTrue(PropertyLocation.objects.filter(pk=location.pk).exists())

    def test_repair_addresses_moves_embedded_barrio_to_neighborhood(self):
        property_obj = Property.objects.create(
            fingerprint="repair-address-barrio",
            title="Casa con barrio pegado",
            address="Valentín Alsina 2243 - Barrio Cartero",
            detected_address="Valentín Alsina 2243 - Barrio Cartero",
            locality="Hurlingham",
        )

        output = StringIO()
        call_command("repair_addresses", "--property-id", str(property_obj.pk), stdout=output)

        property_obj.refresh_from_db()
        self.assertEqual(property_obj.address, "Valentín Alsina 2243")
        self.assertEqual(property_obj.neighborhood, "Barrio Cartero")

    @patch("properties.management.commands.repair_merged_listings.get_adapter")
    def test_repair_merged_listings_splits_invalid_address_cluster(self, get_adapter):
        source = Source.objects.create(
            slug="guarnieri", name="Guarnieri", base_url="https://guarnieri.example"
        )
        property_obj = Property.objects.create(
            fingerprint="merged-guarnieri",
            title="Fusion erronea",
            address="Ciudad: Hurlingham",
            locality="Hurlingham",
        )
        first = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="casa-a",
            url="https://guarnieri.example/casa-a",
        )
        second = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="casa-b",
            url="https://guarnieri.example/casa-b",
        )

        class FakeAdapter:
            def parse(self, url):
                if url.endswith("casa-a"):
                    return {
                        "external_id": "casa-a",
                        "url": url,
                        "title": "Casa A",
                        "address": "Necochea 900",
                        "locality": "Hurlingham",
                        "property_type": "house",
                        "currency": "USD",
                        "price": 100000,
                    }
                return {
                    "external_id": "casa-b",
                    "url": url,
                    "title": "Casa B",
                    "address": "Padre Torello 2600",
                    "locality": "Hurlingham",
                    "property_type": "house",
                    "currency": "USD",
                    "price": 120000,
                }

        get_adapter.return_value = FakeAdapter()

        output = StringIO()
        call_command(
            "repair_merged_listings",
            "--property-id",
            str(property_obj.pk),
            "--source",
            "guarnieri",
            stdout=output,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        property_obj.refresh_from_db()
        self.assertEqual(first.property_id, property_obj.pk)
        self.assertNotEqual(second.property_id, property_obj.pk)
        self.assertEqual(property_obj.address, "Necochea 900")
        self.assertEqual(second.property.address, "Padre Torello 2600")
        self.assertIn("1 publicaciones separadas", output.getvalue())


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
        self.assertContains(response, "Ver publicacion original")
        self.assertContains(response, 'class="original-cta"')
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

    def test_neighborhood_filter_uses_inferred_zone_as_fallback(self):
        listing, _ = ingest_listing(
            self.listing.source,
            {
                "external_id": "inferred-zone",
                "url": "https://example.com/inferred-zone",
                "title": "Casa con zona inferida",
                "address": "Uspallata",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 100000,
            },
        )
        listing.property.inferred_neighborhood = "Barrio Ingl\u00e9s"
        listing.property.save(update_fields=["inferred_neighborhood"])

        response = self.client.get("/", {"neighborhood": "Barrio Ingl\u00e9s"})

        self.assertContains(response, "Casa con zona inferida")

    def test_detail_allows_manual_location_without_existing_pin(self):
        listing, _ = ingest_listing(
            self.listing.source,
            {
                "external_id": "no-pin",
                "url": "https://example.com/no-pin",
                "title": "Casa sin pin",
                "address": "Uspallata",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 100000,
            },
        )
        response = self.client.get(f"/propiedad/{listing.property_id}/")
        self.assertContains(response, "Ubicar manualmente")
        self.assertContains(response, '"has_location": false')

        response = self.client.post(
            f"/api/propiedad/{listing.property_id}/ubicacion/",
            data=json.dumps({"latitude": -34.591, "longitude": -58.641}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        listing.property.refresh_from_db()
        self.assertEqual(listing.property.location.provider, "manual")
        self.assertTrue(listing.property.location.manually_corrected)

    def test_detail_collapses_equal_price_history_segments(self):
        listing, _ = ingest_listing(
            self.listing.source,
            {
                "external_id": "history",
                "url": "https://example.com/history",
                "title": "Casa historial",
                "description": "Version uno",
                "address": "Guardia Vieja",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 100000,
            },
        )
        ingest_listing(
            self.listing.source,
            {
                "external_id": "history",
                "url": "https://example.com/history",
                "title": "Casa historial",
                "description": "Version dos",
                "address": "Guardia Vieja",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 100000,
            },
        )
        response = self.client.get(f"/propiedad/{listing.property_id}/")
        self.assertContains(response, "2 registros iguales")

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

    def test_table_column_filters_and_multi_sort(self):
        ingest_listing(
            self.listing.source,
            {
                "external_id": "web-cheap",
                "url": "https://example.com/web-cheap",
                "title": "Depto chico",
                "address": "Paso 1200",
                "locality": "Hurlingham",
                "property_type": "apartment",
                "currency": "USD",
                "price": 80000,
                "bedrooms": 1,
                "covered_area": 45,
                "agency": "Beta Propiedades",
            },
        )

        response = self.client.get("/", {"view": "table", "sort": "price,-title"})
        content = response.content.decode()
        self.assertContains(response, 'id="table-filter-form"')
        self.assertContains(response, 'name="title"')
        self.assertContains(response, 'name="price_m2_min"')
        self.assertLess(content.index("Depto chico"), content.index("Casa con pileta"))
        self.assertIn("sort=-price%2C-title", content)

        response = self.client.get("/", {"view": "table", "title": "pileta"})
        self.assertContains(response, "Casa con pileta")
        self.assertNotContains(response, "Depto chico")

    def test_table_pagination_can_jump_to_last_or_specific_page(self):
        for index in range(30):
            ingest_listing(
                self.listing.source,
                {
                    "external_id": f"page-{index}",
                    "url": f"https://example.com/page-{index}",
                    "title": f"Paginada {index}",
                    "address": f"San Martin {index}",
                    "locality": "Hurlingham",
                    "property_type": "house",
                    "currency": "USD",
                    "price": 50000 + index,
                },
            )

        response = self.client.get("/", {"view": "table", "sort": "price"})
        self.assertContains(response, 'class="pagination-jump"')
        self.assertContains(response, 'aria-label="Ultima pagina"')
        self.assertContains(response, 'max="2"')

        response = self.client.get("/", {"view": "table", "sort": "price", "page": "2"})
        self.assertContains(response, "Paginada 29")

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

    def test_neighborhood_filter_uses_declared_zone(self):
        ingest_listing(
            self.listing.source,
            {
                "external_id": "web-zone",
                "url": "https://example.com/web-zone",
                "title": "Departamento en Villa Club",
                "address": "Aconcagua 1600",
                "locality": "Hurlingham",
                "neighborhood": "Villa Club",
                "property_type": "apartment",
                "currency": "USD",
                "price": 120000,
            },
        )
        response = self.client.get("/", {"neighborhood": "Villa Club"})
        self.assertContains(response, "Departamento en Villa Club")
        self.assertContains(response, "Villa Club")
        self.assertNotContains(response, "Casa con pileta")

    def test_neighborhood_filter_is_searchable_multiselect_and_canonical(self):
        listing, _ = ingest_listing(
            self.listing.source,
            {
                "external_id": "web-barrio-ingles",
                "url": "https://example.com/web-barrio-ingles",
                "title": "Casa barrio ingles",
                "address": "Necochea 900",
                "locality": "Hurlingham",
                "neighborhood": "Barrio Ingles",
                "property_type": "house",
                "currency": "USD",
                "price": 130000,
            },
        )
        listing.property.neighborhood = "Barrio Ingles"
        listing.property.save(update_fields=["neighborhood"])

        response = self.client.get("/")
        self.assertContains(response, 'id="zone-search"')
        self.assertContains(response, 'class="zone-selected"')
        self.assertContains(response, 'src="/static/js/zone-filter.js"')

        response = self.client.get("/", {"neighborhood": "Barrio Ingl\u00e9s"})
        self.assertContains(response, "Casa barrio ingles")

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

    def test_scraping_api_blocks_new_job_when_one_is_active(self):
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        active = ScrapeJob.objects.create(
            status=ScrapeJob.Status.RUNNING,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
        )
        ScrapeJobSource.objects.create(
            job=active,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            status=ScrapeJobSource.Status.RUNNING,
        )

        with patch("properties.views.start_scrape_job") as starter:
            response = self.client.post(
                "/api/scraping/jobs/",
                data=json.dumps(
                    {
                        "sources": ["mapaprop"],
                        "workers": {"mapaprop": 1},
                        "scrape_mode": "trial",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 409)
        starter.assert_not_called()
        payload = response.json()
        self.assertEqual(payload["id"], active.pk)
        self.assertIn(f"Job #{active.pk}", payload["error"])
        self.assertEqual(ScrapeJob.objects.count(), 1)

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

    def test_scraping_api_blocks_retry_while_another_job_is_active(self):
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        active = ScrapeJob.objects.create(
            status=ScrapeJob.Status.RUNNING,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
        )
        ScrapeJobSource.objects.create(
            job=active,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            status=ScrapeJobSource.Status.RUNNING,
        )
        finished = ScrapeJob.objects.create(
            status=ScrapeJob.Status.PARTIAL,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
        )
        ScrapeJobSource.objects.create(
            job=finished,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            status=ScrapeJobSource.Status.PARTIAL,
            error_urls=[{"url": "https://example.com/bad", "error": "timeout"}],
        )

        with patch("properties.views.start_scrape_job") as starter:
            retry_response = self.client.post(f"/api/scraping/jobs/{finished.pk}/retry/")
            retry_errors_response = self.client.post(f"/api/scraping/jobs/{finished.pk}/retry-errors/")

        self.assertEqual(retry_response.status_code, 409)
        self.assertEqual(retry_errors_response.status_code, 409)
        starter.assert_not_called()
        self.assertEqual(retry_response.json()["id"], active.pk)
        self.assertEqual(retry_errors_response.json()["id"], active.pk)
        self.assertEqual(ScrapeJob.objects.count(), 2)

    def test_scraping_api_exposes_elapsed_and_retries_error_urls(self):
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        original = ScrapeJob.objects.create(
            status=ScrapeJob.Status.PARTIAL,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 2},
            scrape_mode=ScrapeJob.Mode.COMPLETE,
        )
        ScrapeJobSource.objects.create(
            job=original,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            workers=2,
            status=ScrapeJobSource.Status.PARTIAL,
            error_urls=[
                {
                    "url": "https://example.com/bad",
                    "error": "timeout",
                    "timestamp": "2026-06-11T01:00:00-03:00",
                }
            ],
        )

        response = self.client.get(f"/api/scraping/jobs/{original.pk}/")
        payload = response.json()
        self.assertIn("elapsed_seconds", payload)
        self.assertEqual(payload["sources"][0]["error_urls"][0]["url"], "https://example.com/bad")

        with patch("properties.views.start_scrape_job") as starter:
            response = self.client.post(f"/api/scraping/jobs/{original.pk}/retry-errors/")

        self.assertEqual(response.status_code, 201)
        starter.assert_called_once()
        retried = ScrapeJob.objects.exclude(pk=original.pk).get()
        self.assertEqual(retried.selected_sources, ["mapaprop"])
        self.assertEqual(retried.worker_config, {"mapaprop": 2})
        self.assertEqual(retried.retry_urls, {"mapaprop": ["https://example.com/bad"]})
        self.assertEqual(retried.scrape_mode, ScrapeJob.Mode.TRIAL)


class ScraperParserTests(TestCase):
    def parse_with_fixture(self, scraper_cls, fixture_name, url):
        scraper = scraper_cls()
        scraper.soup = lambda parsed_url: fixture_soup(fixture_name)
        return scraper.parse(url)

    def test_becerra_parser_extracts_unlabeled_address(self):
        data = self.parse_with_fixture(
            BecerraScraper,
            "becerra_detail_rossini.html",
            "https://becerrapropiedades.com/ficha/6768701",
        )
        self.assertEqual(data["address"], "Rossini 2000")
        self.assertEqual(data["locality"], "Hurlingham")
        self.assertEqual(data["location_precision"], "exact")

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

    def test_argencasas_discovery_uses_locality_sale_pagination(self):
        scraper = ArgencasasScraper()

        def fake_soup(url):
            if "page=2" in url:
                return fixture_soup("argencasas_listing_page2.html")
            if "page=" in url:
                return BeautifulSoup("<html><body></body></html>", "lxml")
            return fixture_soup("argencasas_listing.html")

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(
            urls,
            [
                "https://www.argencasas.com/propiedad-casa-venta-hurlingham-301-1001",
                "https://www.argencasas.com/propiedad-local-venta-villa-club-304-1002",
                "https://www.argencasas.com/propiedad-galpon-venta-parque-johnston-305-1003",
            ],
        )
        self.assertEqual(scraper.discovery_stats["declared_total"], 662)
        self.assertEqual(scraper.discovery_stats["pages_seen"], 5)
        self.assertEqual(scraper.discovery_stats["coverage_ratio"], 0.5)

    def test_argencasas_parser_captures_zone(self):
        data = self.parse_with_fixture(
            ArgencasasScraper,
            "argencasas_detail.html",
            "https://www.argencasas.com/propiedad-departamento-venta-villa-club-304-1394",
        )
        self.assertEqual(data["neighborhood"], "Villa Club")
        self.assertEqual(data["raw_data"]["argencasas_zone"], "Villa Club")
        self.assertEqual(data["property_type"], Property.Type.APARTMENT)

    def test_argencasas_parser_reads_labeled_metrics(self):
        data = self.parse_with_fixture(
            ArgencasasScraper,
            "argencasas_metric_detail.html",
            "https://www.argencasas.com/propiedad-casa-venta-villa-alemania-309-358",
        )
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("78000"))
        self.assertEqual(data["rooms"], 3)
        self.assertEqual(data["bedrooms"], 2)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["covered_area"], Decimal("76"))
        self.assertEqual(data["total_area"], Decimal("92"))
        self.assertEqual(data["uncovered_area"], Decimal("10"))
        self.assertEqual(data["age_years"], 45)

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

    def test_mapaprop_discovery_uses_offsets_and_declared_total(self):
        scraper = MapapropScraper()
        calls = []

        def fake_soup(url):
            calls.append(url)
            if "from_12" in url:
                return fixture_soup("mapaprop_listing_page2.html")
            if "from_0" in url:
                return fixture_soup("mapaprop_listing.html")
            return BeautifulSoup("<html><body></body></html>", "lxml")

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(
            urls,
            [
                "https://www.mapaprop.com/en/property/venta-de-casa-en-hurlingham-1001/hash",
                "https://www.mapaprop.com/en/property/venta-de-local-comercial-en-hurlingham-1002/hash",
            ],
        )
        self.assertIn("from_12", calls[1])
        self.assertEqual(scraper.discovery_stats["declared_total"], 393)
        self.assertEqual(scraper.discovery_stats["pages_seen"], 5)
        self.assertEqual(scraper.discovery_stats["coverage_ratio"], 0.5)

    def test_mapaprop_keeps_commercial_listings(self):
        data = self.parse_with_fixture(
            MapapropScraper,
            "mapaprop_commercial_detail.html",
            "https://www.mapaprop.com/en/property/venta-de-local-comercial-en-hurlingham-1002/hash",
        )
        self.assertIsNotNone(data)
        self.assertEqual(data["property_type"], Property.Type.OTHER)
        self.assertEqual(data["price"], Decimal("90000"))

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
        self.assertEqual(data["address"], "Pérez Galdós 1100")

    def test_analia_fernandez_structured_tables(self):
        data = self.parse_with_fixture(
            AnaliaFernandezScraper,
            "analia_fernandez_full_detail.html",
            "https://www.fernandezpropiedades.com.ar/p/4743235-Casa-en-Venta-en-Hurlingham-Diego-Carabajal-al-500",
        )
        self.assertEqual(data["address"], "Diego Carabajal 500")
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

    def test_marcelo_russo_semicovered_garage_text(self):
        data = self.parse_with_fixture(
            MarceloRussoScraper,
            "marcelo_russo_semicovered_garage_detail.html",
            "https://marcelorussoprop.com.ar/property/2822-hurlingham/",
        )
        self.assertEqual(data["garages"], 1)

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

    def test_guarnieri_splits_embedded_barrio_from_address(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_barrio_embedded_address.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/chalet-barrio-los-troncos",
        )
        self.assertEqual(data["address"], "Las Araucarias 1900")
        self.assertEqual(data["neighborhood"], "Los Troncos")
        self.assertEqual(data["locality"], "Hurlingham")

    def test_guarnieri_ignores_suggested_price_and_metrics(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_barrio_ingles_suggested_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/chalet-2-plantas-b-ingles",
        )
        self.assertIsNone(data["price"])
        self.assertEqual(data["currency"], "")
        self.assertEqual(data["rooms"], 5)
        self.assertEqual(data["bedrooms"], 5)
        self.assertEqual(data["bathrooms"], Decimal("3"))
        self.assertEqual(data["garages"], 1)
        self.assertEqual(data["covered_area"], Decimal("200"))
        self.assertEqual(data["land_area"], Decimal("250"))
        self.assertEqual(data["total_area"], Decimal("250"))
        self.assertEqual(data["front_width"], Decimal("10"))
        self.assertEqual(data["lot_depth"], Decimal("25"))
        self.assertFalse(data.get("address"))
        self.assertEqual(data["neighborhood"], "Barrio Ingl\u00e9s")
        self.assertEqual(
            data["images"],
            [
                "https://guarnieripropiedades.com.ar/inmobiliaria/wp-content/uploads/2024/02/Pablo-Pizzurno-1287.jpeg"
            ],
        )

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
        self.assertEqual(data["neighborhood"], "Santos Tesei")
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
        scraper = MarceloRussoScraper(max_pages=1)
        scraper.soup = lambda parsed_url: fixture_soup("marcelo_russo_listing.html")
        self.assertEqual(
            list(scraper.discover()),
            ["https://marcelorussoprop.com.ar/property/3963-hurlingham/"],
        )

    def test_marcelo_russo_discovery_reads_embedded_property_links(self):
        scraper = MarceloRussoScraper(max_pages=1)
        html = """
        <html><body>
          <a href="/property/4323-hurlingham/">4323 - Hurlingham</a>
          <a href="/property-status/venta/">Venta</a>
          <script>
            var listings = [
              "https://marcelorussoprop.com.ar/property/4323-hurlingham/",
              "https://marcelorussoprop.com.ar/property/4289-ciudad-tesei/",
              "/property/4294-william-morris/",
              "https://marcelorussoprop.com.ar/property/5000-castelar/"
            ];
          </script>
        </body></html>
        """
        scraper.soup = lambda parsed_url: BeautifulSoup(html, "lxml")
        self.assertEqual(
            list(scraper.discover()),
            [
                "https://marcelorussoprop.com.ar/property/4323-hurlingham/",
                "https://marcelorussoprop.com.ar/property/4289-ciudad-tesei/",
                "https://marcelorussoprop.com.ar/property/4294-william-morris/",
            ],
        )

    def test_wordpress_listing_pagination_helpers(self):
        self.assertEqual(declared_total_from_text("1 a 12 de 349 propiedades"), 349)
        self.assertEqual(declared_total_from_text("Found 393 results"), 393)
        self.assertEqual(
            declared_total_from_text("Showing 1 to 12 properties of 393 found"),
            393,
        )
        self.assertEqual(declared_total_from_text("662 Propiedades en venta"), 662)
        self.assertEqual(declared_total_from_text("521 Results Found"), 521)
        self.assertEqual(declared_total_from_text("30 Resultados de busqueda"), 30)
        self.assertEqual(declared_total_from_text("12 Resultados encontrados"), 12)
        self.assertEqual(declared_total_from_text("Se encontraron 204 propiedades"), 204)
        self.assertEqual(declared_total_from_text("110 propiedades encontradas"), 110)
        self.assertEqual(declared_total_from_text("Se encontraron 393 resultados relacionados"), 393)
        self.assertEqual(declared_total_from_text("Mostrando 1 a 12 propiedades de 393 encontradas"), 393)
        self.assertEqual(declared_total_from_text("752 Propiedades e inmuebles en venta"), 752)
        self.assertEqual(declared_total_from_text("Zona Argentina (98) Tipo de operación venta"), 98)
        self.assertEqual(max_page_from_markup('<a href="/venta/page/4/">4</a>'), 4)
        self.assertEqual(
            max_page_from_markup('<a href="/motor/props.php?zona=109&page=5">5</a>'),
            5,
        )
        self.assertEqual(
            max_page_from_markup('<a href="/motor/props.php?zona=109&amp;page=21">21</a>'),
            21,
        )

    def test_fincas_guarnieri_and_offset_discovery_follow_pagination(self):
        cases = (
            (
                FincasScraper,
                "/propiedad-casa-1-1",
                "/propiedad-casa-2-2",
                "page=2",
                "98 Propiedades en venta",
            ),
            (
                GuarnieriScraper,
                "/inmobiliaria/propiedad/casa-1",
                "/inmobiliaria/propiedad/casa-2",
                "/page/2",
                "521 Results Found",
            ),
            (
                BecerraScraper,
                "/ficha/casa-1",
                "/ficha/casa-2",
                "page=2",
                "110 propiedades encontradas",
            ),
            (
                MiglieriniScraper,
                "/propiedad/casa-1/",
                "/propiedad/casa-2/",
                "/page/2/",
                "146 Propiedades encontradas",
            ),
            (
                RiquelmeScraper,
                "/propiedad/casa-1",
                "/propiedad/casa-2",
                "page=1",
                "Se encontraron 204 propiedades",
            ),
            (
                PatagonPropScraper,
                "/propiedad/casa-1",
                "/propiedad/casa-2",
                "from_12",
                "Showing 1 to 12 properties of 393 found",
            ),
            (
                ZonapropScraper,
                "/propiedades/clasificado/veclcain-casa-1.html",
                "/propiedades/clasificado/veclcain-casa-2.html",
                "pagina-2",
                "749 Propiedades en venta",
            ),
        )

        for scraper_cls, first_href, second_href, second_marker, total_text in cases:
            scraper = scraper_cls(max_pages=2)
            calls = []

            def fake_soup(url):
                calls.append(url)
                href = second_href if second_marker in url else first_href
                return BeautifulSoup(
                    f"<html><body><h1>{total_text}</h1><a href='{href}'>Venta Hurlingham</a></body></html>",
                    "lxml",
                )

            scraper.soup = fake_soup
            urls = list(scraper.discover())
            self.assertEqual(len(urls), 2, scraper_cls.__name__)
            self.assertTrue(any(second_marker in url for url in calls), scraper_cls.__name__)
            self.assertEqual(scraper.discovery_stats["pages_seen"], 2)

    def test_tokko_sources_use_ajax_pagination_until_empty(self):
        for scraper_cls in (LopezCombaScraper, AnaliaFernandezScraper, AliagaScraper):
            scraper = scraper_cls()
            calls = []

            def fake_soup(url):
                calls.append(url)
                if "p=2" in url:
                    html = "<a href='/p/200-casa-en-venta-hurlingham'>Casa 2</a>"
                elif "p=3" in url:
                    html = "--NoMoreProperties--"
                else:
                    html = "30 Resultados de busqueda <a href='/p/100-casa-en-venta-hurlingham'>Casa 1</a>"
                return BeautifulSoup(html, "lxml")

            scraper.soup = fake_soup
            urls = list(scraper.discover())
            self.assertEqual(len(urls), 2, scraper_cls.__name__)
            self.assertTrue(any("p=2" in url for url in calls), scraper_cls.__name__)
            self.assertEqual(scraper.discovery_stats["declared_total"], 30)

    def test_century21_json_discovery_uses_public_results_payload(self):
        class Response:
            def json(self):
                return {
                    "totalHits": "76",
                    "results": [
                        {
                            "urlCorrectaPropiedad": "/propiedad/casa-1",
                            "tipoOperacionTxt": "Venta",
                            "localidad": "Hurlingham",
                        },
                        {
                            "urlCorrectaPropiedad": "/propiedad/alquiler-1",
                            "tipoOperacionTxt": "Alquiler",
                            "localidad": "Hurlingham",
                        },
                    ],
                }

        scraper = Century21Scraper()
        scraper.get = lambda url: Response()
        self.assertEqual(list(scraper.discover()), ["https://century21.com.ar/propiedad/casa-1"])
        self.assertEqual(scraper.discovery_stats["declared_total"], 76)

    def test_remax_argentina_discovers_and_parses_public_api(self):
        scraper = RemaxArgentinaScraper()

        def fake_find_all(page):
            items = [
                {
                    "slug": f"venta-casa-{page}",
                    "operation": {"value": "sale"},
                    "geoLabel": "Hurlingham, Buenos Aires",
                }
            ]
            return {"data": {"data": items, "totalPages": 2, "totalItems": 2}}

        scraper._find_all = fake_find_all
        urls = list(scraper.discover())
        self.assertEqual(
            urls,
            [
                "https://www.remax.com.ar/listings/venta-casa-0",
                "https://www.remax.com.ar/listings/venta-casa-1",
            ],
        )
        self.assertEqual(scraper.discovery_stats["declared_total"], 2)
        self.assertEqual(scraper.discovery_stats["coverage_ratio"], 100.0)

        detail = {
            "data": {
                "id": "uuid-1",
                "title": "Casa en venta en William Morris",
                "slug": "venta-casa-0",
                "description": "Casa con galpon y parque",
                "operation": {"value": "sale"},
                "type": {"value": "casa"},
                "currency": {"value": "USD"},
                "price": 160000,
                "displayAddress": "Paso Morales 1500",
                "geo": {"neighborhood": "william morris", "label": "William Morris, Hurlingham"},
                "associate": {"office": {"name": "REMAX Desafio II", "slug": "desafioii"}},
                "location": {"coordinates": [-58.65, -34.57]},
                "totalRooms": 7,
                "bedrooms": 5,
                "bathrooms": 2,
                "parkingSpaces": 1,
                "dimensionLand": 857,
                "dimensionTotalBuilt": 857,
                "dimensionCovered": 156,
                "photos": [{"value": "listings/uuid-1/photo.jpg"}],
                "features": [{"value": "Galpon"}],
                "aptCredit": True,
            }
        }
        scraper._api_get = lambda path, **params: detail
        data = scraper.parse(urls[0])
        self.assertEqual(data["external_id"], "uuid-1")
        self.assertEqual(data["locality"], "William C. Morris")
        self.assertEqual(data["neighborhood"], "william morris")
        self.assertEqual(data["agency"], "REMAX Desafio II")
        self.assertEqual(data["price"], Decimal("160000"))
        self.assertEqual(data["land_area"], Decimal("857"))
        self.assertEqual(data["latitude"], -34.57)
        self.assertIn("Apto credito", data["features"])

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
            "remax",
            "century21-hurlingham",
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
    def test_single_active_job_guard_blocks_second_creation(self):
        class FakeDefinition:
            slug = "fake"
            name = "Fake Source"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            first = create_scrape_job(
                ["fake"],
                {"fake": 1},
                enforce_single_active=True,
            )
            with self.assertRaises(ActiveScrapeJobError) as ctx:
                create_scrape_job(
                    ["fake"],
                    {"fake": 1},
                    enforce_single_active=True,
                )

        self.assertEqual(ctx.exception.active_job_id, first.pk)
        self.assertEqual(ScrapeJob.objects.count(), 1)

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
            job = create_scrape_job(["fake"], {"fake": 1}, geocode_limit=0)
            run_scrape_job(job.pk)

        source = job.sources.get(slug="fake")
        self.assertEqual(source.status, ScrapeJobSource.Status.PARTIAL)
        self.assertEqual(source.processed, 5)
        self.assertIn("Fuente detenida automaticamente por bloqueo 403/CDN", source.logs)
        self.assertIn("No se marcan ausentes", source.logs)

    def test_url_errors_are_stored_for_selective_retry(self):
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
                return ["https://example.com/ok", "https://example.com/bad"]

            def parse(self, url):
                if url.endswith("/bad"):
                    raise RuntimeError("detalle roto")
                return {
                    "external_id": "ok",
                    "url": url,
                    "title": "Casa OK",
                    "address": "Calle 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", side_effect=lambda *args, **kwargs: FakeAdapter(**kwargs)):
            job = create_scrape_job(["fake"], {"fake": 1})
            run_scrape_job(job.pk)

        source = job.sources.get(slug="fake")
        self.assertEqual(source.status, ScrapeJobSource.Status.PARTIAL)
        self.assertEqual(source.error_urls[0]["url"], "https://example.com/bad")
        self.assertIn("detalle roto", source.error_urls[0]["error"])

    def test_terminal_listing_errors_are_removed_not_stored_for_retry(self):
        Path(".scrape.lock").unlink(missing_ok=True)
        source = Source.objects.create(
            slug="fake-gone", name="Fake Gone", base_url="https://example.com"
        )
        listing, _ = ingest_listing(
            source,
            {
                "external_id": "gone",
                "url": "https://example.com/gone",
                "title": "Casa retirada",
                "address": "Calle 100",
                "locality": "Hurlingham",
                "currency": "USD",
                "price": "100000",
            },
        )

        class FakeDefinition:
            slug = "fake-gone"
            name = "Fake Gone"
            base_url = "https://example.com"
            enabled = False
            crawl_delay = 0
            notes = ""

        class FakeAdapter:
            definition = FakeDefinition()

            def __init__(self, max_pages=None, request_timeout=None, max_listings=None, should_cancel=None):
                pass

            def discover(self):
                return ["https://example.com/gone"]

            def parse(self, url):
                raise RuntimeError(f"404 Client Error: AVISO TERMINADO for url: {url}")

        with patch("properties.services.scraping.get_adapter", side_effect=lambda *args, **kwargs: FakeAdapter(**kwargs)):
            job = create_scrape_job(["fake-gone"], {"fake-gone": 1}, geocode_limit=0)
            run_scrape_job(job.pk)

        source_progress = job.sources.get(slug="fake-gone")
        listing.refresh_from_db()
        listing.property.refresh_from_db()
        self.assertEqual(source_progress.status, ScrapeJobSource.Status.SUCCESS)
        self.assertEqual(source_progress.errors, 0)
        self.assertEqual(source_progress.skipped, 1)
        self.assertEqual(source_progress.error_urls, [])
        self.assertIn("Retirada: URL no disponible", source_progress.logs)
        self.assertFalse(listing.active)
        self.assertEqual(listing.source_status, "removed")
        self.assertEqual(listing.property.status, Property.Status.REMOVED)

    def test_retry_urls_skip_discovery_and_process_only_failures(self):
        Path(".scrape.lock").unlink(missing_ok=True)
        parsed = []

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
                raise AssertionError("No debe descubrir URLs durante reproceso selectivo.")

            def parse(self, url):
                parsed.append(url)
                return {
                    "external_id": url.rsplit("/", 1)[-1],
                    "url": url,
                    "title": "Casa retry",
                    "address": "Calle 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", side_effect=lambda *args, **kwargs: FakeAdapter(**kwargs)):
            job = create_scrape_job(
                ["fake"],
                {"fake": 1},
                geocode_limit=0,
                retry_urls={"fake": ["https://example.com/bad"]},
            )
            run_scrape_job(job.pk)

        job.refresh_from_db()
        source = job.sources.get(slug="fake")
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertEqual(source.total_to_process, 1)
        self.assertEqual(parsed, ["https://example.com/bad"])
        self.assertIn("Reproceso selectivo: 1 URLs con error", source.logs)

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
