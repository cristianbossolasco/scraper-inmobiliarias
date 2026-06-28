import json
import re
import threading
from datetime import timedelta
from pathlib import Path
from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from properties.models import (
    Agency,
    GeocodeCache,
    Listing,
    ListingIdentity,
    ListingSnapshot,
    OperationJob,
    OperationJobStep,
    Property,
    PropertyLocationIntelligence,
    PropertyLocation,
    ScrapeJob,
    ScrapeJobSource,
    ScrapeRun,
    Source,
)
from properties.services.operations import create_operation_job, run_operation_job
from properties.services.ingestion import ingest_listing, mark_listing_removed, mark_missing
from properties.services.location_enrichment import clean_detected_address, enrich_location_data
from properties.services.agency_normalization import normalize_agency_name
from properties.services.data_quality import is_garage_like, is_rental_url, valid_price, valid_value
from properties.services.geocoding import Geocoder
from properties.services.normalization import (
    address_alias_variants,
    build_fingerprint,
    classify_address_precision,
    infer_condition_category,
    is_plausible_property_address,
    known_neighborhood_name,
    locality_from_neighborhood,
    normalize_address,
    normalize_locality,
    normalize_neighborhood_name,
    normalize_street_number_address,
    parse_decimal,
    repair_mojibake_text,
)
from properties.services.scraping import (
    ActiveScrapeJobError,
    JOB_THREADS,
    create_scrape_job,
    db_writer_snapshot,
    run_scrape_job,
    serialize_job,
    source_catalog,
)
from properties.services.security_scoring import risk_from_coverage, score_coordinates
from properties.services.location_intelligence import (
    location_intelligence_layers_payload,
    score_property_location_intelligence,
)
from properties.services.crime_context import crime_layers_payload, homicide_counts_by_zone
from properties.services.canonical_zones import missing_required_zones
from properties.services.geo_hierarchy import geo_hierarchy_payload
from properties.services.hurlingham_centro_backfill import backfill_hurlingham_centro_zone
from properties.services.spatial import haversine_km, point_in_polygon
from properties.services.zone_names import UNIFIED_HURLINGHAM_CENTRO_ZONE
from properties.services.zone_inference import (
    apply_zone_inference,
    infer_property_zone,
    infer_zone_for_point,
    load_zone_index,
)
from properties.services.territory_hierarchy import (
    infer_property_territory,
    infer_territory_for_point,
)
from properties.scrapers.argenprop import ArgenpropScraper
from properties.scrapers.base import BaseScraper, ROBOTS_CACHE, SourceDefinition
from properties.scrapers.argencasas import ArgencasasScraper
from properties.scrapers.local_wordpress import (
    MiglieriniScraper,
    OdriozolaScraper,
    is_miglierini_detail_url,
    is_odriozola_detail_url,
)
from properties.scrapers.local_sites import AliagaScraper, BecerraScraper, FaellaScraper
from properties.scrapers.mapaprop import MapapropScraper
from properties.scrapers.mercadoprop import MercadoPropScraper
from properties.scrapers.pending_sources import (
    AnaliaFernandezScraper,
    FincasScraper,
    GabrielParisScraper,
    GuarnieriScraper,
    HGranelliScraper,
    HollmannArielScraper,
    InmueblesClarinScraper,
    LopezCombaScraper,
    MarceloRussoScraper,
    MatiasBarbieriScraper,
    MatiasSzpiraScraper,
    MercadoLibreScraper,
    MudafyScraper,
    NerinaAlloScraper,
    OscarDahbarScraper,
    PatagonPropScraper,
    PaulaFossatiScraper,
    parse_dimension_value,
    parse_multi_unit_offers,
    RemaxArgentinaScraper,
    RemaxDataworkScraper,
    RiquelmeScraper,
    Century21Scraper,
    ValentiScraper,
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
        self.assertEqual(normalize_street_number_address("Rolland al 1.200"), "Rolland 1200")
        self.assertEqual(normalize_street_number_address("GRANADA 500, Piso 0"), "GRANADA 500")
        self.assertEqual(normalize_street_number_address("Esteban De Luca al 0"), "Esteban De Luca 100")
        self.assertEqual(normalize_street_number_address("Bonorino 634 , Piso 1"), "Bonorino 634")
        self.assertIn("José de Andonaegui 2600", address_alias_variants("J De Andonaegui 2600"))
        self.assertIn("Esteban Bonorino 634", address_alias_variants("Bonorino 634"))
        self.assertIn("Eduardo Acevedo 329", address_alias_variants("Acevedo Eduardo 329"))
        self.assertIn("Juan Díaz de Solís 700", address_alias_variants("Solis 700"))
        self.assertIn("Einstein 100", address_alias_variants("Alberto Einstein 100"))
        self.assertIn("Diego de Carvajal 600", address_alias_variants("Diego de Carbajal 600"))
        self.assertIn(
            "Maestra A. González de Hecht 1200",
            address_alias_variants("Maestra A Gonzalez De Hecht 1200"),
        )
        self.assertIn(
            "Catalina de Pizzagalli 700",
            address_alias_variants("Maestra Catalina G. de Pizzagalli 700"),
        )
        self.assertIn(
            "Pizzagalli 700",
            address_alias_variants("Maestra Catalina G. de Pizzagalli 700"),
        )
        self.assertIn("Isabel Maestro 3500", address_alias_variants("Isabel del Maestro 3500"))
        self.assertIn("Isabel de Maestro 3500", address_alias_variants("Isabel del Maestro 3500"))
        self.assertIn("El Maestro Argentino 1900", address_alias_variants("Maestra Argentino 1900"))
        self.assertIn("El Maestro Argentino 3100", address_alias_variants("Maestro Argentino 3100"))
        self.assertIn("Gral. Simón Bolívar 1700", address_alias_variants("Gral Simon Bolivar 1700"))
        self.assertIn("Gral. Simón Bolívar 1738", address_alias_variants("Simón Bolívar 1738"))
        self.assertIn("Dip. Hector Finochietto 2000", address_alias_variants("Diputado Finochietto 2000"))
        self.assertIn("Dip. Hector Finochietto 1700", address_alias_variants("Finocchieto 1700"))
        self.assertIn("Finochietto 1700", address_alias_variants("Finocchieto 1700"))
        self.assertIn("Vasco Núñez de Balboa 379", address_alias_variants("BALBOA 379"))
        self.assertIn("Gral. Martín Güemes 1668", address_alias_variants("GUEMES 1668"))
        self.assertIn("Tte. Gral. Julio Argentino Roca 1940", address_alias_variants("Av.Julio A Roca 1940"))
        self.assertIn("Manuel A. Padilla 1200", address_alias_variants("Cnel M A Padilla 1200"))
        self.assertIn("Pablo Pizzurno 686", address_alias_variants("Pizzurno 686"))
        self.assertIn("Cjal. Enrique Recagno 700", address_alias_variants("Recagno 700"))
        self.assertIn("Tte. Gral. Julio Argentino Roca 2100", address_alias_variants("Av. Roca 2100"))
        self.assertIn("Quintino Bocayuva 218", address_alias_variants("Bocayuba 218"))
        self.assertIn("Kennedy y Jorge Daniel Thevenin", address_alias_variants("Kennedy y Thevening"))
        self.assertIn("Tte. Gral. Pablo Ricchieri 1400", address_alias_variants("Richieri 1400"))
        self.assertIn("Nilda Figueira 1400", address_alias_variants("Nilda Figueiras 1400"))
        self.assertIn("Diego de Carvajal 800", address_alias_variants("Diego de Carabajal 800"))
        self.assertIn("José Garibaldi 2600", address_alias_variants("Garibaldi 2600"))
        self.assertIn("Av. Gdor. Vergara 3604", address_alias_variants("avenida vergara 3604"))
        self.assertIn("Eva Perón 2200 esquina Guevara", address_alias_variants("J. Bustamante y Guevara 2200"))

        variants = address_alias_variants("General T de Luzuriaga 1700")
        self.assertTrue(any("Toribio" in item and "1700" in item for item in variants))
        self.assertIn("Dip. Hector Finochietto 1900", address_alias_variants("Finochieto 1900"))
        self.assertIn("Schubert 2400", address_alias_variants("Schubet 2400"))
        self.assertTrue(any("Mart" in item and "1400" in item for item in address_alias_variants("Tte.Gral. GUEMES 1400")))
        self.assertIn("Av. Rosas Castillo 2900", address_alias_variants("SGTO. ROSAS CASTILLO 2900"))
        self.assertIn("Gutenberg 2100", address_alias_variants("Gutemberg 2100"))
        self.assertTrue(any("Alfredo" in item and "1635" in item for item in address_alias_variants("Rodriguez 1635")))
        self.assertTrue(any("100" in item for item in address_alias_variants("BUSTAMANTE al 0")))
        self.assertIn("Vicente Camargo 2900", address_alias_variants("Avenida Vicente Camargo 2900"))
        self.assertIn("Virriato Unia 2412", address_alias_variants("Virriato Unía 2412"))
        self.assertIn("Dr. Delfor Díaz 2600", address_alias_variants("DELFOR DIAZ 2600"))
        self.assertIn("Basilio Delleva 1500", address_alias_variants("DELL EVA 1500"))
        self.assertIn("Gral. Francisco Miranda 1500", address_alias_variants("Miranda 1500"))
        self.assertIn("José de Minoguye 2400", address_alias_variants("Minoguyen 2400"))
        self.assertIn("Av. Rosas Castillo 2400", address_alias_variants("Rosa Castilllo 2400"))
        self.assertIn("Bombero Celiz 200", address_alias_variants("Bomberos Celiz 200"))
        self.assertIn("Gral. Toribio de Luzuriaga 1700", address_alias_variants("General Toribio Luzuriaga 1700"))
        self.assertIn("German Argerich 1300", address_alias_variants("Argerich 1300"))
        self.assertIn("Conscripto Bernardi 587", address_alias_variants("Bernardi 587"))
        self.assertIn("Coraceros 2400", address_alias_variants("Caroceros 2400"))
        self.assertIn("Coraceros 3115", address_alias_variants("CORACERO 3115"))
        self.assertIn("Tte. Manuel Origone 287", address_alias_variants("Tte. Origone 287"))

    def test_decimal_formats(self):
        self.assertEqual(parse_decimal("USD 169.000"), Decimal("169000"))
        self.assertEqual(parse_decimal("151,50 m²"), Decimal("151.50"))

    def test_repair_mojibake_text(self):
        cases = {
            "Ba\u00c3\u00b1os": "Baños",
            "Tambi\u00c3\u00a9n": "También",
            "Par\u00c3\u00a1metro": "Parámetro",
            "151,50 m\u00c2\u00b2": "151,50 m²",
            "151,50 m\u00c3\u201a\u00c2\u00b2": "151,50 m²",
            "d\u00c3\u00baplex": "dúplex",
            "\u00c3\u0081ngel Vicente Peñaloza": "Ángel Vicente Peñaloza",
        }
        for value, expected in cases.items():
            with self.subTest(value=value.encode("unicode_escape").decode("ascii")):
                self.assertEqual(repair_mojibake_text(value), expected)

        self.assertEqual(
            normalize_street_number_address("Albari\u00c3\u00b1os al 1700"),
            "Albariños 1700",
        )

    def test_public_ui_files_do_not_contain_mojibake(self):
        pattern = re.compile(r"(?:Ã.|Â.|�)")
        offenders = []
        for root in (Path("templates"), Path("static")):
            for path in root.rglob("*"):
                if path.suffix.lower() not in {".html", ".css", ".js"}:
                    continue
                if pattern.search(path.read_text(encoding="utf-8")):
                    offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_required_canonical_zone_guardrail_detects_missing_geojson_feature(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "zones.geojson"
            payload = {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {"zone_name": "Hurlingham Centro"}}],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(missing_required_zones(path), [UNIFIED_HURLINGHAM_CENTRO_ZONE])

            payload["features"].append(
                {"type": "Feature", "properties": {"zone_name": UNIFIED_HURLINGHAM_CENTRO_ZONE}}
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(missing_required_zones(path), [])

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
        self.assertEqual(normalize_neighborhood_name("Barrio Ingles"), UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(normalize_neighborhood_name("Ingl\u00e9s"), UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(normalize_neighborhood_name("Hurlingham Centro"), UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(normalize_neighborhood_name("Morris"), "William C. Morris")
        self.assertEqual(normalize_neighborhood_name("5 esquinas, Hurlingham Centro"), "5 esquinas")
        self.assertEqual(
            normalize_neighborhood_name(
                "de perfil familiar, con accesos cercanos. Consultanos para conocer mas"
            ),
            "",
        )

    def test_locality_is_strict_and_neighborhoods_are_not_localities(self):
        self.assertEqual(normalize_locality("Hurlingham"), "Hurlingham")
        self.assertEqual(normalize_locality("Villa Tesei"), "Villa Tesei")
        self.assertEqual(normalize_locality("William Morris"), "William C. Morris")
        self.assertEqual(normalize_locality("Parque Johnston"), "")
        self.assertEqual(normalize_locality("Ar Emprendimientos Inmobiliarias Mapa Es Publicar"), "")
        self.assertEqual(known_neighborhood_name("Parque Jhonston"), "Parque Johnston")
        self.assertEqual(locality_from_neighborhood("Parque Johnston"), "Hurlingham")

    def test_repair_localities_dry_run_apply_and_manual_override(self):
        dirty = Property.objects.create(
            fingerprint="dirty-locality",
            title="Casa con zona como localidad",
            operation="sale",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
            locality="Parque Johnston",
        )
        manual = Property.objects.create(
            fingerprint="manual-locality",
            title="Casa manual",
            operation="sale",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
            locality="Parque Quirno",
            manual_overrides={"locality": "Parque Quirno"},
        )

        output = StringIO()
        call_command("repair_localities", "--dry-run", stdout=output)
        dirty.refresh_from_db()
        self.assertEqual(dirty.locality, "Parque Johnston")

        call_command("repair_localities", stdout=StringIO())
        dirty.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(dirty.locality, "Hurlingham")
        self.assertEqual(dirty.neighborhood, "Parque Johnston")
        self.assertIn("repair_localities", dirty.location_notes)
        self.assertEqual(manual.locality, "Parque Quirno")

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

    def test_zonaprop_without_address_uses_strict_content_identity(self):
        source = Source(slug="zonaprop", name="Zonaprop", base_url="https://www.zonaprop.com.ar")
        base_data = {
            "title": "Venta Lote 400 m2 Hurlingham",
            "description": "Excelente lote en Hurlingham con salida rapida y medidas claras.",
            "address": "",
            "locality": "Hurlingham",
            "property_type": Property.Type.LAND,
            "operation": "sale",
            "currency": "USD",
            "price": Decimal("70000"),
            "land_area": Decimal("400"),
        }
        first = build_fingerprint(
            {
                **base_data,
                "external_id": "59337696-a",
                "url": "https://www.zonaprop.com.ar/propiedades/clasificado/a.html",
            },
            source=source,
        )
        second = build_fingerprint(
            {
                **base_data,
                "external_id": "59337696-b",
                "url": "https://www.zonaprop.com.ar/propiedades/clasificado/b.html",
            },
            source=source,
        )
        different_price = build_fingerprint(
            {
                **base_data,
                "external_id": "59337696-c",
                "url": "https://www.zonaprop.com.ar/propiedades/clasificado/c.html",
                "price": Decimal("72000"),
            },
            source=source,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_price)

    def test_condition_category_inference(self):
        self.assertEqual(
            infer_condition_category("Casa a estrenar en Hurlingham"),
            Property.ConditionCategory.NEW,
        )
        self.assertEqual(
            infer_condition_category("Chalet a reciclar con gran lote"),
            Property.ConditionCategory.NEEDS_WORK,
        )
        self.assertEqual(
            infer_condition_category("Casa reciclada en excelente estado"),
            Property.ConditionCategory.RENOVATED,
        )
        self.assertEqual(infer_condition_category("Casa en venta"), Property.ConditionCategory.UNKNOWN)

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
            "Eduardo Acevedo 329",
        )
        self.assertEqual(
            clean_detected_address("Valentín Alsina 2243 - Barrio Cartero"),
            "Valentín Alsina 2243",
        )
        self.assertEqual(
            clean_detected_address("Carhue 391. Entre Maestra Salinas y Las Provincias"),
            "Carhué 391",
        )
        self.assertEqual(
            clean_detected_address("Hurlingham-conscripto Bernardi 1900"),
            "Conscripto Bernardi 1900",
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


class SecurityScoringTests(TestCase):
    def _security_geojson_path(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "security.geojson"
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "zone_name": "Zona Test",
                        "security_infrastructure_score": 72,
                        "security_level": "alta",
                        "source": "test",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-58.70, -34.66],
                            [-58.60, -34.66],
                            [-58.60, -34.55],
                            [-58.70, -34.55],
                            [-58.70, -34.66],
                        ]],
                    },
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _security_points_path(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "points.geojson"
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"security_type": "camera", "name": "Camara Test"},
                    "geometry": {"type": "Point", "coordinates": [-58.64, -34.60]},
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_security_score_normalizes_coverage_and_risk(self):
        feature = json.loads(self._security_geojson_path().read_text(encoding="utf-8"))["features"][0]
        point = json.loads(self._security_points_path().read_text(encoding="utf-8"))["features"][0]

        score = score_coordinates(-34.60, -58.64, [feature], [point])

        self.assertEqual(score.coverage_score, 72)
        self.assertEqual(risk_from_coverage(score.coverage_score), 28)
        self.assertEqual(score.zone_label, "Zona Test")
        self.assertEqual(score.evidence["nearby_points"]["by_type"]["camera"], 1)

    def test_score_security_command_dry_run_and_apply(self):
        property_obj = Property.objects.create(
            fingerprint="security-command",
            title="Casa segura",
            operation="sale",
            status=Property.Status.ACTIVE,
        )
        PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.60,
            longitude=-58.64,
            precision=PropertyLocation.Precision.EXACT,
        )
        geojson_path = self._security_geojson_path()
        points_path = self._security_points_path()

        call_command(
            "score_security",
            "--dry-run",
            "--property-id",
            str(property_obj.pk),
            "--geojson",
            str(geojson_path),
            "--points-geojson",
            str(points_path),
            stdout=StringIO(),
        )
        property_obj.refresh_from_db()
        self.assertIsNone(property_obj.security_coverage_score)

        call_command(
            "score_security",
            "--property-id",
            str(property_obj.pk),
            "--geojson",
            str(geojson_path),
            "--points-geojson",
            str(points_path),
            stdout=StringIO(),
        )
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.security_coverage_score, 72)
        self.assertEqual(property_obj.security_risk_score, 28)
        self.assertEqual(property_obj.security_level, "alta")
        self.assertEqual(property_obj.security_zone_label, "Zona Test")


class LocationIntelligenceScoringTests(TestCase):
    def _location_geojson_path(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "location_value.geojson"
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "zone_name": "Zona Test",
                        "overall_location_value_score": 73,
                        "location_value_level": "alta",
                        "transport_access_score": 81,
                        "education_access_score": 66,
                        "health_access_score": 52,
                        "flood_penalty_score": 15,
                        "in_flood_risk_zone": False,
                        "urban_informality_score": 20,
                        "nearest_renabap_m": 420,
                        "nearest_sube_point_m": 180,
                        "nearest_school_m": 260,
                        "nearest_health_center_m": 500,
                        "data_confidence": "high",
                        "generated_at": "2026-06-13T00:00:00+00:00",
                        "score_methodology": "test",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-58.70, -34.66],
                            [-58.60, -34.66],
                            [-58.60, -34.55],
                            [-58.70, -34.55],
                            [-58.70, -34.66],
                        ]],
                    },
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_location_intelligence_scores_by_coordinates_and_zone_fallback(self):
        path = self._location_geojson_path()
        features = json.loads(path.read_text(encoding="utf-8"))["features"]
        located = Property.objects.create(
            fingerprint="location-intel-point",
            title="Casa territorial",
            operation="sale",
            status=Property.Status.ACTIVE,
        )
        PropertyLocation.objects.create(
            property=located,
            latitude=-34.60,
            longitude=-58.64,
            precision=PropertyLocation.Precision.EXACT,
        )
        fallback = Property.objects.create(
            fingerprint="location-intel-zone",
            title="Casa zona",
            operation="sale",
            status=Property.Status.ACTIVE,
            inferred_neighborhood="Zona Test",
        )

        point_score = score_property_location_intelligence(
            located,
            zones=features,
            source_signature="test",
        )
        zone_score = score_property_location_intelligence(
            fallback,
            zones=features,
            source_signature="test",
        )

        self.assertEqual(point_score.overall_score, 73)
        self.assertEqual(point_score.match_method, "coordinates")
        self.assertEqual(point_score.transport_score, 81)
        self.assertEqual(point_score.evidence["matched_zone"], "Zona Test")
        self.assertEqual(zone_score.overall_score, 73)
        self.assertEqual(zone_score.match_method, "zone")

    def test_score_location_intelligence_command_dry_run_apply_and_only_missing(self):
        path = self._location_geojson_path()
        existing = Property.objects.create(
            fingerprint="location-intel-existing",
            title="Casa existente",
            operation="sale",
            status=Property.Status.ACTIVE,
            inferred_neighborhood="Zona Test",
        )
        PropertyLocationIntelligence.objects.create(
            property=existing,
            overall_score=10,
            level="baja",
            zone_name="Vieja",
        )
        missing = Property.objects.create(
            fingerprint="location-intel-missing",
            title="Casa faltante",
            operation="sale",
            status=Property.Status.ACTIVE,
            inferred_neighborhood="Zona Test",
        )

        call_command(
            "score_location_intelligence",
            "--dry-run",
            "--geojson",
            str(path),
            stdout=StringIO(),
        )
        self.assertFalse(
            PropertyLocationIntelligence.objects.filter(property=missing).exists()
        )

        call_command(
            "score_location_intelligence",
            "--only-missing",
            "--geojson",
            str(path),
            stdout=StringIO(),
        )
        existing.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual(existing.location_intelligence.overall_score, 10)
        self.assertEqual(missing.location_intelligence.overall_score, 73)
        self.assertEqual(missing.location_intelligence.zone_name, "Zona Test")

    def test_location_intelligence_layers_payload_sanitizes_zones(self):
        payload = location_intelligence_layers_payload(zone_path=self._location_geojson_path())

        self.assertTrue(payload["configured"])
        props = payload["zones"]["features"][0]["properties"]
        self.assertEqual(props["overall_score"], 73)
        self.assertEqual(props["transport_score"], 81)
        self.assertIn("renabap", payload["notes"])


class CrimeContextTests(TestCase):
    def _crime_fixture_paths(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        summary_path = base / "summary.json"
        zones_path = base / "zones.geojson"
        points_path = base / "homicide_points.geojson"
        timeseries_path = base / "timeseries.csv"
        summary_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "metrics": {
                        "crime_data_scope": "municipio",
                        "crime_spatial_precision": "low",
                        "crime_municipality": "Hurlingham",
                        "crime_metric_window_start_year": 2017,
                        "crime_metric_window_end_year": 2024,
                        "reported_crimes_total": 10,
                        "reported_property_crime_count": 4,
                        "reported_homicide_count": 1,
                    },
                    "validation": {"crime_zone_features": 1},
                    "source_row_counts": {"snic_departamentos_mensual": 2},
                }
            ),
            encoding="utf-8",
        )
        zones_path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "id": "zone-test",
                                "zone_name": "Zona Test",
                                "crime_data_scope": "municipio",
                                "crime_spatial_precision": "low",
                                "reported_crimes_total": 10,
                                "reported_property_crime_count": 4,
                                "reported_homicide_count": 1,
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[
                                    [-58.70, -34.66],
                                    [-58.60, -34.66],
                                    [-58.60, -34.55],
                                    [-58.70, -34.55],
                                    [-58.70, -34.66],
                                ]],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        points_path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": "sat-test",
                            "properties": {
                                "period_year": 2020,
                                "period_month": 5,
                                "id_hecho": "1",
                                "victims_count": 2,
                                "tipo_lugar": "Via publica",
                                "clase_arma": "Arma de fuego",
                                "assigned_zone_name": "Zona Test",
                                "is_exact_location": False,
                            },
                            "geometry": {"type": "Point", "coordinates": [-58.64, -34.60]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        timeseries_path.write_text(
            "\n".join(
                [
                    "source,dataset,source_key,source_role,geo_level,municipality,province,period_year,period_month,crime_group,crime_type,measure,value,source_file",
                    "SNIC,SNIC,snic_departamentos_mensual,canonical_general,municipio,Hurlingham,Buenos Aires,2020,5,robo,Robo,cantidad_hechos,3,raw.csv",
                    "SNIC,SNIC,snic_departamentos_mensual,canonical_general,municipio,Hurlingham,Buenos Aires,2020,5,robo,Robo,cantidad_victimas,1,raw.csv",
                    "SAT,SAT,sat_propiedad,property_detail,municipio,Hurlingham,Buenos Aires,2020,5,hurto,Hurto,cantidad_hechos,4,raw.csv",
                    "SNIC,SNIC,snic_departamentos_mensual,canonical_general,municipio,Otro,Buenos Aires,2020,5,robo,Robo,cantidad_hechos,99,raw.csv",
                ]
            ),
            encoding="utf-8",
        )
        return summary_path, zones_path, points_path, timeseries_path

    def test_crime_layers_payload_loads_and_aggregates_sources(self):
        summary_path, zones_path, points_path, timeseries_path = self._crime_fixture_paths()

        payload = crime_layers_payload(
            summary_path=summary_path,
            zone_path=zones_path,
            point_path=points_path,
            timeseries_path=timeseries_path,
        )

        self.assertTrue(payload["configured"])
        self.assertEqual(len(payload["zones"]["features"]), 1)
        self.assertEqual(payload["zones"]["features"][0]["properties"]["crime_data_scope"], "municipio")
        self.assertEqual(len(payload["homicide_points"]["features"]), 1)
        self.assertFalse(payload["homicide_points"]["features"][0]["properties"]["is_exact_location"])
        self.assertEqual(payload["timeseries"]["monthly"][0]["cantidad_hechos"], 3)
        self.assertEqual(payload["timeseries"]["monthly"][0]["cantidad_victimas"], 1)
        self.assertEqual(payload["timeseries"]["property_monthly"][0]["cantidad_hechos"], 4)
        self.assertEqual(payload["timeseries"]["property_seasonality"][0]["value"], 4)
        self.assertEqual(homicide_counts_by_zone(points_path)["Zona Test"]["victim_count"], 2)

    def test_crime_layers_payload_handles_missing_files(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        missing = Path(temp_dir.name) / "missing.json"

        payload = crime_layers_payload(
            summary_path=missing,
            zone_path=missing,
            point_path=missing,
            timeseries_path=missing,
        )

        self.assertFalse(payload["configured"])
        self.assertEqual(payload["zones"]["features"], [])
        self.assertFalse(payload["timeseries"]["configured"])


class GeoHierarchyTests(TestCase):
    def _feature_collection(self, features):
        return {
            "type": "FeatureCollection",
            "metadata": {"crs": "EPSG:4326"},
            "features": features,
        }

    def _polygon_feature(self, feature_id, props, ring):
        return {
            "type": "Feature",
            "id": feature_id,
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    def _write_geojson(self, base, filename, features):
        (base / filename).write_text(
            json.dumps(self._feature_collection(features)),
            encoding="utf-8",
        )

    def test_geo_hierarchy_payload_builds_tree_and_anonymized_evidence(self):
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            partido_ring = [
                [-58.70, -34.66],
                [-58.60, -34.66],
                [-58.60, -34.55],
                [-58.70, -34.55],
                [-58.70, -34.66],
            ]
            zone_ring = [
                [-58.64, -34.61],
                [-58.62, -34.61],
                [-58.62, -34.58],
                [-58.64, -34.58],
                [-58.64, -34.61],
            ]
            self._write_geojson(
                base,
                "01_partido_hurlingham.geojson",
                [
                    self._polygon_feature(
                        "partido_hurlingham",
                        {"level": 1, "canonical_name": "Partido de Hurlingham", "level_name": "partido"},
                        partido_ring,
                    )
                ],
            )
            self._write_geojson(
                base,
                "02_localidades_hurlingham.geojson",
                [
                    self._polygon_feature(
                        "locality_hurlingham",
                        {"level": 2, "canonical_name": "Hurlingham", "locality_name": "Hurlingham", "level_name": "localidad"},
                        partido_ring,
                    )
                ],
            )
            self._write_geojson(
                base,
                "03_zonas_hurlingham_final.geojson",
                [
                    self._polygon_feature(
                        "zone_hurlinghamcentro",
                        {
                            "level": 3,
                            "canonical_name": UNIFIED_HURLINGHAM_CENTRO_ZONE,
                            "zone_name": UNIFIED_HURLINGHAM_CENTRO_ZONE,
                            "parent_locality": "Hurlingham",
                            "level_name": "zona",
                        },
                        zone_ring,
                    )
                ],
            )
            self._write_geojson(
                base,
                "03b_microzonas_hurlingham_final.geojson",
                [],
            )
            self._write_geojson(
                base,
                "04_gaps_zonas_hurlingham_final.geojson",
                [
                    self._polygon_feature(
                        "gap_001",
                        {"level": 99, "canonical_name": "Gap 001", "gap_id": "GAP_001", "level_name": "gap_diagnostico"},
                        zone_ring,
                    )
                ],
            )
            evidence = self._feature_collection([])
            (base / "03b_microzonas_hurlingham_evidence_points.geojson").write_text(
                json.dumps(evidence),
                encoding="utf-8",
            )

            payload = geo_hierarchy_payload(base_dir=base)

        self.assertTrue(payload["configured"])
        hurlingham = payload["tree"]["children"][0]
        self.assertEqual(hurlingham["label"], "Hurlingham")
        zone = hurlingham["children"][0]
        self.assertEqual(zone["label"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(zone["children"], [])
        self.assertEqual(payload["evidence"]["barrio_ingles_points"]["features"], [])

    def test_territory_view_and_layers_api_respond(self):
        page = self.client.get("/territorio/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "territory-map")

        response = self.client.get("/api/jerarquia-geografica/capas/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("tree", payload)
        self.assertIn("layers", payload)
        self.assertIn("barrio_ingles_points", payload["evidence"])


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
            polygon("Hurlingham Centro", direct_ring),
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
        self.assertEqual(inside["zone"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(inside["method"], "polygon")

        boundary = infer_zone_for_point(-34.6000, -58.6405, path, max_distance_m=100)
        self.assertEqual(boundary["zone"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(boundary["method"], "polygon")

        nearby = infer_zone_for_point(-34.5997, -58.6405, path, max_distance_m=100)
        self.assertEqual(nearby["zone"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(nearby["method"], "nearest")

        far = infer_zone_for_point(-34.5980, -58.6405, path, max_distance_m=20)
        self.assertEqual(far["zone"], "")
        self.assertEqual(far["method"], "no_match")

    def test_zone_loader_rebuilds_closed_osm_relation_and_reports_incomplete(self):
        index = load_zone_index(self._geojson_path())
        names = {polygon.name for polygon in index.polygons}
        self.assertIn("Cartero", names)
        self.assertIn("300", index.skipped_relations)

        result = infer_zone_for_point(-34.6025, -58.6425, self._geojson_path())
        self.assertEqual(result["zone"], "Cartero")

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

        self.assertEqual(result.inferred_neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)
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
        self.assertEqual(property_obj.inferred_neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)
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
        self.assertEqual(property_obj.inferred_neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)

    def test_hurlingham_centro_backfill_updates_aliases_without_touching_manual_timestamp(self):
        corrected_at = timezone.now()
        property_obj = Property.objects.create(
            fingerprint="zone-backfill",
            title="Casa Barrio Ingles",
            neighborhood="Barrio Inglés",
            detected_neighborhood="Hurlingham Centro",
            inferred_neighborhood="Barrio Ingles",
            manual_overrides={
                "neighborhood": "Barrio Inglés",
                "address": corrected_at.isoformat(),
            },
            data_manually_corrected_at=corrected_at,
            zone_inference_evidence={
                "source_zone": "Barrio Ingles",
                "inferred_zone": "Hurlingham Centro",
            },
        )
        PropertyLocationIntelligence.objects.create(
            property=property_obj,
            zone_name="Hurlingham Centro",
            evidence={"matched_zone": "Barrio Ingles"},
        )

        result = backfill_hurlingham_centro_zone()

        property_obj.refresh_from_db()
        record = property_obj.location_intelligence
        self.assertEqual(result["counts"]["properties"], 1)
        self.assertEqual(property_obj.neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(property_obj.detected_neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(property_obj.inferred_neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(property_obj.manual_overrides["neighborhood"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(property_obj.manual_overrides["address"], corrected_at.isoformat())
        self.assertEqual(property_obj.data_manually_corrected_at, corrected_at)
        self.assertEqual(property_obj.zone_inference_evidence["source_zone"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(record.zone_name, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(record.evidence["matched_zone"], UNIFIED_HURLINGHAM_CENTRO_ZONE)


class TerritoryHierarchyInferenceTests(TestCase):
    def _geo_dir(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)

        def write(name, features):
            (base / name).write_text(
                json.dumps({"type": "FeatureCollection", "features": features}),
                encoding="utf-8",
            )

        def polygon(feature_id, props, ring):
            return {
                "type": "Feature",
                "id": feature_id,
                "properties": props,
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }

        partido = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        locality = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        zone = [[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]]
        write(
            "01_partido_hurlingham.geojson",
            [
                polygon(
                    "partido-1",
                    {"canonical_name": "Partido de Hurlingham", "source_confidence": "high"},
                    partido,
                )
            ],
        )
        write(
            "02_localidades_hurlingham.geojson",
            [
                polygon(
                    "localidad-1",
                    {"canonical_name": "Hurlingham", "source_confidence": "high"},
                    locality,
                )
            ],
        )
        write(
            "03_zonas_hurlingham_final.geojson",
            [
                polygon(
                    "zona-1",
                    {
                        "canonical_name": "Hurlingham Centro",
                        "parent_locality": "Hurlingham",
                        "source_confidence": "medium",
                        "source_method": "manual",
                    },
                    zone,
                )
            ],
        )
        return base

    def test_point_inference_returns_three_layers_and_canonical_alias(self):
        result = infer_territory_for_point(1, 1, geo_dir=self._geo_dir(), coordinate_source="test")

        self.assertEqual(result.partido, "Partido de Hurlingham")
        self.assertEqual(result.locality, "Hurlingham")
        self.assertEqual(result.zone, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertFalse(result.needs_review)
        self.assertEqual(result.evidence["coordinate_source"], "test")

    def test_point_inside_locality_without_zone_keeps_hierarchy_and_marks_review(self):
        result = infer_territory_for_point(8, 8, geo_dir=self._geo_dir())

        self.assertEqual(result.partido, "Partido de Hurlingham")
        self.assertEqual(result.locality, "Hurlingham")
        self.assertEqual(result.zone, "")
        self.assertTrue(result.needs_review)

    def test_property_manual_location_is_used_before_detected_coordinates(self):
        prop = Property.objects.create(
            fingerprint="territory-priority",
            title="Casa prioridad manual",
            detected_latitude=8,
            detected_longitude=8,
        )
        PropertyLocation.objects.create(
            property=prop,
            latitude=1,
            longitude=1,
            precision=PropertyLocation.Precision.EXACT,
        )

        result = infer_property_territory(prop, geo_dir=self._geo_dir())

        self.assertEqual(result.zone, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(result.evidence["coordinate_source"], "location")

    def test_backfill_command_dry_run_and_apply_preserve_manual_fields(self):
        corrected_at = timezone.now() - timedelta(days=1)
        prop = Property.objects.create(
            fingerprint="territory-backfill",
            title="Casa backfill territorio",
            manual_overrides={"neighborhood": "Barrio Ingles"},
            data_manually_corrected_at=corrected_at,
        )
        PropertyLocation.objects.create(
            property=prop,
            latitude=1,
            longitude=1,
            precision=PropertyLocation.Precision.EXACT,
        )
        geo_dir = self._geo_dir()

        call_command(
            "backfill_territory_hierarchy",
            "--dry-run",
            "--quiet",
            "--property-id",
            str(prop.pk),
            "--geo-dir",
            str(geo_dir),
            stdout=StringIO(),
        )
        prop.refresh_from_db()
        self.assertEqual(prop.inferred_zone, "")
        self.assertEqual(prop.manual_overrides, {"neighborhood": "Barrio Ingles"})
        self.assertEqual(prop.data_manually_corrected_at, corrected_at)

        call_command(
            "backfill_territory_hierarchy",
            "--apply",
            "--quiet",
            "--property-id",
            str(prop.pk),
            "--geo-dir",
            str(geo_dir),
            stdout=StringIO(),
        )
        prop.refresh_from_db()
        intelligence = prop.location_intelligence
        self.assertEqual(prop.inferred_partido, "Partido de Hurlingham")
        self.assertEqual(prop.inferred_locality, "Hurlingham")
        self.assertEqual(prop.inferred_zone, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(intelligence.partido_name, "Partido de Hurlingham")
        self.assertEqual(intelligence.locality_name, "Hurlingham")
        self.assertEqual(intelligence.zone_name, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(prop.manual_overrides, {"neighborhood": "Barrio Ingles"})
        self.assertEqual(prop.data_manually_corrected_at, corrected_at)


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

    def test_ingestion_creates_listing_identity_for_new_listing(self):
        listing, created = ingest_listing(self.source, self.data)

        self.assertTrue(created)
        identity = ListingIdentity.objects.get(
            source=self.source,
            external_id=listing.external_id,
        )
        self.assertEqual(identity.url, listing.url)
        self.assertEqual(identity.last_seen_reason, "ingest")

    def test_known_listing_identity_is_not_counted_as_created(self):
        ListingIdentity.objects.create(
            source=self.source,
            external_id=self.data["external_id"],
            url=self.data["url"],
            last_seen_reason="seed_discovery",
        )

        listing, created = ingest_listing(self.source, self.data)

        self.assertFalse(created)
        self.assertTrue(Listing.objects.filter(pk=listing.pk).exists())
        identity = ListingIdentity.objects.get(
            source=self.source,
            external_id=self.data["external_id"],
        )
        self.assertEqual(identity.last_seen_reason, "ingest")

    def test_manual_overrides_are_not_overwritten_by_ingestion(self):
        listing, _ = ingest_listing(self.source, self.data)
        property_obj = listing.property
        property_obj.price = Decimal("99000")
        property_obj.address = "Rolland 1200"
        property_obj.normalized_address = "rolland 1200"
        property_obj.manual_overrides = {"price": "manual", "address": "manual"}
        property_obj.save(update_fields=["price", "address", "normalized_address", "manual_overrides"])

        ingest_listing(
            self.source,
            dict(
                self.data,
                price="130000",
                address="Ocampo 2000",
                latitude=None,
                longitude=None,
            ),
        )

        property_obj.refresh_from_db()
        self.assertEqual(property_obj.price, Decimal("99000"))
        self.assertEqual(property_obj.address, "Rolland 1200")
        self.assertEqual(property_obj.normalized_address, "rolland 1200")

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

    def test_mark_missing_keeps_property_active_with_other_listing(self):
        other_source = Source.objects.create(
            slug="other-missing", name="Other Missing", base_url="https://other.example"
        )
        listing, _ = ingest_listing(self.source, self.data)
        other_listing, _ = ingest_listing(
            other_source,
            dict(
                self.data,
                external_id="abc-other-missing",
                url="https://other.example/abc",
            ),
        )
        self.assertEqual(listing.property_id, other_listing.property_id)

        mark_missing(self.source, [])
        mark_missing(self.source, [])

        listing.refresh_from_db()
        other_listing.refresh_from_db()
        listing.property.refresh_from_db()
        self.assertFalse(listing.active)
        self.assertTrue(other_listing.active)
        self.assertEqual(listing.property.status, Property.Status.ACTIVE)

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

    def test_geocoder_candidates_try_hurlingham_tesei_and_morris(self):
        garibaldi = Property.objects.create(
            fingerprint="geo-candidates-garibaldi",
            title="Casa Garibaldi",
            address="Gral Jose Garibaldi 3000",
            locality="Hurlingham",
        )
        garibaldi_candidates = Geocoder().query_candidates(garibaldi)
        self.assertIn(
            "Gral Jose Garibaldi 3000, William C. Morris, Buenos Aires, Argentina",
            garibaldi_candidates,
        )

        finochietto = Property.objects.create(
            fingerprint="geo-candidates-finochietto",
            title="Casa Finochietto",
            address="Finocchieto 1700",
            locality="Hurlingham",
        )
        finochietto_candidates = Geocoder().query_candidates(finochietto)
        self.assertIn(
            "Dip. Hector Finochietto 1700, Hurlingham, Buenos Aires, Argentina",
            finochietto_candidates,
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

    def test_geocoder_candidates_translate_intersections(self):
        batlle = Property.objects.create(
            fingerprint="geo-candidates-batlle",
            title="Lote Batlle",
            address="J. Batlle y Ordoñez e/ Lima y Misserere",
            locality="Villa Tesei",
        )
        candidates = Geocoder().query_candidates(batlle)
        self.assertIn(
            "José Batlle y Ordoñez esquina Lima, Villa Tesei, Buenos Aires, Argentina",
            candidates,
        )

        ginebra = Property.objects.create(
            fingerprint="geo-candidates-ginebra",
            title="Casa Ginebra",
            address="Ginebra e/ Atuel y Solís",
            locality="Hurlingham",
        )
        candidates = Geocoder().query_candidates(ginebra)
        self.assertIn(
            "Ginebra esquina Atuel, Hurlingham, Buenos Aires, Argentina",
            candidates,
        )

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

    @patch("properties.management.commands.repair_addresses.Geocoder")
    def test_repair_addresses_applies_curated_manual_batch(self, geocoder_cls):
        cases = [
            (1559, "Maestra Catalina G. de Pizzagalli 700", "Hurlingham", "Villa Tesei"),
            (4514, "Rolland al 1.200", "Hurlingham", "Hurlingham"),
            (1207, "Maestra A Gonzalez De Hecht 1200", "Villa Tesei", "Villa Tesei"),
            (1154, "Carhue 391. Entre Maestra Salinas y Las Provincias", "Villa Tesei", "Villa Tesei"),
            (1140, "Alberto Einstein 100", "Hurlingham", "Villa Tesei"),
            (4401, "Diego de Carbajal 600, Hurlingham", "Hurlingham", "Hurlingham"),
            (4393, "waksman 404", "Hurlingham", "Villa Tesei"),
            (1093, "J. Batlle y Ordoñez e/ Lima y Misserere", "Villa Tesei", "Villa Tesei"),
            (1086, "Ginebra e/ Atuel y Solís", "Hurlingham", "Hurlingham"),
            (1085, "Lavalle e/ Cañuelas y Dolores de Huici", "William C. Morris", "William C. Morris"),
            (37, "El Maestro Argentino 1800", "Hurlingham", "William C. Morris"),
            (163, "Diputado Finochietto 2000", "Hurlingham", "Hurlingham"),
            (719, "Gral Simon Bolivar 1700", "Hurlingham", "Hurlingham"),
            (1400, "Diputado Finochietto 2000", "Hurlingham", "Hurlingham"),
            (3758, "Maestra Argentino 1900", "Hurlingham", "William C. Morris"),
            (5693, "BALBOA 379", "Hurlingham", "Villa Tesei"),
            (5692, "General Pedro Díaz 2400", "Hurlingham", "William C. Morris"),
            (5680, "Félix Frías 2500", "Hurlingham", "Hurlingham"),
            (5679, "Valentín Alsina 2400", "Hurlingham", "Hurlingham"),
            (5678, "Guemes 1000", "Hurlingham", "Hurlingham"),
            (5677, "Av.Julio A Roca 1940", "Hurlingham", "Hurlingham"),
            (5674, "Tte. Gral. Julio Argentino Roca 1276", "Hurlingham", "Hurlingham"),
            (5643, "Francisco Miranda 1700", "Hurlingham", "Hurlingham"),
            (5630, "Richieri 1400", "Hurlingham", "Hurlingham"),
            (5623, "Hurlingham-conscripto Bernardi 1900", "Hurlingham", "Hurlingham"),
            (5616, "Teniente General Julio Argentino Roca 2700", "Hurlingham", "William C. Morris"),
            (5613, "Nilda Figueiras 1400", "Hurlingham", "Hurlingham"),
            (5611, "Tte. Gral. Julio Argentino Roca 1686", "Hurlingham", "Hurlingham"),
            (5566, "Manuel A. Ocampo 1900", "Hurlingham", "Hurlingham"),
            (5563, "General Bernardo O'Higgins 1918", "Hurlingham", "Hurlingham"),
            (5561, "Diego de Carabajal 800", "Hurlingham", "Hurlingham"),
            (5558, "Maestra A. González de Hecht 1100", "Hurlingham", "Villa Tesei"),
            (5544, "Pablo Pizzurno 441", "Hurlingham", "Hurlingham"),
            (5543, "Garibaldi 2600", "Hurlingham", "William C. Morris"),
            (5540, "avenida vergara 3604", "Hurlingham", "Hurlingham"),
            (5539, "GUEMES 1668", "Hurlingham", "Hurlingham"),
            (5537, "J. Bustamante y Guevara 2200", "Hurlingham", "Hurlingham"),
        ]
        for property_id, address, locality, _expected_locality in cases:
            Property.objects.create(
                id=property_id,
                fingerprint=f"repair-address-batch-{property_id}",
                title=f"Propiedad {property_id}",
                address=address,
                detected_address=address,
                locality=locality,
            )
        geocoder_cls.return_value.geocode_property.return_value = True

        output = StringIO()
        call_command(
            "repair_addresses",
            *[item for property_id, *_rest in cases for item in ("--property-id", str(property_id))],
            "--geocode",
            stdout=output,
        )

        expected = {
            1559: ("Maestra Catalina G. de Pizzagalli 700", "Villa Tesei", ""),
            4514: ("Rolland 1200", "Hurlingham", ""),
            1207: ("Maestra A. González de Hecht 1200", "Villa Tesei", "Santos Tesei"),
            1154: ("Carhué 391", "Villa Tesei", "Santos Tesei"),
            1140: ("Einstein 100", "Villa Tesei", ""),
            4401: ("Diego de Carvajal 600", "Hurlingham", "Parque Quirno"),
            4393: ("Waksman 404", "Villa Tesei", "Barrio Italia"),
            1093: ("José Batlle y Ordoñez esquina Lima", "Villa Tesei", "Santos Tesei"),
            1086: ("Ginebra esquina Atuel", "Hurlingham", ""),
            1085: ("Cañuelas esquina Dolores de Huici", "William C. Morris", ""),
            37: ("El Maestro Argentino 1800", "William C. Morris", ""),
            163: ("Dip. Hector Finochietto 2000", "Hurlingham", "Parque Johnston"),
            719: ("Gral. Simón Bolívar 1700", "Hurlingham", "Parque Johnston"),
            1400: ("Dip. Hector Finochietto 2000", "Hurlingham", "Parque Johnston"),
            3758: ("El Maestro Argentino 1900", "William C. Morris", ""),
            5693: ("Vasco Núñez de Balboa 379", "Villa Tesei", ""),
            5692: ("Gral. Pedro Díaz 2400", "William C. Morris", ""),
            5680: ("Félix Frías 2500", "Hurlingham", ""),
            5679: ("Valentín Alsina 2400", "Hurlingham", ""),
            5678: ("Gral. Martín Güemes 1000", "Hurlingham", ""),
            5677: ("Tte. Gral. Julio Argentino Roca 1940", "Hurlingham", ""),
            5674: ("Tte. Gral. Julio Argentino Roca 1276", "Hurlingham", ""),
            5643: ("Gral. Francisco Miranda 1700", "Hurlingham", ""),
            5630: ("Tte. Gral. Pablo Ricchieri 1400", "Hurlingham", ""),
            5623: ("Conscripto Bernardi 1900", "Hurlingham", ""),
            5616: ("Tte. Gral. Julio Argentino Roca 2700", "William C. Morris", ""),
            5613: ("Nilda Figueira 1400", "Hurlingham", ""),
            5611: ("Tte. Gral. Julio Argentino Roca 1686", "Hurlingham", ""),
            5566: ("Manuel A. Ocampo 1900", "Hurlingham", ""),
            5563: ("Gral. Bernardo O'Higgins 1918", "Hurlingham", ""),
            5561: ("Diego de Carvajal 800", "Hurlingham", "Parque Quirno"),
            5558: ("Maestra A. González de Hecht 1100", "Villa Tesei", "Santos Tesei"),
            5544: ("Pablo Pizzurno 441", "Hurlingham", ""),
            5543: ("José Garibaldi 2600", "William C. Morris", ""),
            5540: ("Av. Gdor. Vergara 3604", "Hurlingham", ""),
            5539: ("Gral. Martín Güemes 1668", "Hurlingham", ""),
            5537: ("Eva Perón 2200 esquina Guevara", "Hurlingham", ""),
        }
        for property_id, (address, locality, neighborhood) in expected.items():
            property_obj = Property.objects.get(pk=property_id)
            self.assertEqual(property_obj.address, address)
            self.assertEqual(property_obj.locality, locality)
            if neighborhood:
                self.assertEqual(property_obj.neighborhood, neighborhood)
        self.assertEqual(geocoder_cls.return_value.geocode_property.call_count, 37)
        self.assertIn("37 direcciones corregidas", output.getvalue())

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

    @patch("properties.management.commands.repair_merged_listings.get_adapter")
    def test_repair_merged_listings_splits_riquelme_active_and_sold(self, get_adapter):
        source = Source.objects.create(
            slug="riquelme",
            name="Riquelme Propiedades",
            base_url="https://www.riquelmepropiedades.com.ar",
        )
        property_obj = Property.objects.create(
            fingerprint="merged-riquelme",
            title="Hurlingham: Villa Tesei. 2 chalet de 4 amb. A terminar",
            address="Bizet",
            locality="Villa Tesei",
            currency="USD",
            price=125000,
        )
        active_listing = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="venta-de-casa-en-villa-tesei-hurlingham-buenos-aires-708-93836",
            url="https://www.riquelmepropiedades.com.ar/propiedad/venta-de-casa-en-villa-tesei-hurlingham-buenos-aires-708-93836",
        )
        sold_listing = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="venta-de-casa-en-villa-tesei-hurlingham-buenos-aires-708-8505",
            url="https://www.riquelmepropiedades.com.ar/propiedad/venta-de-casa-en-villa-tesei-hurlingham-buenos-aires-708-8505",
        )

        class FakeAdapter:
            def parse(self, url):
                if url.endswith("93836"):
                    return {
                        "external_id": "venta-de-casa-en-villa-tesei-hurlingham-buenos-aires-708-93836",
                        "url": url,
                        "title": "Hurlingham: Villa Tesei. 2 chalet de 4 amb. A terminar",
                        "address": "Bizet",
                        "locality": "Villa Tesei",
                        "property_type": "house",
                        "currency": "USD",
                        "price": 125000,
                        "status": Property.Status.ACTIVE,
                        "source_status": "",
                    }
                return {
                    "external_id": "venta-de-casa-en-villa-tesei-hurlingham-buenos-aires-708-8505",
                    "url": url,
                    "title": "Casa en Venta en Villa Tesei, Hurlingham, Buenos Aires Camargo",
                    "address": "Camargo",
                    "locality": "Villa Tesei",
                    "property_type": "house",
                    "currency": "USD",
                    "price": 90000,
                    "status": Property.Status.SOLD,
                    "source_status": "sold",
                    "raw_data": {"riquelme_status_badge": "sold"},
                }

        get_adapter.return_value = FakeAdapter()

        output = StringIO()
        call_command(
            "repair_merged_listings",
            "--property-id",
            str(property_obj.pk),
            "--source",
            "riquelme",
            stdout=output,
        )

        active_listing.refresh_from_db()
        sold_listing.refresh_from_db()
        property_obj.refresh_from_db()
        self.assertEqual(active_listing.property_id, property_obj.pk)
        self.assertEqual(property_obj.status, Property.Status.ACTIVE)
        self.assertEqual(active_listing.source_status, "")
        self.assertNotEqual(sold_listing.property_id, property_obj.pk)
        self.assertEqual(sold_listing.source_status, "sold")
        self.assertEqual(sold_listing.property.status, Property.Status.SOLD)
        self.assertEqual(sold_listing.raw_data["riquelme_status_badge"], "sold")
        self.assertIn("1 publicaciones separadas", output.getvalue())


class MergePropertiesCommandTests(TestCase):
    def create_property(self, title, **overrides):
        defaults = {
            "fingerprint": f"merge-test-{title.lower().replace(' ', '-')}",
            "title": title,
            "operation": "sale",
            "status": Property.Status.ACTIVE,
            "property_type": Property.Type.HOUSE,
            "currency": "USD",
            "price": Decimal("100000"),
            "address": title,
        }
        defaults.update(overrides)
        return Property.objects.create(**defaults)

    def create_listing(self, source, property_obj, external_id, url):
        return Listing.objects.create(
            source=source,
            property=property_obj,
            external_id=external_id,
            url=url,
        )

    def test_merge_properties_detects_url_tail_components_without_writing_on_dry_run(self):
        argenprop = Source.objects.create(
            slug="argenprop",
            name="Argenprop",
            base_url="https://www.argenprop.com",
        )
        clarin = Source.objects.create(
            slug="inmuebles-clarin",
            name="Inmuebles Clarin",
            base_url="https://www.inmuebles.clarin.com",
        )
        first = self.create_property("Casa A")
        middle = self.create_property("Casa B")
        last = self.create_property("Casa C")
        self.create_listing(argenprop, first, "a1", "https://www.argenprop.com/casa--100")
        self.create_listing(clarin, middle, "c1", "https://www.inmuebles.clarin.com/casa--100")
        self.create_listing(argenprop, middle, "a2", "https://www.argenprop.com/casa--200")
        self.create_listing(clarin, last, "c2", "https://www.inmuebles.clarin.com/casa--200")

        output = StringIO()
        call_command(
            "merge_properties",
            "--detect-url-tail-sources",
            "argenprop,inmuebles-clarin",
            "--dry-run",
            stdout=output,
        )

        text = output.getvalue()
        self.assertIn("Componentes a procesar: 1", text)
        self.assertIn("propiedades involucradas: 3", text)
        self.assertEqual(first.listings.count(), 1)
        self.assertEqual(middle.listings.count(), 2)
        self.assertEqual(last.listings.count(), 1)
        self.assertFalse(middle.is_hidden)
        self.assertEqual(middle.status, Property.Status.ACTIVE)

    def test_merge_properties_component_preserves_state_notes_and_listings(self):
        source = Source.objects.create(
            slug="manual",
            name="Manual",
            base_url="https://example.com",
        )
        canonical_seed = self.create_property("Casa visible", covered_area=Decimal("120"))
        favorite = self.create_property(
            "Casa favorita",
            is_favorite=True,
            is_hidden=True,
            personal_notes="Nota favorita",
            bedrooms=3,
        )
        noted = self.create_property(
            "Casa anotada",
            personal_notes="Nota secundaria",
            status=Property.Status.RESERVED,
        )
        self.create_listing(source, canonical_seed, "l1", "https://example.com/1")
        self.create_listing(source, favorite, "l2", "https://example.com/2")
        self.create_listing(source, noted, "l3", "https://example.com/3")

        call_command(
            "merge_properties",
            "--component",
            f"{canonical_seed.pk},{favorite.pk},{noted.pk}",
            stdout=StringIO(),
        )

        canonical_seed.refresh_from_db()
        favorite.refresh_from_db()
        noted.refresh_from_db()
        self.assertFalse(canonical_seed.is_hidden)
        self.assertEqual(canonical_seed.status, Property.Status.ACTIVE)
        self.assertTrue(canonical_seed.is_favorite)
        self.assertEqual(canonical_seed.bedrooms, 3)
        self.assertIn("Nota favorita", canonical_seed.personal_notes)
        self.assertIn("Nota secundaria", canonical_seed.personal_notes)
        self.assertEqual(canonical_seed.listings.count(), 3)
        self.assertTrue(favorite.is_hidden)
        self.assertEqual(favorite.status, Property.Status.REMOVED)
        self.assertEqual(favorite.listings.count(), 0)
        self.assertTrue(noted.is_hidden)
        self.assertEqual(noted.status, Property.Status.REMOVED)
        self.assertEqual(noted.listings.count(), 0)

    def test_merge_properties_component_respects_canonical_id(self):
        source = Source.objects.create(
            slug="manual-canonical",
            name="Manual Canonical",
            base_url="https://example.com",
        )
        first = self.create_property("Casa primera", bedrooms=2)
        middle = self.create_property("Casa media", bedrooms=3, is_favorite=True)
        chosen = self.create_property("Casa elegida", bedrooms=1)
        self.create_listing(source, first, "l1", "https://example.com/1")
        self.create_listing(source, middle, "l2", "https://example.com/2")
        self.create_listing(source, chosen, "l3", "https://example.com/3")

        call_command(
            "merge_properties",
            "--component",
            f"{first.pk},{middle.pk},{chosen.pk}",
            "--canonical-id",
            str(chosen.pk),
            stdout=StringIO(),
        )

        first.refresh_from_db()
        middle.refresh_from_db()
        chosen.refresh_from_db()
        self.assertFalse(chosen.is_hidden)
        self.assertEqual(chosen.status, Property.Status.ACTIVE)
        self.assertEqual(chosen.listings.count(), 3)
        self.assertTrue(chosen.is_favorite)
        self.assertTrue(first.is_hidden)
        self.assertTrue(middle.is_hidden)


class RepairZonapropDetailsCommandTests(TestCase):
    def create_zonaprop_listing(self, property_id=5368, **property_overrides):
        source, _ = Source.objects.get_or_create(
            slug="zonaprop",
            defaults={"name": "Zonaprop", "base_url": "https://www.zonaprop.com.ar"},
        )
        defaults = {
            "id": property_id,
            "fingerprint": f"zonaprop-repair-{property_id}",
            "title": "Casa Zonaprop",
            "operation": "sale",
            "status": Property.Status.ACTIVE,
            "property_type": Property.Type.HOUSE,
            "locality": "Hurlingham",
        }
        defaults.update(property_overrides)
        property_obj = Property.objects.create(**defaults)
        listing = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id=f"zp-{property_id}",
            url=f"https://www.zonaprop.com.ar/propiedades/clasificado/test-{property_id}.html",
        )
        return property_obj, listing

    def test_repair_zonaprop_details_dry_run_does_not_write(self):
        property_obj, listing = self.create_zonaprop_listing()

        output = StringIO()
        call_command(
            "repair_zonaprop_details",
            "--property-id",
            str(property_obj.pk),
            "--skip-live",
            stdout=output,
        )

        property_obj.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(property_obj.address, "")
        self.assertEqual(property_obj.total_area, None)
        self.assertEqual(listing.raw_data, {})
        self.assertIn("dry-run", output.getvalue())

    def test_repair_zonaprop_details_apply_updates_known_case_and_raw_data(self):
        property_obj, listing = self.create_zonaprop_listing()

        call_command(
            "repair_zonaprop_details",
            "--property-id",
            str(property_obj.pk),
            "--skip-live",
            "--apply",
            stdout=StringIO(),
        )

        property_obj.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(property_obj.address, "Williams 2328")
        self.assertEqual(property_obj.detected_address, "Williams 2328")
        self.assertEqual(property_obj.total_area, Decimal("217"))
        self.assertEqual(property_obj.covered_area, Decimal("100"))
        self.assertEqual(property_obj.rooms, 3)
        self.assertEqual(property_obj.bedrooms, 2)
        self.assertEqual(property_obj.age_years, 30)
        self.assertEqual(
            listing.raw_data["zonaprop_repair"]["fields"]["address"],
            "Williams 2328",
        )

    def test_repair_zonaprop_details_preserves_manual_overrides(self):
        property_obj, _listing = self.create_zonaprop_listing(
            property_id=5371,
            address="Manual 123",
            manual_overrides={"address": "Manual 123"},
        )

        call_command(
            "repair_zonaprop_details",
            "--property-id",
            str(property_obj.pk),
            "--skip-live",
            "--apply",
            stdout=StringIO(),
        )

        property_obj.refresh_from_db()
        self.assertEqual(property_obj.address, "Manual 123")
        self.assertEqual(property_obj.total_area, Decimal("186"))


class RepairMapapropStatusesCommandTests(TestCase):
    class FakeAdapter:
        def __init__(self, payloads):
            self.payloads = payloads

        def parse(self, url):
            return self.payloads[url]

    def create_mapaprop_listing(self, **property_overrides):
        source, _ = Source.objects.get_or_create(
            slug="mapaprop",
            defaults={"name": "Mapaprop", "base_url": "https://www.mapaprop.com"},
        )
        defaults = {
            "fingerprint": f"mapaprop-repair-{Property.objects.count()}",
            "title": "Casa Mapaprop",
            "operation": "sale",
            "status": Property.Status.ACTIVE,
            "property_type": Property.Type.HOUSE,
            "currency": "USD",
            "price": Decimal("1"),
        }
        defaults.update(property_overrides)
        property_obj = Property.objects.create(**defaults)
        listing = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id=f"repair-{property_obj.pk}",
            url=f"https://www.mapaprop.com/en/property/repair-{property_obj.pk}",
        )
        return property_obj, listing

    def test_repair_mapaprop_dry_run_does_not_write(self):
        property_obj, listing = self.create_mapaprop_listing()
        parsed = {
            listing.url: {
                "status": Property.Status.RESERVED,
                "source_status": "reserved",
                "price": None,
                "currency": "",
                "raw_data": {"mapaprop_price_hidden_reason": "placeholder_price_1"},
            }
        }

        output = StringIO()
        with patch(
            "properties.management.commands.repair_mapaprop_statuses.get_adapter",
            return_value=self.FakeAdapter(parsed),
        ):
            call_command(
                "repair_mapaprop_statuses",
                "--property-id",
                str(property_obj.pk),
                stdout=output,
            )

        property_obj.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(property_obj.status, Property.Status.ACTIVE)
        self.assertEqual(property_obj.price, Decimal("1.00"))
        self.assertEqual(property_obj.currency, "USD")
        self.assertEqual(listing.source_status, "")
        self.assertIn("ACTUALIZARIA", output.getvalue())

    def test_repair_mapaprop_apply_updates_state_price_and_raw_data(self):
        property_obj, listing = self.create_mapaprop_listing()
        parsed = {
            listing.url: {
                "status": Property.Status.RESERVED,
                "source_status": "reserved",
                "price": None,
                "currency": "",
                "raw_data": {"mapaprop_price_hidden_reason": "placeholder_price_1"},
            }
        }

        with patch(
            "properties.management.commands.repair_mapaprop_statuses.get_adapter",
            return_value=self.FakeAdapter(parsed),
        ):
            call_command(
                "repair_mapaprop_statuses",
                "--property-id",
                str(property_obj.pk),
                "--apply",
                stdout=StringIO(),
            )

        property_obj.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(property_obj.status, Property.Status.RESERVED)
        self.assertIsNone(property_obj.price)
        self.assertEqual(property_obj.currency, "")
        self.assertEqual(listing.source_status, "reserved")
        self.assertEqual(listing.raw_data["mapaprop_price_hidden_reason"], "placeholder_price_1")
        self.assertEqual(listing.raw_data["mapaprop_repair"]["status"], Property.Status.RESERVED)

    def test_repair_mapaprop_preserves_manual_overrides(self):
        property_obj, listing = self.create_mapaprop_listing(
            price=Decimal("123000"),
            currency="USD",
            manual_overrides={"price": "manual", "status": "manual"},
        )
        parsed = {
            listing.url: {
                "status": Property.Status.SOLD,
                "source_status": "sold",
                "price": None,
                "currency": "",
                "raw_data": {"mapaprop_price_hidden_reason": "placeholder_price_1"},
            }
        }

        with patch(
            "properties.management.commands.repair_mapaprop_statuses.get_adapter",
            return_value=self.FakeAdapter(parsed),
        ):
            call_command(
                "repair_mapaprop_statuses",
                "--property-id",
                str(property_obj.pk),
                "--apply",
                stdout=StringIO(),
            )

        property_obj.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(property_obj.status, Property.Status.ACTIVE)
        self.assertEqual(property_obj.price, Decimal("123000.00"))
        self.assertEqual(property_obj.currency, "USD")
        self.assertEqual(listing.source_status, "sold")
        self.assertIn("status", listing.raw_data["mapaprop_repair"]["protected_fields"])
        self.assertIn("price", listing.raw_data["mapaprop_repair"]["protected_fields"])
        self.assertIn("currency", listing.raw_data["mapaprop_repair"]["protected_fields"])

    def test_repair_mapaprop_keeps_valid_price_from_sibling_listing(self):
        property_obj, first_listing = self.create_mapaprop_listing()
        second_listing = Listing.objects.create(
            source=first_listing.source,
            property=property_obj,
            external_id=f"repair-{property_obj.pk}-b",
            url=f"https://www.mapaprop.com/en/property/repair-{property_obj.pk}-b",
        )
        parsed = {
            first_listing.url: {
                "status": Property.Status.SOLD,
                "source_status": "sold",
                "price": Decimal("230000"),
                "currency": "USD",
                "raw_data": {},
            },
            second_listing.url: {
                "status": Property.Status.SOLD,
                "source_status": "sold",
                "price": None,
                "currency": "",
                "raw_data": {"mapaprop_price_hidden_reason": "placeholder_price_1"},
            },
        }

        with patch(
            "properties.management.commands.repair_mapaprop_statuses.get_adapter",
            return_value=self.FakeAdapter(parsed),
        ):
            call_command(
                "repair_mapaprop_statuses",
                "--property-id",
                str(property_obj.pk),
                "--apply",
                stdout=StringIO(),
            )

        property_obj.refresh_from_db()
        first_listing.refresh_from_db()
        second_listing.refresh_from_db()
        self.assertEqual(property_obj.status, Property.Status.SOLD)
        self.assertEqual(property_obj.price, Decimal("230000.00"))
        self.assertEqual(property_obj.currency, "USD")
        self.assertEqual(first_listing.source_status, "sold")
        self.assertEqual(second_listing.source_status, "sold")


class OperationRunnerTests(TestCase):
    def _location_geojson_path(self):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "location_value.geojson"
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "zone_name": "Zona Runner",
                        "overall_location_value_score": 69,
                        "location_value_level": "media_alta",
                        "transport_access_score": 70,
                        "in_flood_risk_zone": False,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-58.70, -34.66],
                            [-58.60, -34.66],
                            [-58.60, -34.55],
                            [-58.70, -34.55],
                            [-58.70, -34.66],
                        ]],
                    },
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_geocode_dry_run_does_not_create_location(self):
        property_obj = Property.objects.create(
            fingerprint="operation-geocode-dry-run",
            title="Casa con direccion",
            address="Villegas 1200",
            normalized_address=normalize_address("Villegas 1200"),
            locality="Hurlingham",
            price=Decimal("100000"),
            currency="USD",
        )
        job = create_operation_job(
            kind=OperationJob.Kind.GEOCODE,
            mode=OperationJob.Mode.DRY_RUN,
            steps=[
                {
                    "kind": OperationJob.Kind.GEOCODE,
                    "mode": OperationJob.Mode.DRY_RUN,
                    "params": {"limit": 10, "cache_only": False},
                }
            ],
        )

        run_operation_job(job.pk)

        job.refresh_from_db()
        step = job.steps.get()
        self.assertEqual(job.status, OperationJob.Status.SUCCESS)
        self.assertEqual(step.processed, 1)
        self.assertFalse(PropertyLocation.objects.filter(property=property_obj).exists())

    def test_location_intelligence_operation_step_scores_properties(self):
        property_obj = Property.objects.create(
            fingerprint="operation-location-intelligence",
            title="Casa territorial runner",
            operation="sale",
            status=Property.Status.ACTIVE,
        )
        PropertyLocation.objects.create(
            property=property_obj,
            latitude=-34.60,
            longitude=-58.64,
            precision=PropertyLocation.Precision.EXACT,
        )
        job = create_operation_job(
            kind=OperationJob.Kind.SCORE_LOCATION_INTELLIGENCE,
            mode=OperationJob.Mode.APPLY,
            steps=[
                {
                    "kind": OperationJob.Kind.SCORE_LOCATION_INTELLIGENCE,
                    "mode": OperationJob.Mode.APPLY,
                    "params": {"geojson": str(self._location_geojson_path())},
                }
            ],
        )

        run_operation_job(job.pk)

        job.refresh_from_db()
        property_obj.refresh_from_db()
        self.assertEqual(job.status, OperationJob.Status.SUCCESS)
        self.assertEqual(property_obj.location_intelligence.overall_score, 69)
        self.assertEqual(property_obj.location_intelligence.zone_name, "Zona Runner")


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

    def _dashboard_payload(self, response):
        chart_data = json.loads(
            BeautifulSoup(response.content, "lxml").find(id="chart-data").string
        )
        self.assertFalse(chart_data["loaded"])
        self.assertIn("data_url", chart_data)
        data_response = self.client.get(chart_data["data_url"])
        self.assertEqual(data_response.status_code, 200)
        return data_response.json()

    def test_search_and_geojson_filters(self):
        response = self.client.get("/", {"q": "pileta", "bedrooms_min": 3})
        self.assertContains(response, "Casa con pileta")
        response = self.client.get(
            "/api/propiedades/",
            {"radius_lat": -34.59, "radius_lng": -58.64, "radius_km": 1},
        )
        payload = response.json()
        self.assertEqual(len(payload["features"]), 1)

    def test_search_renders_property_preview_modal(self):
        response = self.client.get("/")

        self.assertContains(response, 'id="property-preview-modal"')
        self.assertContains(response, 'src="/static/js/property-preview.js"')
        self.assertContains(response, 'class="icon-button small property-infer-zone"', count=1)

        table_response = self.client.get("/", {"view": "table"})
        self.assertContains(table_response, 'class="icon-button small property-infer-zone"', count=1)

    def test_search_export_and_view_controls_are_in_results_toolbar(self):
        response = self.client.get("/", {"view": "table", "q": "pileta"})
        soup = BeautifulSoup(response.content, "lxml")

        export_menu = soup.select_one(".header-actions .export-menu")
        self.assertIsNotNone(export_menu)
        self.assertEqual(len(export_menu.select('a[href*="/export/properties.csv"]')), 1)
        self.assertEqual(len(export_menu.select('a[href*="/export/properties.xlsx"]')), 1)
        self.assertFalse(soup.select('#search-form input[type="radio"][name="view"]'))
        self.assertIsNotNone(soup.select_one("#results-pane .results-view-toggle"))

    def test_infer_property_territory_api_updates_territory_and_score(self):
        property_obj = self.listing.property
        territory_result = SimpleNamespace(
            partido="Partido de Hurlingham",
            locality="Hurlingham",
            zone="Parque Johnston",
            confidence="medium_high",
            source_method="test",
            needs_review=False,
            evidence={"signature": "test"},
        )
        score_result = SimpleNamespace(
            overall_score=71,
            level="media_alta",
            zone_name="Parque Johnston",
            match_method="polygon",
            confidence="medium_high",
            transport_score=70,
            education_score=72,
            health_score=73,
            flood_penalty_score=0,
            urban_informality_score=0,
            environmental_penalty_score=0,
            development_potential_score=64,
            in_flood_risk_zone=False,
            nearest_renabap_m=None,
            nearest_sube_point_m=180,
            nearest_school_m=260,
            nearest_health_center_m=420,
            components={"test": True},
            risks={},
            evidence={"source": "test"},
            source_signature="score-test",
        )

        with patch("properties.views.infer_property_territory", return_value=territory_result), patch(
            "properties.views.load_location_zones",
            return_value={"configured": True, "features": [], "signature": "score-test"},
        ), patch("properties.views.score_property_location_intelligence", return_value=score_result):
            response = self.client.post(f"/api/propiedad/{property_obj.pk}/inferir-territorio/")

        self.assertEqual(response.status_code, 200)
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.inferred_partido, "Partido de Hurlingham")
        self.assertEqual(property_obj.inferred_locality, "Hurlingham")
        self.assertEqual(property_obj.inferred_zone, "Parque Johnston")
        intelligence = property_obj.location_intelligence
        self.assertEqual(intelligence.overall_score, 71)
        self.assertEqual(intelligence.zone_name, "Parque Johnston")
        payload = response.json()
        self.assertEqual(payload["territory"]["zone"], "Parque Johnston")
        self.assertEqual(payload["location_intelligence"]["overall_score"], 71)
        self.assertEqual(payload["location"]["latitude"], property_obj.location.latitude)
        self.assertEqual(payload["location"]["longitude"], property_obj.location.longitude)
        self.assertEqual(payload["location"]["precision"], PropertyLocation.Precision.EXACT)

    def test_infer_property_territory_api_requires_coordinates(self):
        property_obj = Property.objects.create(
            fingerprint="without-location",
            title="Sin ubicacion",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
        )

        with patch("properties.views.Geocoder") as geocoder_cls:
            geocoder_cls.return_value.geocode_property.return_value = None
            response = self.client.post(f"/api/propiedad/{property_obj.pk}/inferir-territorio/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("coordenadas", response.json()["error"])
        geocoder_cls.return_value.geocode_property.assert_not_called()
        self.assertFalse(PropertyLocation.objects.filter(property=property_obj).exists())

    def test_infer_property_territory_api_geocodes_missing_coordinates(self):
        property_obj = Property.objects.create(
            fingerprint="without-location-geocode",
            title="Sin ubicacion con direccion",
            address="Rolland 1200",
            locality="Hurlingham",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
        )
        territory_result = SimpleNamespace(
            partido="Partido de Hurlingham",
            locality="Hurlingham",
            zone="Parque Johnston",
            confidence="medium_high",
            source_method="geocode_test",
            needs_review=False,
            evidence={"source": "geocode"},
        )

        with patch("properties.views.Geocoder") as geocoder_cls, patch(
            "properties.views.infer_property_territory",
            return_value=territory_result,
        ) as infer_mock, patch(
            "properties.views.load_location_zones",
            return_value={"configured": False, "features": [], "signature": ""},
        ):
            def geocode_property(prop):
                return PropertyLocation.objects.create(
                    property=prop,
                    latitude=-34.591,
                    longitude=-58.641,
                    precision=PropertyLocation.Precision.EXACT,
                    provider="nominatim",
                    confidence=0.8,
                )

            geocoder_cls.return_value.geocode_property.side_effect = geocode_property
            response = self.client.post(f"/api/propiedad/{property_obj.pk}/inferir-territorio/")

        self.assertEqual(response.status_code, 200)
        geocoder_cls.return_value.geocode_property.assert_called_once()
        inferred_property = infer_mock.call_args.args[0]
        self.assertEqual(inferred_property.location.latitude, -34.591)
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.inferred_zone, "Parque Johnston")
        payload = response.json()
        self.assertEqual(payload["location"]["latitude"], -34.591)
        self.assertEqual(payload["location"]["longitude"], -58.641)
        self.assertEqual(payload["location"]["provider"], "nominatim")

    def test_security_filters_are_applied_to_geojson_api(self):
        primary = self.listing.property
        primary.security_coverage_score = 72
        primary.security_risk_score = 28
        primary.security_level = "alta"
        primary.security_zone_label = "Zona Test"
        primary.save(
            update_fields=[
                "security_coverage_score",
                "security_risk_score",
                "security_level",
                "security_zone_label",
            ]
        )
        other = Property.objects.create(
            fingerprint="security-filter-low",
            title="Casa bajo score",
            operation="sale",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
            security_coverage_score=30,
            security_risk_score=70,
            security_level="baja",
            security_zone_label="Zona Baja",
        )
        PropertyLocation.objects.create(
            property=other,
            latitude=-34.61,
            longitude=-58.65,
            precision=PropertyLocation.Precision.EXACT,
        )

        response = self.client.get(
            "/api/propiedades/",
            {"security_coverage_min": "60", "security_level": "alta"},
        )
        ids = {feature["id"] for feature in response.json()["features"]}

        self.assertIn(primary.pk, ids)
        self.assertNotIn(other.pk, ids)
        feature = next(feature for feature in response.json()["features"] if feature["id"] == primary.pk)
        self.assertEqual(feature["properties"]["security_coverage_score"], 72)

    def test_location_intelligence_filters_payloads_exports_and_stats(self):
        primary = self.listing.property
        PropertyLocationIntelligence.objects.create(
            property=primary,
            overall_score=74,
            level="alta",
            zone_name="Zona Alta",
            match_method="coordinates",
            confidence="high",
            transport_score=82,
            education_score=61,
            health_score=50,
            flood_penalty_score=12,
            in_flood_risk_zone=False,
            nearest_renabap_m=450,
            evidence={"matched_zone": "Zona Alta"},
        )
        other = Property.objects.create(
            fingerprint="location-filter-low",
            title="Casa territorial baja",
            operation="sale",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
            currency="USD",
            price=100000,
        )
        PropertyLocation.objects.create(
            property=other,
            latitude=-34.61,
            longitude=-58.65,
            precision=PropertyLocation.Precision.EXACT,
        )
        PropertyLocationIntelligence.objects.create(
            property=other,
            overall_score=35,
            level="baja",
            zone_name="Zona Baja",
            match_method="coordinates",
            transport_score=20,
            flood_penalty_score=70,
            in_flood_risk_zone=True,
            nearest_renabap_m=80,
        )

        response = self.client.get(
            "/api/propiedades/",
            {"location_score_min": "60", "location_value_level": "alta"},
        )
        features = response.json()["features"]
        ids = {feature["id"] for feature in features}
        self.assertIn(primary.pk, ids)
        self.assertNotIn(other.pk, ids)
        self.assertEqual(features[0]["properties"]["location_value_score"], 74)

        response = self.client.get(f"/api/propiedad/{primary.pk}/resumen/")
        payload = response.json()
        self.assertEqual(payload["location_intelligence"]["overall_score"], 74)
        self.assertEqual(payload["location_intelligence"]["zone_name"], "Zona Alta")
        self.assertIn("facts", payload)
        self.assertIn("edit_sections", payload)
        self.assertIn("map_config", payload)
        self.assertIn("detail_url", payload)

        response = self.client.get("/export/properties.csv")
        self.assertContains(response, "score_territorial")
        self.assertContains(response, "Zona Alta")

        response = self.client.get("/estadisticas/")
        self.assertContains(response, "Score territorial + precio")
        payload = self._dashboard_payload(response)
        self.assertIn("location_intelligence", payload)

    def test_default_filters_show_only_active_status(self):
        Property.objects.create(
            fingerprint="status-active-filter",
            title="Casa activa filtro",
            property_type=Property.Type.HOUSE,
            operation="sale",
            status=Property.Status.ACTIVE,
        )
        statuses = [
            Property.Status.SOLD,
            Property.Status.RESERVED,
            Property.Status.SUSPENDED,
            Property.Status.REMOVED,
        ]
        for status in statuses:
            Property.objects.create(
                fingerprint=f"status-{status}-filter",
                title=f"Casa {status}",
                property_type=Property.Type.HOUSE,
                operation="sale",
                status=status,
            )

        response = self.client.get("/")
        self.assertContains(response, "Casa activa filtro")
        for status in statuses:
            self.assertNotContains(response, f"Casa {status}")

        response = self.client.get("/", {"status": Property.Status.SUSPENDED})
        self.assertContains(response, "Casa suspended")
        self.assertNotContains(response, "Casa activa filtro")

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

    def test_detail_renders_complete_property_data_editor(self):
        response = self.client.get(f"/propiedad/{self.listing.property_id}/")

        self.assertContains(response, "Datos de la propiedad")
        self.assertContains(response, 'id="property-data-form"')
        self.assertContains(response, 'name="property_type"')
        self.assertContains(response, 'name="covered_area"')
        self.assertContains(response, 'list="property-locality-options"')
        self.assertContains(response, 'list="property-neighborhood-options"')
        self.assertContains(response, "Zona declarada/manual")
        self.assertContains(response, 'value="El Destino"')
        self.assertContains(response, 'value="Cartero"')
        self.assertContains(response, 'value="Parque Johnston"')
        self.assertNotContains(response, "Casa en Venta en Hurlingham")
        self.assertNotContains(response, "Chalet en Venta")
        self.assertContains(response, 'id="property-edit-payload"')

    def test_detail_zone_datalist_keeps_current_manual_value_only(self):
        property_obj = self.listing.property
        property_obj.neighborhood = "Zona Manual Nueva"
        property_obj.save(update_fields=["neighborhood"])
        Property.objects.create(
            fingerprint="dirty-zone-option",
            title="Propiedad con zona sucia",
            neighborhood="Casa en Venta en Hurlingham",
            property_type=Property.Type.HOUSE,
            status=Property.Status.ACTIVE,
        )

        response = self.client.get(f"/propiedad/{property_obj.pk}/")

        self.assertContains(response, 'value="Zona Manual Nueva"')
        self.assertContains(response, "Zona Manual Nueva (actual/manual)")
        self.assertNotContains(response, "Casa en Venta en Hurlingham")

    def test_active_filters_persist_from_radar_to_dashboard(self):
        self.client.get("/", {"price_max": "200000", "sort": "price"})

        response = self.client.get("/estadisticas/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/estadisticas/?price_max=200000&sort=price")

    def test_active_filters_persist_from_dashboard_to_radar(self):
        self.client.get("/estadisticas/", {"locality": "Villa Tesei", "view": "table"})

        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/?locality=Villa+Tesei&view=table")

    def test_clear_filters_removes_active_session_filter(self):
        self.client.get("/", {"price_max": "200000"})

        response = self.client.get("/", {"clear_filters": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertNotIn("radar_active_filters", self.client.session)

        response = self.client.get("/estadisticas/")
        self.assertEqual(response.status_code, 200)

    def test_navigation_links_use_active_filter_query(self):
        response = self.client.get("/", {"price_max": "200000", "sort": "price"})
        self.assertContains(response, 'href="/estadisticas/?price_max=200000&amp;sort=price"')
        self.assertContains(response, 'href="/export/properties.csv?price_max=200000&amp;sort=price"')
        self.assertContains(response, "window.RADAR_CLEAR_FILTERS_URL")
        self.assertContains(response, "clear_filters\\u003D1")

        response = self.client.get("/estadisticas/", {"price_max": "200000", "sort": "price"})
        self.assertContains(response, 'href="/?price_max=200000&amp;sort=price"')
        self.assertContains(response, 'href="/export/properties.csv?price_max=200000&amp;sort=price"')
        self.assertContains(response, 'href="/estadisticas/?clear_filters=1"')

    def test_property_data_endpoint_updates_fields_and_marks_overrides(self):
        property_obj = self.listing.property
        self.assertTrue(PropertyLocation.objects.filter(property=property_obj).exists())

        response = self.client.post(
            f"/api/propiedad/{property_obj.pk}/datos/",
            data=json.dumps(
                {
                    "address": "Rolland al 1.200",
                    "locality": "Nueva Localidad Manual",
                    "price": "160000",
                    "neighborhood": "Barrio Ingles",
                    "bedrooms": "4",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.address, "Rolland 1200")
        self.assertEqual(property_obj.normalized_address, "rolland 1200")
        self.assertEqual(property_obj.locality, "Nueva Localidad Manual")
        self.assertEqual(property_obj.price, Decimal("160000"))
        self.assertEqual(property_obj.neighborhood, UNIFIED_HURLINGHAM_CENTRO_ZONE)
        self.assertEqual(property_obj.bedrooms, 4)
        self.assertIn("address", property_obj.manual_overrides)
        self.assertIn("locality", property_obj.manual_overrides)
        self.assertIn("price", property_obj.manual_overrides)
        self.assertFalse(PropertyLocation.objects.filter(property=property_obj).exists())

    def test_property_data_endpoint_rejects_unknown_fields(self):
        response = self.client.post(
            f"/api/propiedad/{self.listing.property_id}/datos/",
            data=json.dumps({"fingerprint": "nope"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Campos no editables", response.json()["error"])

    def test_property_data_endpoint_keeps_manual_location(self):
        property_obj = self.listing.property
        location = property_obj.location
        location.manually_corrected = True
        location.precision = PropertyLocation.Precision.MANUAL
        location.provider = "manual"
        location.save(update_fields=["manually_corrected", "precision", "provider"])

        response = self.client.post(
            f"/api/propiedad/{property_obj.pk}/datos/",
            data=json.dumps({"address": "Diego de Carbajal 600"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        property_obj.refresh_from_db()
        self.assertEqual(property_obj.address, "Diego de Carvajal 600")
        self.assertTrue(PropertyLocation.objects.filter(pk=location.pk).exists())

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
        self.assertTrue(response.json()["has_location"])
        self.assertTrue(response.json()["territory_ready"])

    def test_manual_location_then_infer_territory_uses_saved_pin(self):
        listing, _ = ingest_listing(
            self.listing.source,
            {
                "external_id": "manual-pin-infer",
                "url": "https://example.com/manual-pin-infer",
                "title": "Casa con pin manual",
                "address": "Gral. Pedro Díaz 2400",
                "locality": "William C. Morris",
                "property_type": "house",
                "currency": "USD",
                "price": 120000,
            },
        )
        PropertyLocation.objects.filter(property=listing.property).delete()
        location_response = self.client.post(
            f"/api/propiedad/{listing.property_id}/ubicacion/",
            data=json.dumps({"latitude": -34.606, "longitude": -58.648}),
            content_type="application/json",
        )
        self.assertEqual(location_response.status_code, 200)
        self.assertTrue(location_response.json()["has_location"])

        territory_result = SimpleNamespace(
            partido="Partido de Hurlingham",
            locality="William C. Morris",
            zone="Los Patitos",
            confidence="medium_high",
            source_method="manual_pin_test",
            needs_review=False,
            evidence={"source": "manual_pin"},
        )
        score_result = SimpleNamespace(
            overall_score=68,
            level="media",
            zone_name="Los Patitos",
            match_method="polygon",
            confidence="medium_high",
            transport_score=60,
            education_score=70,
            health_score=66,
            flood_penalty_score=0,
            urban_informality_score=0,
            environmental_penalty_score=0,
            development_potential_score=62,
            in_flood_risk_zone=False,
            nearest_renabap_m=None,
            nearest_sube_point_m=200,
            nearest_school_m=300,
            nearest_health_center_m=500,
            components={},
            risks={},
            evidence={"source": "manual_pin"},
            source_signature="score-test",
        )

        with patch("properties.views.Geocoder") as geocoder_cls, patch(
            "properties.views.infer_property_territory", return_value=territory_result
        ) as infer_mock, patch(
            "properties.views.load_location_zones",
            return_value={"configured": True, "features": [], "signature": "score-test"},
        ), patch("properties.views.score_property_location_intelligence", return_value=score_result):
            response = self.client.post(f"/api/propiedad/{listing.property_id}/inferir-territorio/")

        self.assertEqual(response.status_code, 200)
        geocoder_cls.return_value.geocode_property.assert_not_called()
        infer_mock.assert_called_once()
        inferred_property = infer_mock.call_args.args[0]
        self.assertEqual(inferred_property.location.precision, PropertyLocation.Precision.MANUAL)
        self.assertTrue(response.json()["location"]["manually_corrected"])
        listing.property.refresh_from_db()
        self.assertEqual(listing.property.inferred_locality, "William C. Morris")
        self.assertEqual(listing.property.inferred_zone, "Los Patitos")

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
        listing.property.inferred_neighborhood = UNIFIED_HURLINGHAM_CENTRO_ZONE
        listing.property.save(update_fields=["inferred_neighborhood"])

        response = self.client.get("/", {"neighborhood": "Barrio Ingl\u00e9s"})

        self.assertContains(response, "Casa con zona inferida")

    def test_neighborhood_filter_ignores_noncanonical_dirty_values(self):
        clean_listing, _ = ingest_listing(
            self.listing.source,
            {
                "external_id": "inferred-clean-zone",
                "url": "https://example.com/inferred-clean-zone",
                "title": "Casa limpia por zona",
                "address": "Uspallata",
                "locality": "Hurlingham",
                "property_type": "house",
                "currency": "USD",
                "price": 100000,
            },
        )
        clean_listing.property.inferred_zone = "El Destino"
        clean_listing.property.neighborhood = ""
        clean_listing.property.save(update_fields=["inferred_zone", "neighborhood"])
        dirty = Property.objects.create(
            fingerprint="dirty-filter-zone",
            title="Propiedad con zona sucia",
            neighborhood="Casa en Venta en Hurlingham",
            property_type=Property.Type.HOUSE,
            status=Property.Status.ACTIVE,
        )

        response = self.client.get("/", {"neighborhood": "El Destino"})
        self.assertContains(response, "Casa limpia por zona")

        dirty_response = self.client.get("/", {"neighborhood": dirty.neighborhood})
        self.assertNotContains(dirty_response, "Propiedad con zona sucia")

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
        self.assertContains(response, "Ubicar por direccion")
        self.assertContains(response, '"has_location": false')
        payload = json.loads(BeautifulSoup(response.content, "lxml").find(id="property-location").string)
        self.assertTrue(payload["can_geocode_from_address"])
        self.assertEqual(payload["geocode_address_label"], "Uspallata")

        response = self.client.post(
            f"/api/propiedad/{listing.property_id}/ubicacion/",
            data=json.dumps({"latitude": -34.591, "longitude": -58.641}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        listing.property.refresh_from_db()
        self.assertEqual(listing.property.location.provider, "manual")
        self.assertTrue(listing.property.location.manually_corrected)

    def test_detail_hides_geocode_button_without_useful_address(self):
        property_obj = Property.objects.create(
            fingerprint="no-address-no-pin",
            title="Casa sin direccion ni pin",
            locality="Hurlingham",
            property_type=Property.Type.HOUSE,
            status=Property.Status.ACTIVE,
        )

        response = self.client.get(f"/propiedad/{property_obj.pk}/")

        self.assertContains(response, "Ubicar manualmente")
        self.assertNotContains(response, "Ubicar por direccion")
        payload = json.loads(BeautifulSoup(response.content, "lxml").find(id="property-location").string)
        self.assertFalse(payload["has_location"])
        self.assertFalse(payload["can_geocode_from_address"])
        self.assertEqual(payload["geocode_address_label"], "")

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
        self.assertContains(response, "Crimen reportado")
        self.assertContains(response, "quality_field=surface")
        chart_data = self._dashboard_payload(response)
        self.assertIn("url", chart_data["by_locality"][0])
        self.assertIn("price_buckets", chart_data)
        self.assertIn("crime", chart_data)
        self.assertIn("zone_insights", chart_data["crime"])

    def test_stats_surface_trend_uses_real_comparable_groups(self):
        source = self.listing.source
        for index, area in enumerate((100, 110, 120, 130, 140), start=1):
            ingest_listing(
                source,
                {
                    "external_id": f"needs-work-{index}",
                    "url": f"https://example.com/needs-work-{index}",
                    "title": f"Casa a refaccionar comparable {index}",
                    "address": f"Necochea {900 + index}",
                    "locality": "Hurlingham",
                    "property_type": Property.Type.HOUSE,
                    "condition_category": Property.ConditionCategory.NEEDS_WORK,
                    "age_years": 45,
                    "currency": "USD",
                    "price": area * 900,
                    "covered_area": area,
                },
            )
            ingest_listing(
                source,
                {
                    "external_id": f"new-house-{index}",
                    "url": f"https://example.com/new-house-{index}",
                    "title": f"Casa a estrenar comparable {index}",
                    "address": f"Roma {900 + index}",
                    "locality": "Hurlingham",
                    "property_type": Property.Type.HOUSE,
                    "condition_category": Property.ConditionCategory.NEW,
                    "age_years": 2,
                    "currency": "USD",
                    "price": area * 1800,
                    "covered_area": area,
                },
            )
            ingest_listing(
                source,
                {
                    "external_id": f"land-{index}",
                    "url": f"https://example.com/land-{index}",
                    "title": f"Lote comparable {index}",
                    "address": f"Paris {900 + index}",
                    "locality": "Hurlingham",
                    "property_type": Property.Type.LAND,
                    "condition_category": Property.ConditionCategory.UNKNOWN,
                    "age_years": None,
                    "currency": "USD",
                    "price": area * 350,
                    "land_area": area,
                },
            )

        response = self.client.get("/estadisticas/")
        chart_data = self._dashboard_payload(response)
        surface_price = chart_data["surface_price"]
        needs_work = [item for item in surface_price if item["title"].startswith("Casa a refaccionar")]
        new_houses = [item for item in surface_price if item["title"].startswith("Casa a estrenar")]
        land = [item for item in surface_price if item["title"].startswith("Lote comparable")]
        singleton = next(item for item in surface_price if item["id"] == self.listing.property_id)

        self.assertEqual({item["comparable_count"] for item in needs_work}, {5})
        self.assertEqual({item["comparable_count"] for item in new_houses}, {5})
        self.assertEqual({item["comparable_count"] for item in land}, {5})
        self.assertTrue(all("A refaccionar" in item["comparable_group"] for item in needs_work))
        self.assertTrue(all("A estrenar" in item["comparable_group"] for item in new_houses))
        self.assertTrue(all("Terreno" in item["comparable_group"] for item in land))
        self.assertTrue(all(item["discount"] is not None for item in needs_work + new_houses + land))
        self.assertEqual(singleton["comparable_count"], 1)
        self.assertIsNone(singleton["discount"])

    def test_crime_layers_api_returns_payload(self):
        with patch(
            "properties.views.crime_layers_payload",
            return_value={
                "configured": False,
                "summary": {},
                "zones": {"type": "FeatureCollection", "features": []},
                "homicide_points": {"type": "FeatureCollection", "features": []},
                "timeseries": {"configured": False, "monthly": []},
            },
        ):
            response = self.client.get("/api/crimen/capas/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["configured"])

        with patch(
            "properties.views.crime_layers_payload",
            return_value={
                "configured": True,
                "summary": {"metrics": {"crime_data_scope": "municipio"}},
                "zones": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "crime_data_scope": "municipio",
                                "crime_spatial_precision": "low",
                            },
                            "geometry": {"type": "Polygon", "coordinates": []},
                        }
                    ],
                },
                "homicide_points": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"is_exact_location": False},
                            "geometry": {"type": "Point", "coordinates": [-58.64, -34.60]},
                        }
                    ],
                },
                "timeseries": {"configured": True, "monthly": []},
            },
        ):
            response = self.client.get("/api/crimen/capas/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["zones"]["features"][0]["properties"]["crime_data_scope"], "municipio")
        self.assertFalse(payload["homicide_points"]["features"][0]["properties"]["is_exact_location"])

    def test_stats_chart_uses_canonical_localities_only(self):
        Property.objects.create(
            fingerprint="stats-dirty-locality",
            title="Casa con localidad sucia",
            operation="sale",
            status=Property.Status.ACTIVE,
            property_type=Property.Type.HOUSE,
            currency="USD",
            price=100000,
            locality="Parque Johnston",
        )
        response = self.client.get("/estadisticas/")
        chart_data = self._dashboard_payload(response)
        labels = {item["label"] for item in chart_data["by_locality"]}
        self.assertIn("Hurlingham", labels)
        self.assertNotIn("Parque Johnston", labels)

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
        self.assertFalse(job.mark_missing)
        source_progress = ScrapeJobSource.objects.get(job=job, slug="mapaprop")
        self.assertEqual(source_progress.workers, 2)

        response = self.client.get(f"/api/scraping/jobs/{job.pk}/")
        self.assertEqual(response.json()["sources"][0]["slug"], "mapaprop")

        response = self.client.post(f"/api/scraping/jobs/{job.pk}/cancel/")
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertTrue(job.cancel_requested)

    def test_operation_api_creates_job_and_catalog(self):
        catalog = self.client.get("/api/operations/catalog/")
        self.assertEqual(catalog.status_code, 200)
        self.assertIn("steps", catalog.json())

        with patch("properties.views.start_operation_job") as starter:
            response = self.client.post(
                "/api/operations/jobs/",
                data=json.dumps(
                    {
                        "kind": "geocode",
                        "mode": "apply",
                        "title": "Geocode test",
                        "steps": [
                            {
                                "kind": "geocode",
                                "mode": "apply",
                                "params": {"limit": 5, "cache_only": True},
                            }
                        ],
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201)
        starter.assert_called_once()
        job = OperationJob.objects.get()
        self.assertEqual(job.kind, OperationJob.Kind.GEOCODE)
        self.assertEqual(job.mode, OperationJob.Mode.APPLY)
        self.assertEqual(job.total_steps, 1)
        self.assertEqual(job.steps.get().params["limit"], 5)

    def test_operation_api_requires_dry_run_before_risky_apply(self):
        with patch("properties.views.start_operation_job") as starter:
            response = self.client.post(
                "/api/operations/jobs/",
                data=json.dumps(
                    {
                        "kind": "repair_addresses",
                        "mode": "apply",
                        "steps": [{"kind": "repair_addresses", "mode": "apply"}],
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("simulacion", response.json()["error"])
        starter.assert_not_called()

    def test_operation_apply_from_dry_run_creates_apply_job(self):
        dry_run = OperationJob.objects.create(
            kind=OperationJob.Kind.REPAIR_AGENCIES,
            mode=OperationJob.Mode.DRY_RUN,
            status=OperationJob.Status.SUCCESS,
            total_steps=1,
            completed_steps=1,
        )
        OperationJobStep.objects.create(
            job=dry_run,
            order=1,
            kind=OperationJob.Kind.REPAIR_AGENCIES,
            mode=OperationJob.Mode.DRY_RUN,
            status=OperationJob.Status.SUCCESS,
        )
        with patch("properties.views.start_operation_job") as starter:
            response = self.client.post(
                f"/api/operations/jobs/{dry_run.pk}/apply-from-dry-run/"
            )
        self.assertEqual(response.status_code, 201)
        starter.assert_called_once()
        apply_job = OperationJob.objects.exclude(pk=dry_run.pk).get()
        self.assertEqual(apply_job.mode, OperationJob.Mode.APPLY)
        self.assertEqual(apply_job.source_job, dry_run)
        self.assertEqual(apply_job.steps.get().mode, OperationJob.Mode.APPLY)

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

    def test_running_job_with_live_thread_is_not_marked_interrupted(self):
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
        JOB_THREADS[job.pk] = threading.current_thread()
        try:
            response = self.client.get(f"/api/scraping/jobs/{job.pk}/")
        finally:
            JOB_THREADS.pop(job.pk, None)
        payload = response.json()
        self.assertEqual(payload["status"], ScrapeJob.Status.RUNNING)
        self.assertEqual(payload["sources"][0]["status"], ScrapeJobSource.Status.RUNNING)

    def test_cancel_operation_marks_interrupted_child_scrape_cancel_requested(self):
        operation = OperationJob.objects.create(
            kind=OperationJob.Kind.SCRAPE,
            mode=OperationJob.Mode.APPLY,
            status=OperationJob.Status.RUNNING,
            total_steps=1,
        )
        scrape_job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.INTERRUPTED,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
        )
        OperationJobStep.objects.create(
            job=operation,
            order=1,
            kind=OperationJob.Kind.SCRAPE,
            mode=OperationJob.Mode.APPLY,
            status=OperationJob.Status.RUNNING,
            result_summary={"scrape_job_id": scrape_job.pk},
        )

        response = self.client.post(f"/api/operations/jobs/{operation.pk}/cancel/")

        self.assertEqual(response.status_code, 200)
        scrape_job.refresh_from_db()
        self.assertTrue(scrape_job.cancel_requested)

    def test_operation_status_reconciles_finished_child_scrape(self):
        now = timezone.now()
        operation = OperationJob.objects.create(
            kind=OperationJob.Kind.SCRAPE,
            mode=OperationJob.Mode.APPLY,
            status=OperationJob.Status.RUNNING,
            total_steps=1,
            cancel_requested=True,
            started_at=now,
        )
        scrape_job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.INTERRUPTED,
            selected_sources=["mapaprop"],
            worker_config={"mapaprop": 1},
            started_at=now,
            finished_at=now,
        )
        source = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        ScrapeJobSource.objects.create(
            job=scrape_job,
            source=source,
            slug="mapaprop",
            name="Mapaprop",
            status=ScrapeJobSource.Status.INTERRUPTED,
            processed=3,
            total_to_process=5,
            created=1,
            updated=1,
            errors=1,
            finished_at=now,
        )
        OperationJobStep.objects.create(
            job=operation,
            order=1,
            kind=OperationJob.Kind.SCRAPE,
            mode=OperationJob.Mode.APPLY,
            status=OperationJob.Status.RUNNING,
            total=1,
            result_summary={"scrape_job_id": scrape_job.pk},
        )

        response = self.client.get(f"/api/operations/jobs/{operation.pk}/")

        payload = response.json()
        self.assertEqual(payload["status"], OperationJob.Status.CANCELLED)
        self.assertEqual(payload["completed_steps"], 1)
        self.assertEqual(payload["processed"], 3)
        self.assertEqual(payload["changed"], 2)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(payload["steps"][0]["status"], OperationJob.Status.CANCELLED)
        self.assertEqual(payload["steps"][0]["result_summary"]["scrape_job_id"], scrape_job.pk)
        self.assertEqual(len(payload["steps"][0]["result_summary"]["sources"]), 1)

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

    def test_becerra_parser_extracts_visible_gba_address_and_map(self):
        cases = [
            (
                "Chalet en Venta en Hurlingham",
                "Diego de Carvajal al 800",
                "Diego de Carvajal 800",
                "Parque Quirno",
                "-34.6001003",
                "-58.6345574",
            ),
            (
                "Casa en venta de 5 ambientes en barrio cerrado El Pasaje - Hurlingham",
                "Nilda Figueira al 1400",
                "Nilda Figueira 1400",
                "Barrio El Pasaje",
                "-34.5869687",
                "-58.6358693",
            ),
        ]
        for title, source_address, expected_address, neighborhood, latitude, longitude in cases:
            with self.subTest(address=source_address):
                scraper = BecerraScraper()
                scraper.soup = lambda _url, title=title, address=source_address, neighborhood=neighborhood, latitude=latitude, longitude=longitude: BeautifulSoup(
                    f"""
                    <html><body>
                      <h1>{title}</h1>
                      <main>{title} {address} G.B.A. Zona Oeste | Hurlingham | {neighborhood} Venta USD 295.000</main>
                      <div data-latitude="{latitude}" data-longitude="{longitude}"></div>
                    </body></html>
                    """,
                    "lxml",
                )
                data = scraper.parse("https://becerrapropiedades.com/ficha/7177687")
                self.assertEqual(data["address"], expected_address)
                self.assertEqual(data["detected_address"], expected_address)
                self.assertAlmostEqual(data["latitude"], float(latitude))
                self.assertAlmostEqual(data["longitude"], float(longitude))

    def test_becerra_parser_prefers_visible_h3_and_ignores_header_noise(self):
        scraper = BecerraScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>PH 3 ambientes en Venta en Hurlingham</h1>
              <nav>Tel: 4662-2562 INICIO DESTACADOS EMPRENDIMIENTOS SERVICIOS QUIENES SOMOS CONTACTO</nav>
              <h3>Juan DÃ­az de SolÃ­s al 1500</h3>
              <main>G.B.A. Zona Oeste | Hurlingham | Venta USD 95.000</main>
              <script>window.map = {"lat":"-34.5888949406","lng":"-58.639576753"};</script>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://becerrapropiedades.com/ficha/6966002")
        self.assertIn("Juan", data["address"])
        self.assertIn("1500", data["address"])
        self.assertNotIn("Tel:", data["address"])
        self.assertAlmostEqual(data["latitude"], -34.5888949406)
        self.assertAlmostEqual(data["longitude"], -58.639576753)

    def test_becerra_parser_marks_terminal_error_as_removed(self):
        scraper = BecerraScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Whoops! We seem to have hit a snag.</h1>
              <p>La ficha ya no esta disponible.</p>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://becerrapropiedades.com/ficha/9999999")
        self.assertEqual(data["status"], Property.Status.REMOVED)
        self.assertEqual(data["source_status"], "removed")
        self.assertTrue(data["raw_data"]["becerra_retired_text"])

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

    def test_scraper_helpers_repair_mojibake_metrics(self):
        offers = parse_multi_unit_offers(
            "Unidad A \u00e2\u20ac\u201c 2 Ambientes "
            "Superficie total: 42 m\u00c2\u00b2 Precio: USD 100.000"
        )
        self.assertEqual(offers[0]["unit"], "A")
        self.assertEqual(offers[0]["total_area"], "42")

        area, front, depth = parse_dimension_value("8 \u00c3\u0097 10 m\u00c2\u00b2")
        self.assertEqual(area, Decimal("80"))
        self.assertEqual(front, Decimal("8"))
        self.assertEqual(depth, Decimal("10"))

        scraper = ArgenpropScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa en Hurlingham</h1>
              <p>USD 120.000 2 Ba\u00c3\u00b1os Contact\u00c3\u00a1 al anunciante Demo Propiedades Ver tel</p>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.argenprop.com/casa-en-venta-en-hurlingham--100")
        self.assertEqual(data["bathrooms"], Decimal("2"))
        self.assertEqual(data["agency"], "Demo Propiedades")

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

    def test_fincas_parser_prioritizes_structured_haurie_address(self):
        data = self.parse_with_fixture(
            FincasScraper,
            "haurie_address_detail.html",
            "https://www.haurie.argencasas.com/propiedad-local-con-vivienda-venta-hurlingham-301-1082",
        )
        self.assertTrue(data["address"].startswith("Eva Per"))
        self.assertIn("2600", data["address"])
        self.assertNotEqual(data["address"].lower(), "salon de 17")

    def test_fincas_parser_rejects_description_as_address(self):
        data = self.parse_with_fixture(
            FincasScraper,
            "haurie_false_address_detail.html",
            "https://www.haurie.argencasas.com/propiedad-casa-venta-hurlingham-301-1083",
        )
        self.assertFalse(data.get("address"))

    def test_fincas_parser_handles_argencasas_xx_address_and_map(self):
        scraper = FincasScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa para 2 Familias en Venta en Hurlingham</h1>
              <main>
                Todas Argentina GBA Oeste Hurlingham Hurlingham Hurlingham
                RIO COLORADO (XX) al 2100 u$s 230.000 Casa para 2 Familias venta Hurlingham
              </main>
              <script>{"latitude":"-34.588521830213","longitude":"-58.64304371568"}</script>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.haurie.argencasas.com/propiedad-casa-para-2-familias-venta-hurlingham-301-1073")
        self.assertEqual(data["address"], "Río Colorado 2100")
        self.assertEqual(data["detected_address"], "Río Colorado 2100")
        self.assertAlmostEqual(data["latitude"], -34.588521830213)
        self.assertAlmostEqual(data["longitude"], -58.64304371568)
        self.assertEqual(data["location_precision"], "exact")

    def test_fincas_parser_strips_territory_breadcrumb_from_direct_address(self):
        scraper = FincasScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Chalet en Venta en Hurlingham</h1>
              <main>
                Argentina GBA Oeste Hurlingham Hurlingham Hurlingham
                Miranda 1500 u$s 120.000 Chalet venta Hurlingham
              </main>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.haurie.argencasas.com/propiedad-chalet-venta-hurlingham-301-1043")
        self.assertEqual(data["address"], "Gral. Francisco Miranda 1500")
        self.assertEqual(data["detected_address"], "Gral. Francisco Miranda 1500")

    def test_fincas_parser_rejects_declared_out_of_target_without_target_map(self):
        scraper = FincasScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Departamento en Venta en Castelar Norte</h1>
              <main>Todas Argentina GBA Oeste Moron Castelar Castelar Norte Arredondo 2374 al 2300</main>
            </body></html>
            """,
            "lxml",
        )
        self.assertIsNone(scraper.parse("https://www.haurie.argencasas.com/propiedad-departamento-venta-castelar-norte-301-997"))

    def test_oscar_dahbar_parser_extracts_map_coordinates(self):
        scraper = OscarDahbarScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>LIBRERIA EL SOL EN VENTA FRENTE A COLEGIO LINCOLN</h1>
              <main>Venta Hurlingham Código: 175838 Dormitorios 1 M2 Totales 30</main>
              <script>window.map = {"lat": "-34.58066609762967", "lng": "-58.64246672233726"};</script>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://oscardahbarpropiedades.com.ar/ad/libreria-el-sol-en-venta-frente-a-colegio-lincoln")
        self.assertAlmostEqual(data["latitude"], -34.58066609762967)
        self.assertAlmostEqual(data["longitude"], -58.64246672233726)
        self.assertEqual(data["location_precision"], "exact")

    def test_oscar_dahbar_parser_rejects_declared_out_of_target_listing(self):
        scraper = OscarDahbarScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Monoambiente en Mar de Ajo</h1>
              <main>Venta Hurlingham Código: 175999 Hipolito Yrigoyen 431, Mar De Ajo, Buenos Aires, Argentina</main>
            </body></html>
            """,
            "lxml",
        )
        self.assertIsNone(scraper.parse("https://oscardahbarpropiedades.com.ar/ad/monoambiente-en-mar-de-ajo"))

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

    def test_base_scraper_can_bypass_robots_when_source_allows_it(self):
        class FakeResponse:
            ok = True
            text = "<html><body>ok</body></html>"

            def raise_for_status(self):
                return None

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, timeout=None):
                self.calls.append(url)
                if url.endswith("/robots.txt"):
                    raise AssertionError("robots.txt should not be fetched")
                return FakeResponse()

        class FakeScraper(BaseScraper):
            definition = SourceDefinition(
                slug="fake-no-robots",
                name="Fake No Robots",
                base_url="https://example.com",
                search_url="https://example.com/private",
                crawl_delay=0,
                respect_robots=False,
            )

            def discover(self):
                return []

            def parse(self, url):
                return None

        session = FakeSession()
        scraper = FakeScraper(session=session)

        response = scraper.get("https://example.com/private")

        self.assertEqual(response.text, "<html><body>ok</body></html>")
        self.assertEqual(session.calls, ["https://example.com/private"])

    def test_valenti_bypasses_robots_for_motor_pagination(self):
        class FakeResponse:
            ok = True
            text = '<html><body><a href="/propiedad-casa-venta-hurlingham-143-16">Casa</a></body></html>'

            def raise_for_status(self):
                return None

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, timeout=None):
                self.calls.append(url)
                if url.endswith("/robots.txt"):
                    raise AssertionError("Valenti should bypass robots.txt")
                return FakeResponse()

        scraper = ValentiScraper(session=FakeSession())
        page_url = scraper._page_url(2)

        self.assertFalse(scraper.definition.respect_robots)
        self.assertIn("/motor/props.php", page_url)
        soup = scraper.soup(page_url)
        self.assertEqual(soup.get_text(" ", strip=True), "Casa")
        self.assertEqual(scraper.session.calls, [page_url])

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

    def test_mapaprop_status_badges_hide_suspicious_prices(self):
        cases = [
            ("mapaprop_reserved_usd1.html", Property.Status.RESERVED, "reserved", "placeholder_price_1"),
            ("mapaprop_sold_usd1.html", Property.Status.SOLD, "sold", "placeholder_price_1"),
            ("mapaprop_suspended_usd1.html", Property.Status.SUSPENDED, "suspended", "placeholder_price_1"),
            ("mapaprop_reserved_ars_old.html", Property.Status.RESERVED, "reserved", "non_active_ars_price"),
        ]
        for fixture_name, status, source_status, reason in cases:
            with self.subTest(fixture=fixture_name):
                data = self.parse_with_fixture(
                    MapapropScraper,
                    fixture_name,
                    f"https://www.mapaprop.com/en/property/{fixture_name}",
                )
                self.assertEqual(data["status"], status)
                self.assertEqual(data["source_status"], source_status)
                self.assertIsNone(data["price"])
                self.assertEqual(data["currency"], "")
                self.assertEqual(data["raw_data"]["mapaprop_status_badge"], source_status)
                self.assertEqual(data["raw_data"]["mapaprop_price_hidden_reason"], reason)
                self.assertIn("mapaprop_public_price", data["raw_data"])

    def test_mapaprop_missing_and_valid_prices(self):
        missing = self.parse_with_fixture(
            MapapropScraper,
            "mapaprop_reserved_no_price.html",
            "https://www.mapaprop.com/en/property/reserved-no-price",
        )
        self.assertEqual(missing["status"], Property.Status.RESERVED)
        self.assertEqual(missing["source_status"], "reserved")
        self.assertIsNone(missing["price"])
        self.assertEqual(missing["currency"], "")

        valid = self.parse_with_fixture(
            MapapropScraper,
            "mapaprop_active_valid_price.html",
            "https://www.mapaprop.com/en/property/active-valid-price",
        )
        self.assertEqual(valid["status"], Property.Status.ACTIVE)
        self.assertEqual(valid["source_status"], "")
        self.assertEqual(valid["price"], Decimal("98000"))
        self.assertEqual(valid["currency"], "USD")
        self.assertNotIn("mapaprop_price_hidden_reason", valid.get("raw_data", {}))

        ars_placeholder = self.parse_with_fixture(
            MapapropScraper,
            "mapaprop_active_ars_placeholder.html",
            "https://www.mapaprop.com/en/property/active-ars-placeholder",
        )
        self.assertEqual(ars_placeholder["status"], Property.Status.ACTIVE)
        self.assertIsNone(ars_placeholder["price"])
        self.assertEqual(ars_placeholder["currency"], "")
        self.assertEqual(
            ars_placeholder["raw_data"]["mapaprop_price_hidden_reason"],
            "ars_placeholder_price",
        )

    def test_riquelme_status_badges_and_clean_address(self):
        cases = [
            ("<button class='btn-danger'>Suspendido / No disponible</button>", Property.Status.SUSPENDED, "suspended"),
            ("<div class='search-item-status search-item-reserved'></div>", Property.Status.RESERVED, "reserved"),
            ("<button class='btn-danger'>Vendido</button>", Property.Status.SOLD, "sold"),
            ("<div class='search-item-status search-item-sold'></div>", Property.Status.SOLD, "sold"),
        ]
        for badge_html, expected_status, expected_source_status in cases:
            with self.subTest(expected_source_status=expected_source_status):
                scraper = RiquelmeScraper()
                scraper.soup = lambda _url, badge_html=badge_html: BeautifulSoup(
                    f"""
                    <html><body>
                      <h1>Casa en Hurlingham</h1>
                      <main class="property-description">
                        {badge_html}
                        <p>Venta de casa en Hurlingham</p>
                      </main>
                      <footer>011 4452-1000 Av. Vergara N° 3090</footer>
                    </body></html>
                    """,
                    "lxml",
                )
                data = scraper.parse(
                    "https://www.riquelmepropiedades.com.ar/propiedad/venta-de-casa-en-hurlingham-hurlingham-buenos-aires-708-134229"
                )
                self.assertEqual(data["status"], expected_status)
                self.assertEqual(data["source_status"], expected_source_status)
                self.assertEqual(data["raw_data"]["riquelme_status_badge"], expected_source_status)
                self.assertEqual(data["address"], "")
                self.assertNotIn("Vergara", data.get("detected_address") or "")

        scraper = RiquelmeScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa en Hurlingham</h1>
              <main class="property-description">
                <p>Venta de casa en Hurlingham</p>
              </main>
              <footer>Todos los derechos Reservados Av. Vergara N° 3090</footer>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse(
            "https://www.riquelmepropiedades.com.ar/propiedad/venta-de-casa-en-hurlingham-buenos-aires-708-34472"
        )
        self.assertEqual(data["source_status"], "")
        self.assertNotIn("riquelme_status_badge", data["raw_data"])

    def test_century21_detail_json_parses_metrics_and_coordinates(self):
        scraper = Century21Scraper()

        class FakeResponse:
            def json(self):
                return {
                    "entity": {
                        "id": 108737,
                        "titulo": "Departamento en venta",
                        "tipoOperacion": "venta",
                        "moneda": "USD",
                        "precioVenta": "93000",
                        "ambientes": 2,
                        "recamaras": 1,
                        "banios": 1,
                        "m2T": "62.0",
                        "m2C": "47.0",
                        "antiguedad": 2026,
                        "lat": "-34.590000000000000",
                        "lon": "-58.627000000000000",
                    },
                    "amenitiesTxt": ["Pileta", "Gimnasio"],
                    "fotos": [{"url": "https://example.com/foto.jpg"}],
                }

        scraper.get = lambda url: FakeResponse()
        data = scraper.parse("https://century21.com.ar/propiedad/108737_departamento-en-venta")
        self.assertEqual(data["external_id"], "108737_departamento-en-venta")
        self.assertEqual(data["property_type"], Property.Type.APARTMENT)
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("93000"))
        self.assertEqual(data["total_area"], Decimal("62.0"))
        self.assertEqual(data["covered_area"], Decimal("47.0"))
        self.assertEqual(data["rooms"], 2)
        self.assertEqual(data["bedrooms"], 1)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["age_years"], 0)
        self.assertEqual(data["latitude"], -34.59)
        self.assertEqual(data["longitude"], -58.627)
        self.assertIn("Gimnasio", data["features"])

    def test_zonaprop_detail_extracts_price_address_type_and_currency_rule(self):
        cases = [
            (
                "Casa PH con Lote",
                "$ 70.000",
                "ARGERICH , Hurlingham, Hurlingham",
                "https://www.zonaprop.com.ar/propiedades/clasificado/veclphin-casa-ph-con-lote-56491769.html",
                "PH · 40m² · 2 ambientes · 1 cochera 120 m² tot. 40 m² cub. 2 amb. 1 baño 1 coch. 1 dorm. 20 años",
                Property.Type.PH,
                "USD",
                Decimal("70000"),
                Decimal("120"),
                Decimal("40"),
                "German Argerich",
            ),
            (
                "Venta fondo de comercio vivero",
                "$ 26.000.000",
                "Teniente General Julio A. Roca 579, Hurlingham, Hurlingham",
                "https://www.zonaprop.com.ar/propiedades/clasificado/veclfcin-venta-fondo-de-comercio-vivero-en-hurlingham-sobre-av-59216053.html",
                "Fondo de Comercio · 1m² 1 m² tot. 1 m² cub. 10 años",
                Property.Type.OTHER,
                "ARS",
                Decimal("26000000"),
                Decimal("1"),
                Decimal("1"),
                "Tte. Gral. Julio Argentino Roca 579",
            ),
            (
                "Casa en Hurlingham",
                "USD 150.000",
                "GUEMES 1668, Hurlingham, Hurlingham",
                "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-59000000.html",
                "Casa · 100m² 100 m² tot. 80 m² cub. 4 amb. 2 baños 3 dorm. 20 años",
                Property.Type.HOUSE,
                "USD",
                Decimal("150000"),
                Decimal("100"),
                Decimal("80"),
                "Gral. Martín Güemes 1668",
            ),
            (
                "Casa Conscripto Bernardi",
                "USD 120.000",
                "Hurlingham-conscripto Bernardi 1900, Hurlingham, Hurlingham",
                "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-59000001.html",
                "Casa · 90m² 90 m² tot. 70 m² cub. 3 amb. 1 baño 2 dorm. 20 años",
                Property.Type.HOUSE,
                "USD",
                Decimal("120000"),
                Decimal("90"),
                Decimal("70"),
                "Conscripto Bernardi 1900",
            ),
        ]
        for (
            title,
            price,
            address,
            url,
            highlights,
            property_type,
            currency,
            amount,
            total_area,
            covered_area,
            expected_address,
        ) in cases:
            with self.subTest(title=title):
                scraper = ZonapropScraper()
                scraper.soup = lambda _url, title=title, price=price, address=address: BeautifulSoup(
                    f"""
                    <html><body>
                      <h1>{title}</h1>
                      <div class="price-value">venta <span>{price}</span></div>
                      <div class="article-map-container"><h4>{address}</h4></div>
                      <section>{highlights}</section>
                    </body></html>
                    """,
                    "lxml",
                )
                data = scraper.parse(url)
                self.assertEqual(data["property_type"], property_type)
                self.assertEqual(data["currency"], currency)
                self.assertEqual(data["price"], amount)
                self.assertTrue(data["address"].startswith(expected_address))
                self.assertEqual(data["total_area"], total_area)
                self.assertEqual(data["covered_area"], covered_area)
                self.assertEqual(data["raw_data"]["zonaprop_currency_inference"]["inferred_currency"], currency)

    def test_zonaprop_detail_extracts_embedded_map_coordinates(self):
        scraper = ZonapropScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa en venta</h1>
              <div class="price-value">venta USD 120.000</div>
              <div class="article-map-container"><h4>Coraceros 3115, Hurlingham, Hurlingham</h4></div>
              <div data-latitude="-34,59123" data-longitude="-58,64123"></div>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse(
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-59999999.html"
        )
        self.assertAlmostEqual(data["latitude"], -34.59123)
        self.assertAlmostEqual(data["longitude"], -58.64123)
        self.assertEqual(data["location_precision"], "exact")
        self.assertEqual(data["raw_data"]["zonaprop_map_coordinate"]["method"], "data-latitude")

        base64_scraper = ZonapropScraper()
        base64_scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Departamento en venta</h1>
              <div class="price-value">venta USD 90.000</div>
              <script>
                const mapLatOf = "LTM0LjU4Mjk5OTk5OTk5OTk5OA==";
                const mapLngOf = "LTU4LjYzNzAwMDAwMDAwMDAwMA==";
              </script>
            </body></html>
            """,
            "lxml",
        )
        encoded = base64_scraper.parse(
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclapin-departamento-en-venta-en-hurlingham-57401637.html"
        )
        self.assertAlmostEqual(encoded["latitude"], -34.583)
        self.assertAlmostEqual(encoded["longitude"], -58.637)
        self.assertEqual(encoded["raw_data"]["zonaprop_map_coordinate"]["method"], "zonaprop_base64_map")

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

    def test_faella_discovery_uses_public_card_pagination(self):
        scraper = FaellaScraper(max_pages=2)
        calls = []

        def fake_soup(url):
            calls.append(url)
            if "page=2" in url:
                return BeautifulSoup(
                    """
                    <html><body>
                      <span class="results-count"><strong>36</strong> propiedades encontradas</span>
                      <div class="card">
                        <a class="card-link" href="https://casa.mercadolibre.com.ar/MLA-222-venta-casa-hurlingham-_JM">
                          <div class="card-price">U$S 100.000</div>
                          <h3 class="card-title">Venta Casa Hurlingham</h3>
                          <span class="feature">100 m2</span>
                          <div class="card-location">Hurlingham, Bs.As. G.B.A. Oeste</div>
                        </a>
                      </div>
                    </body></html>
                    """,
                    "lxml",
                )
            return fixture_soup("faella_listing.html")

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(
            urls,
            [
                "https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham#MLA1831365219",
                "https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham#MLA3243981582",
                "https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham&page=2#MLA222",
            ],
        )
        self.assertEqual(scraper.discovery_stats["declared_total"], 36)
        self.assertEqual(scraper.discovery_stats["pages_seen"], 2)
        self.assertTrue(scraper.discovery_stats["limited_by_max_pages"])
        self.assertTrue(any("page=2" in url for url in calls))

    def test_faella_discovery_respects_max_listings(self):
        scraper = FaellaScraper(max_listings=1)
        scraper.soup = lambda _url: fixture_soup("faella_listing.html")
        urls = list(scraper.discover())
        self.assertEqual(
            urls,
            ["https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham#MLA1831365219"],
        )
        self.assertEqual(scraper.discovery_stats["declared_total"], 36)
        self.assertTrue(scraper.discovery_stats["limited_by_max_listings"])

    def test_faella_parser_reads_listing_card(self):
        data = self.parse_with_fixture(
            FaellaScraper,
            "faella_listing.html",
            "https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham#MLA1831365219",
        )
        self.assertEqual(data["external_id"], "MLA1831365219")
        self.assertEqual(
            data["url"],
            "https://casa.mercadolibre.com.ar/MLA-1831365219-venta-casa-hurlingham-parque-johnston-lote-jardin-pileta-_JM",
        )
        self.assertEqual(data["agency"], "Faella Propiedades")
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("220000"))
        self.assertEqual(data["title"], "Venta Casa Hurlingham Parque Johnston Lote Jardin Pileta")
        self.assertEqual(data["property_type"], Property.Type.HOUSE)
        self.assertEqual(data["total_area"], Decimal("295"))
        self.assertEqual(data["bedrooms"], 3)
        self.assertEqual(data["bathrooms"], Decimal("2"))
        self.assertEqual(data["locality"], "Hurlingham")
        self.assertEqual(data["neighborhood"], "Parque Johnston")
        self.assertEqual(
            data["images"],
            ["https://http2.mlstatic.com/D_831095-MLA111917360290_062026-O.jpg"],
        )
        self.assertEqual(data["raw_data"]["faella_page"], FaellaScraper.definition.search_url)
        self.assertIn("295 m2", data["raw_data"]["feature_texts"])

        tesei = self.parse_with_fixture(
            FaellaScraper,
            "faella_listing.html",
            "https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham#MLA3243981582",
        )
        self.assertEqual(tesei["locality"], "Villa Tesei")

    def test_faella_parser_keeps_public_card_address(self):
        scraper = FaellaScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <div class="card">
                <a class="card-link" href="https://casa.mercadolibre.com.ar/MLA-111-venta-casa-_JM">
                  <div class="card-price">U$S 100.000</div>
                  <h3 class="card-title">Venta Casa Hurlingham</h3>
                  <span class="feature">100 m2</span>
                  <div class="card-location">Acassuso 1925, Hurlingham, Hurlingham</div>
                </a>
              </div>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://faellainmuebles.com.ar/?operation=venta&city=Hurlingham#MLA111")
        self.assertEqual(data["address"], "Acassuso 1925")
        self.assertEqual(data["detected_address"], "Acassuso 1925")
        self.assertEqual(data["location_precision"], "exact")

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
        self.assertEqual(miglierini["latitude"], -34.596891378643)
        self.assertEqual(miglierini["longitude"], -58.640695687162)
        self.assertEqual(miglierini["location_precision"], "exact")
        self.assertEqual(miglierini["raw_data"]["miglierini_map_coordinate"]["method"], "propertyMapData")
        self.assertEqual(odriozola["address"], "Carhue 911, Villa Tesei, Partido de Hurlingham, Buenos Aires, Argentina")
        self.assertEqual(odriozola["locality"], "Villa Tesei")
        self.assertEqual(odriozola["latitude"], -34.583046168811)
        self.assertEqual(odriozola["raw_data"]["odriozola_map_coordinate"]["method"], "data-map")

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

    def test_guarnieri_parser_extracts_data_map_coordinates(self):
        scraper = GuarnieriScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <div class="elementor-location-single property">
                <h1>DÃºplex 4 AMB a estrenar</h1>
                <address class="item-address">Forest CASI SCHUMAN, Hurlingham,PCIA.BS.AS</address>
                <div class="property-description-wrap"><div class="block-content-wrap">Parque Johnston</div></div>
                <div data-map='{"latitude":"-34.590452874017","longitude":"-58.644783496857","address":"Forest CASI SCHUMAN, Hurlingham,PCIA.BS.AS"}'></div>
              </div>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/duplex-4-amb")
        self.assertAlmostEqual(data["latitude"], -34.590452874017)
        self.assertAlmostEqual(data["longitude"], -58.644783496857)
        self.assertEqual(data["location_precision"], "exact")
        self.assertEqual(data["raw_data"]["guarnieri_map_coordinate"]["method"], "data-map")

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
        self.assertEqual(data["address"], "Diego de Carvajal 500")
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

    def test_guarnieri_structured_table_wins_and_marks_conflict(self):
        data = self.parse_with_fixture(
            GuarnieriScraper,
            "guarnieri_structured_conflict_detail.html",
            "https://guarnieripropiedades.com.ar/inmobiliaria/propiedad/casa-4-amb-con-lote-hurlingham-centro-z-curupayti-apta-credito",
        )
        self.assertEqual(data["price"], Decimal("110000"))
        self.assertEqual(data["covered_area"], Decimal("325"))
        self.assertEqual(data["land_area"], Decimal("178"))
        self.assertEqual(data["total_area"], Decimal("178"))
        self.assertEqual(data["source_status"], "metric_conflict_review")
        fields = {
            conflict["field"]
            for conflict in data["raw_data"]["guarnieri_metric_conflicts"]
        }
        self.assertIn("price", fields)
        self.assertIn("covered_area", fields)
        self.assertIn("land_area", fields)

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
        self.assertEqual(data["neighborhood"], UNIFIED_HURLINGHAM_CENTRO_ZONE)
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
        scraper = ZonapropScraper(max_pages=1)
        scraper.soup = lambda parsed_url: fixture_soup("zonaprop_listing.html")
        self.assertEqual(
            list(scraper.discover()),
            [
                "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-57923940.html",
                "https://www.zonaprop.com.ar/propiedades/clasificado/vecltrin-hurlingham-terreno-venta-56884803.html",
            ],
        )

    def test_zonaprop_price_urls_use_allowed_sorted_public_paths(self):
        scraper = ZonapropScraper()
        self.assertEqual(
            scraper._price_url(None, None),
            "https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham-orden-precio-ascendente.html",
        )
        self.assertEqual(
            scraper._price_url(None, 50000, page=2),
            "https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham-menos-50000-dolar-orden-precio-ascendente-pagina-2.html",
        )
        self.assertEqual(
            scraper._price_url(87500, 100000, page=3),
            "https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham-87500-100000-dolar-orden-precio-ascendente-pagina-3.html",
        )
        self.assertEqual(
            scraper._price_url(600000, None),
            "https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham-mas-600000-dolar-orden-precio-ascendente.html",
        )

    def test_zonaprop_segmented_discovery_splits_price_ranges_dynamically(self):
        scraper = ZonapropScraper()
        scraper.max_public_pages = 2
        scraper.price_split_step = 10
        scraper.initial_price_probe = 100
        scraper.segment_capacity_ratio = 1.0
        requested_urls = []
        totals = {
            (None, None): 10,
            (100, None): 3,
            (None, 100): 7,
            (None, 50): 3,
            (50, 100): 4,
        }
        listings = {
            (None, 50): [1, 2, 3],
            (50, 100): [4, 5, 6, 7],
            (100, None): [8, 9, 10],
        }

        def segment_key(url):
            if "menos-" in url:
                return (None, int(re.search(r"menos-(\d+)-dolar", url).group(1)))
            if "mas-" in url:
                return (int(re.search(r"mas-(\d+)-dolar", url).group(1)), None)
            match = re.search(r"hurlingham-hurlingham-(\d+)-(\d+)-dolar", url)
            if match:
                return (int(match.group(1)), int(match.group(2)))
            return (None, None)

        def fake_soup(url):
            requested_urls.append(url)
            is_base_seed = "orden-precio-ascendente" not in url
            key = segment_key(url)
            page_match = re.search(r"pagina-(\d+)", url)
            page = int(page_match.group(1)) if page_match else 1
            total = totals[key]
            ids = [1, 4, 8, 9] if is_base_seed else listings.get(key) or list(range(100, 100 + total))
            page_ids = ids[(page - 1) * 2 : page * 2]
            links = "".join(
                f'<a href="/propiedades/clasificado/veclcain-casa-en-venta-{item}.html">Casa {item}</a>'
                for item in page_ids
            )
            return BeautifulSoup(
                f"<html><body><h1>{total} Propiedades e inmuebles en venta</h1>{links}</body></html>",
                "lxml",
            )

        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(len(urls), 10)
        self.assertEqual(scraper.discovery_stats["segments_seen"], 3)
        self.assertTrue(scraper.discovery_stats["coverage_complete"])
        self.assertEqual(scraper.discovery_stats["coverage_ratio"], 100.0)
        self.assertTrue(any("menos-50-dolar" in url for url in requested_urls))
        self.assertTrue(any("50-100-dolar" in url for url in requested_urls))
        self.assertTrue(any("mas-100-dolar" in url for url in requested_urls))
        self.assertFalse(any("pagina-3" in url for url in requested_urls))

    def test_zonaprop_segmented_discovery_allows_near_complete_global_coverage(self):
        scraper = ZonapropScraper()
        scraper.max_public_pages = 1

        def urls(start, stop):
            return [
                f"https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-{item}.html"
                for item in range(start, stop)
            ]

        scraper.soup = lambda _url: BeautifulSoup(
            "<html><body><h1>100 Propiedades e inmuebles en venta</h1></body></html>",
            "lxml",
        )
        scraper._build_price_segments = lambda: (
            {"declared_total": 100, "first_page_urls": []},
            [
                {"min_price": None, "max_price": 100000, "declared_total": 50, "first_page_urls": urls(1, 50)},
                {"min_price": 100000, "max_price": None, "declared_total": 50, "first_page_urls": urls(50, 100)},
            ],
            [],
            150,
            120,
        )

        discovered = list(scraper.discover())

        self.assertEqual(len(discovered), 99)
        self.assertEqual(scraper.discovery_stats["coverage_ratio"], 99.0)
        self.assertTrue(scraper.discovery_stats["segments"][0]["incomplete"])
        self.assertTrue(scraper.discovery_stats["coverage_complete"])

    def test_zonaprop_discovery_caps_public_pagination(self):
        requested_urls = []

        def fake_soup(url):
            requested_urls.append(url)
            page = 1
            match = re.search(r"pagina-(\d+)", url)
            if match:
                page = int(match.group(1))
            return BeautifulSoup(
                f"""
                <html><body>
                  <article>
                    <a href="/propiedades/clasificado/veclcain-casa-en-venta-pagina-{page}-5792394{page}.html?n_src=Listado&n_pg={page}">
                      Casa en venta pagina {page}
                    </a>
                  </article>
                </body></html>
                """,
                "lxml",
            )

        scraper = ZonapropScraper(max_pages=10)
        scraper.soup = fake_soup
        urls = list(scraper.discover())
        self.assertEqual(len(urls), 5)
        self.assertEqual(requested_urls[-1], scraper._page_url(5))
        self.assertNotIn(scraper._page_url(6), requested_urls)

    def test_zonaprop_cloudflare_challenge_is_controlled_block_error(self):
        scraper = ZonapropScraper()
        with self.assertRaisesMessage(RuntimeError, "request blocked by Cloudflare challenge"):
            list(scraper._listing_urls(fixture_soup("zonaprop_cloudflare_challenge.html")))

    def test_zonaprop_detail_allows_benign_cloudflare_jsd_script(self):
        class FakeResponse:
            text = (FIXTURES / "zonaprop_detail_with_jsd.html").read_text(encoding="utf-8")

        scraper = ZonapropScraper()
        scraper.get = lambda url: FakeResponse()
        data = scraper.parse(
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-57923940.html"
        )

        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["price"], Decimal("122000"))
        self.assertEqual(data["operation"], "sale")
        self.assertEqual(data["url"], "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-hurlingham-57923940.html")

    def test_zonaprop_detail_extracts_visible_address_and_highlights(self):
        scraper = ZonapropScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa PH 3 Ambientes</h1>
              <div>WILLIAMS 2328, Hurlingham, Hurlingham</div>
              <div>USD 110.000</div>
              <section>217 m² tot. 100 m² cub. 3 amb. 1 baño 2 dorm. 30 años</section>
            </body></html>
            """,
            "lxml",
        )

        data = scraper.parse(
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclphin-casa-ph-3-ambientes-55363642.html"
        )

        self.assertEqual(data["address"], "WILLIAMS 2328")
        self.assertEqual(data["total_area"], Decimal("217"))
        self.assertEqual(data["covered_area"], Decimal("100"))
        self.assertEqual(data["rooms"], 3)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["bedrooms"], 2)
        self.assertEqual(data["age_years"], 30)
        self.assertIn("zonaprop_highlights", data["raw_data"])

    def test_zonaprop_detail_extracts_street_only_address_with_postal_context(self):
        scraper = ZonapropScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa 3 ambientes en Hurlingham</h1>
              <div>
                Atuel, B1686 Hurlingham, Provincia de Buenos Aires, Argentina,
                Hurlingham, Buenos Aires, Argentina., Hurlingham, Hurlingham
              </div>
              <section>192 m² tot. 80 m² cub. 3 amb. 1 baño 1 coch. 2 dorm. 55 años</section>
            </body></html>
            """,
            "lxml",
        )

        data = scraper.parse(
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-3-ambientes-en-hurlingham-acepta-permuta-y-59107781.html"
        )

        self.assertEqual(data["address"], "Atuel")
        self.assertEqual(data["location_precision"], "street")
        self.assertEqual(data["garages"], 1)
        self.assertEqual(data["age_years"], 55)

    def test_zonaprop_detail_marks_a_estrenar_as_new_condition(self):
        scraper = ZonapropScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Casa tipo PH de 3 ambientes Hurlingham</h1>
              <div>Combate de Pavón 2330, Hurlingham, Hurlingham</div>
              <section>75 m² tot. 70 m² cub. 3 amb. 1 baño 2 dorm. A estrenar</section>
            </body></html>
            """,
            "lxml",
        )

        data = scraper.parse(
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclphin-casa-tipo-ph-de-3-ambiientes-hurlingham.-58163192.html"
        )

        self.assertEqual(data["address"], "Combate de Pavón 2330")
        self.assertEqual(data["age_years"], 0)
        self.assertEqual(data["condition_category"], Property.ConditionCategory.NEW)

    def test_zonaprop_parse_rejects_non_sale_links(self):
        scraper = ZonapropScraper()
        self.assertIsNone(
            scraper.parse(
                "https://www.zonaprop.com.ar/propiedades/clasificado/alcldein-deposito-en-alquiler-en-hurlingham-57923949.html"
            )
        )
        self.assertIsNone(
            scraper.parse("https://www.zonaprop.com.ar/inmuebles-venta-hurlingham-hurlingham.html")
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
        for scraper_cls in (LopezCombaScraper, AnaliaFernandezScraper, AliagaScraper, NerinaAlloScraper):
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

    def test_pixel_ad_parser_extracts_detail_fields(self):
        scraper = HollmannArielScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>PH al frente reciclado</h1>
              <p>En Venta PH al frente reciclado Codigo: 127415 Tte. Origone 287,
              Hurlingham, Buenos Aires, Argentina. USD80.000 PH Dormitorios 2
              Ambientes 3 Banos 1 M2 Totales 130 M2 Cubiertos 80</p>
              <img src="/foto.jpg">
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.hollmannarielpropiedades.com.ar/ad/ph-al-frente")
        self.assertEqual(data["external_id"], "127415")
        self.assertEqual(data["price"], Decimal("80000"))
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["address"], "Tte. Manuel Origone 287")
        self.assertEqual(data["locality"], "Hurlingham")
        self.assertEqual(data["rooms"], 3)
        self.assertEqual(data["bedrooms"], 2)
        self.assertEqual(data["bathrooms"], Decimal("1"))
        self.assertEqual(data["total_area"], Decimal("130"))
        self.assertEqual(data["covered_area"], Decimal("80"))

    def test_argencasas_parser_extracts_jsonld_geo_and_canonical_address(self):
        scraper = ArgencasasScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <script type="application/ld+json">
              {
                "@type": "RealEstateListing",
                "name": "Duplex en Venta en Hurlingham",
                "mainEntity": {
                  "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "Richieri (el mirador) al 1500",
                    "addressLocality": "Hurlingham"
                  },
                  "geo": {
                    "@type": "GeoCoordinates",
                    "latitude": "-34.587651022465",
                    "longitude": "-58.636475811363"
                  }
                },
                "offers": {"price": "130000", "priceCurrency": "USD", "seller": {"name": "FINCAS Propiedades"}}
              }
              </script>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.argencasas.com/propiedad-duplex-venta-hurlingham-302-1314")
        self.assertEqual(data["address"], "Tte. Gral. Pablo Ricchieri (el mirador) 1500")
        self.assertAlmostEqual(data["latitude"], -34.587651022465)
        self.assertAlmostEqual(data["longitude"], -58.636475811363)
        self.assertEqual(data["location_precision"], "exact")
        self.assertEqual(data["raw_data"]["argencasas_map_coordinate"]["method"], "jsonld_geo")

    def test_argencasas_parser_marks_removed_text(self):
        scraper = ArgencasasScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>Error</h1>
              <p>La propiedad ha sido retirada del sistema</p>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.argencasas.com/propiedad-chalet-venta-barrio-ingles-306-1235")
        self.assertEqual(data["source_status"], "removed")
        self.assertEqual(data["status"], Property.Status.REMOVED)

    def test_valenti_parser_reads_json_ld_coordinates_and_metrics(self):
        scraper = ValentiScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><head>
              <script>var code='143-16', latitud=-34.595734427622, longitud=-58.629698753357;</script>
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "RealEstateListing",
                "name": "Chalet en Venta en Barrio Ingles",
                "description": "Distinguido chalet",
                "offers": {
                  "@type": "Offer",
                  "price": "350000",
                  "priceCurrency": "USD",
                  "seller": {"@type": "RealEstateAgent", "name": "VALENTI PROPIEDADES"}
                }
              }
              </script>
            </head><body>
              <div class="calle_precio">NECOCHEA al 1400 <span>u$s 350.000</span></div>
              <h1>Chalet venta Barrio Ingles</h1>
              <p>5 Ambientes 3 Dormitorios 4 Banos 197 m2 Sup Cubierta 500 m2 Sup Total 25 Anos</p>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://www.valentipropiedades.com.ar/propiedad-chalet-venta-barrio-ingles-143-16")
        self.assertEqual(data["external_id"], "143-16")
        self.assertEqual(data["agency"], "VALENTI PROPIEDADES")
        self.assertEqual(data["price"], Decimal("350000"))
        self.assertEqual(data["address"], "General Mariano Necochea 1400")
        self.assertEqual(data["rooms"], 5)
        self.assertEqual(data["covered_area"], Decimal("197"))
        self.assertEqual(data["total_area"], Decimal("500"))
        self.assertAlmostEqual(data["latitude"], -34.595734427622)
        self.assertEqual(data["location_precision"], "exact")

    def test_xintel_api_discovery_and_parser_filter_hurlingham(self):
        scraper = GabrielParisScraper()

        def fake_api(params):
            if params["json"] == "fichas.propiedades":
                return {
                    "resultado": {
                        "datos": {"codigo_ficha": params["id"]},
                        "img": ["https://img.example/gpa4383.jpg"],
                        "ficha": [
                            {
                                "in_fic": "4383",
                                "in_num": "4383",
                                "in_ope": "V",
                                "in_tip": "Casa",
                                "in_tpr": "PH",
                                "titulo": "Casa en venta Villa Tesei 3 ambientes",
                                "in_loc": "Hurlingham",
                                "in_bar": "Villa Tesei",
                                "in_cal": "Juan Jofre",
                                "in_nro": "330",
                                "precio": "U$S 55.000",
                                "in_val": "55000",
                                "cantidad_ambientes": "3",
                                "cantidad_dormitorios": "2",
                                "in_bao": "2",
                                "in_sto": "196.00",
                                "latitud": "-34.6315826",
                                "longitud": "-58.6299",
                            }
                        ],
                    }
                }
            if params.get("sSearch") == "hurlingham":
                return {"resultado": {"datos": {"cantidadFichas": 1}}}
            return {
                "resultado": {
                    "datos": {"cantidadFichas": 2, "paginas": 1},
                    "fichas": [
                        {"in_num": "1", "in_loc": "Ituzaingo", "amigable": "casa-en-venta-ituzaingo-ficha-gpa1"},
                        {
                            "in_num": "4383",
                            "in_loc": "Hurlingham",
                            "in_bar": "Villa Tesei",
                            "titulo": "Casa Villa Tesei",
                            "amigable": "casa-en-venta-en-villa-tesei-ficha-gpa4383",
                        },
                    ],
                }
            }

        scraper._api_get = fake_api
        urls = list(scraper.discover())
        self.assertEqual(urls, ["https://gabrielparis.com.ar/casa-en-venta-en-villa-tesei-ficha-gpa4383"])
        self.assertEqual(scraper.discovery_stats["declared_total"], 1)
        data = scraper.parse(urls[0])
        self.assertEqual(data["external_id"], "4383")
        self.assertEqual(data["locality"], "Villa Tesei")
        self.assertEqual(data["price"], Decimal("55000"))
        self.assertEqual(data["rooms"], 3)
        self.assertEqual(data["latitude"], -34.6315826)

    def test_mudafy_parser_reads_product_and_embedded_coordinates(self):
        scraper = MudafyScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><head>
              <title>Oficina en venta, USD 215.000 | Hurlingham | Mudafy</title>
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Av. Tte. Gral. Julio A. Roca al 1200",
                "description": "Venta de Oficina, Hurlingham",
                "image": "https://img.example/mudafy.jpg",
                "offers": {"@type": "Offer", "price": 215000, "priceCurrency": "USD"}
              }
              </script>
            </head><body>
              <p>VENTA USD 215.000 Av. Tte. Gral. Julio A. Roca al 1200 Oficina en venta en Hurlingham</p>
              <p>Detalle Superficie total : 154 m2 Ambientes : 3 Banos : 1 Dormitorios : 2</p>
              <script>self.__next_f.push([1, "{\\"address\\":{\\"coordinates\\":{\\"latitude\\":-34.5832725,\\"longitude\\":-58.6355559}}}"])</script>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://mudafy.com.ar/propiedades/av-roca-oficina-en-venta-334867")
        self.assertEqual(data["external_id"], "334867")
        self.assertEqual(data["price"], Decimal("215000"))
        self.assertEqual(data["address"], "Av. Tte. Gral. Julio Argentino Roca 1200")
        self.assertEqual(data["property_type"], Property.Type.OTHER)
        self.assertEqual(data["total_area"], Decimal("154"))
        self.assertAlmostEqual(data["latitude"], -34.5832725)

    def test_matias_szpira_api_discovery_and_parse(self):
        scraper = MatiasSzpiraScraper()

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_get(url):
            if "/api/results.json" in url:
                return Response(
                    {
                        "resultado": {
                            "datos": {"cantidadFichas": 1, "paginas": 1},
                            "fichas": [
                                {
                                    "in_num": "8067906",
                                    "in_ope": "V",
                                    "in_loc": "Hurlingham",
                                    "precio": "USD 43000",
                                }
                            ],
                        }
                    }
                )
            return Response(
                {
                    "resultado": {
                        "datos": {"codigo_ficha": "8067906"},
                        "img": ["https://static.tokkobroker.com/pictures/8067906.jpg"],
                        "ficha": [
                            {
                                "in_num": "8067906",
                                "in_ope": "V",
                                "tipo": "Departamento",
                                "titulo": "Departamento",
                                "precio": "USD 43000",
                                "in_loc": "Hurlingham",
                                "in_bar": "",
                                "direccion": "Angel Acuna 1100, Hurlingham",
                                "in_cub": "52.00",
                                "in_sto": "52.00",
                                "in_amb": "1",
                                "ti_dor": "0",
                                "in_bao": "1",
                                "latitud": "-34.6216922",
                                "longitud": "-58.6299204",
                            }
                        ],
                    }
                }
            )

        scraper.get = fake_get
        urls = list(scraper.discover())
        self.assertEqual(urls, ["https://www.matiasszpira.com.ar/propiedad/8067906/"])
        data = scraper.parse(urls[0])
        self.assertEqual(data["external_id"], "8067906")
        self.assertEqual(data["price"], Decimal("43000"))
        self.assertEqual(data["covered_area"], Decimal("52.00"))
        self.assertAlmostEqual(data["longitude"], -58.6299204)

    def test_barbieri_no_disponible_marks_suspended(self):
        scraper = MatiasBarbieriScraper()
        scraper.soup = lambda _url: BeautifulSoup(
            """
            <html><body>
              <h1>CASA EN VENTA - Hurlingham</h1>
              <div class="rh_page__property_title">Inicio Hurlingham CASA EN VENTA - Hurlingham Lima 4686, Villa Tesei, Provincia de Buenos Aires, Argentina Venta</div>
              <div class="rh_page__property_price"><p class="price">- NO DISPONIBLE - USD 120.000</p></div>
              <div class="rh_property__content">ID de la propiedad : ID-8118 Habitaciones 2 Banos 2 Garaje 1 Superficie Cubierta 139 mts2 Superficie Total 209 mts2 Descripcion Linda casa en venta. Villa Tesei. Caracteristicas Electricidad</div>
              <div class="rh_property__meta_wrap">Habitaciones 2 Banos 2 Garaje 1 Superficie Cubierta 139 mts2 Superficie Total 209 mts2</div>
              <script>var propertyMapData = {"lat":"-34.6305082","lng":"-58.6323452"};</script>
            </body></html>
            """,
            "lxml",
        )
        data = scraper.parse("https://barbieripropiedades.com.ar/propiedad/casa-en-venta-hurlingham/")
        self.assertEqual(data["external_id"], "ID-8118")
        self.assertEqual(data["status"], Property.Status.SUSPENDED)
        self.assertEqual(data["source_status"], "no_disponible")
        self.assertIsNone(data["price"])
        self.assertEqual(data["currency"], "")
        self.assertEqual(data["address"], "Lima 4686")
        self.assertEqual(data["total_area"], Decimal("209"))

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
            url = f"{scraper_cls.definition.base_url}/casa-en-venta-en-hurlingham--11598328"
            if scraper_cls is ZonapropScraper:
                url = (
                    "https://www.zonaprop.com.ar/propiedades/clasificado/"
                    "veclcain-casa-en-venta-en-hurlingham-57923940.html"
                )
            data = self.parse_with_fixture(
                scraper_cls,
                "portal_detail.html",
                url,
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

    def test_patagonprop_status_badges_mark_inactive_and_clear_placeholder_price(self):
        cases = [
            ("Vendida", Property.Status.SOLD, "sold"),
            ("Reservada", Property.Status.RESERVED, "reserved"),
            ("Suspendida", Property.Status.SUSPENDED, "suspended"),
        ]
        for badge, expected_status, expected_source_status in cases:
            scraper = PatagonPropScraper()
            scraper.soup = lambda parsed_url, badge=badge: BeautifulSoup(
                f"""
                <html><body>
                  <h1>Casa en Venta en Hurlingham, Hurlingham, Buenos Aires</h1>
                  <span>{badge}</span>
                  <div>USD 1</div>
                  <dl>
                    <dt>Dirección</dt><dd>Rossini 2165</dd>
                    <dt>Ubicación</dt><dd>Hurlingham</dd>
                    <dt>Tipo</dt><dd>Casa</dd>
                  </dl>
                  <img src="https://img.example/casa.jpg">
                </body></html>
                """,
                "lxml",
            )
            data = scraper.parse(
                "https://patagonprop.com/propiedad/venta-de-casa-en-hurlingham-hurlingham-buenos-aires-708-1/hash"
            )
            self.assertEqual(data["status"], expected_status)
            self.assertEqual(data["source_status"], expected_source_status)
            self.assertIsNone(data["price"])
            self.assertEqual(data["raw_data"]["patagonprop_status_badge"], badge)

    def test_repair_metrics_applies_patagonprop_inactive_status(self):
        source = Source.objects.create(
            slug="patagonprop",
            name="PatagonProp",
            base_url="https://patagonprop.com",
        )
        mapaprop = Source.objects.create(
            slug="mapaprop",
            name="Mapaprop",
            base_url="https://www.mapaprop.com",
        )
        property_obj = Property.objects.create(
            fingerprint="patagonprop-repair-status",
            title="Casa en Hurlingham",
            property_type=Property.Type.HOUSE,
            operation="sale",
            address="Rossini 2165",
            locality="Hurlingham",
            currency="USD",
            price=Decimal("1"),
            status=Property.Status.ACTIVE,
        )
        Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="patagon-1",
            url="https://patagonprop.com/propiedad/venta-de-casa-en-hurlingham-hurlingham-buenos-aires-708-8404/hash",
        )
        Listing.objects.create(
            source=mapaprop,
            property=property_obj,
            external_id="mapaprop-1",
            url="https://www.mapaprop.com/en/property/venta-de-casa-en-hurlingham-hurlingham-buenos-aires-708-8404/hash",
        )

        class FakeAdapter:
            definition = type("Definition", (), {"crawl_delay": 0})()

            def parse(self, url):
                return {
                    "external_id": "patagon-1",
                    "url": url,
                    "title": "Casa en Hurlingham",
                    "property_type": Property.Type.HOUSE,
                    "operation": "sale",
                    "address": "Rossini 2165",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": None,
                    "status": Property.Status.SOLD,
                    "source_status": "sold",
                    "raw_data": {"patagonprop_status_badge": "Vendida"},
                }

        with patch("properties.management.commands.repair_metrics.get_adapter", return_value=FakeAdapter()):
            call_command("repair_metrics", "--source", "patagonprop", "--property-id", str(property_obj.pk), stdout=StringIO())

        property_obj.refresh_from_db()
        listing = property_obj.listings.get(source=source)
        self.assertEqual(property_obj.status, Property.Status.SOLD)
        self.assertIsNone(property_obj.price)
        self.assertEqual(listing.source_status, "sold")
        self.assertEqual(listing.raw_data["patagonprop_status_badge"], "Vendida")

    def test_repair_metrics_apply_preserves_manual_overrides_and_merges_raw_data(self):
        source = Source.objects.create(
            slug="riquelme",
            name="Riquelme",
            base_url="https://www.riquelmepropiedades.com.ar",
        )
        property_obj = Property.objects.create(
            fingerprint="riquelme-repair-override",
            title="Titulo manual",
            property_type=Property.Type.HOUSE,
            operation="sale",
            address="Manual 123",
            locality="Hurlingham",
            currency="USD",
            price=Decimal("120000"),
            manual_overrides={"price": True, "address": True},
        )
        listing = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="riquelme-1",
            url="https://www.riquelmepropiedades.com.ar/propiedad/demo",
            raw_data={"existing": "kept"},
        )

        class FakeAdapter:
            definition = type("Definition", (), {"crawl_delay": 0})()

            def parse(self, url):
                return {
                    "external_id": "riquelme-1",
                    "url": url,
                    "title": "Casa reparada",
                    "property_type": Property.Type.HOUSE,
                    "operation": "sale",
                    "address": "Nueva 456",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": Decimal("90000"),
                    "status": Property.Status.RESERVED,
                    "source_status": "reserved",
                    "raw_data": {"riquelme_status_badge": "reserved"},
                }

        with patch("properties.management.commands.repair_metrics.get_adapter", return_value=FakeAdapter()):
            call_command("repair_metrics", "--source", "riquelme", "--apply", "--property-id", str(property_obj.pk), stdout=StringIO())

        property_obj.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(property_obj.price, Decimal("120000"))
        self.assertEqual(property_obj.address, "Manual 123")
        self.assertEqual(property_obj.status, Property.Status.RESERVED)
        self.assertEqual(listing.source_status, "reserved")
        self.assertEqual(listing.raw_data["existing"], "kept")
        self.assertEqual(listing.raw_data["riquelme_status_badge"], "reserved")

    def test_repair_metrics_can_apply_native_coordinates_and_recompute_territory(self):
        source = Source.objects.create(
            slug="century21-hurlingham",
            name="Century 21",
            base_url="https://century21.com.ar",
        )
        property_obj = Property.objects.create(
            fingerprint="century21-repair-location",
            title="Departamento",
            property_type=Property.Type.APARTMENT,
            operation="sale",
            locality="Hurlingham",
        )
        Listing.objects.create(
            source=source,
            property=property_obj,
            external_id="108737",
            url="https://century21.com.ar/propiedad/108737_departamento-en-venta",
        )

        class FakeAdapter:
            definition = type("Definition", (), {"crawl_delay": 0})()

            def parse(self, url):
                return {
                    "external_id": "108737",
                    "url": url,
                    "title": "Departamento",
                    "property_type": Property.Type.APARTMENT,
                    "operation": "sale",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": Decimal("93000"),
                    "latitude": -34.59,
                    "longitude": -58.627,
                    "location_precision": "exact",
                    "status": Property.Status.ACTIVE,
                    "raw_data": {"century21_entity": {"id": 108737}},
                }

        territory_result = SimpleNamespace(
            partido="Partido de Hurlingham",
            locality="Hurlingham",
            zone="Hurlingham Centro (Barrio Ingles)",
            confidence="high",
            source_method="test",
            needs_review=False,
            evidence={},
        )
        score = SimpleNamespace(
            overall_score=80,
            level="alto",
            zone_name="Hurlingham Centro (Barrio Ingles)",
            match_method="coordinates",
            confidence="high",
            transport_score=None,
            education_score=None,
            health_score=None,
            flood_penalty_score=None,
            urban_informality_score=None,
            environmental_penalty_score=None,
            development_potential_score=None,
            in_flood_risk_zone=None,
            nearest_renabap_m=None,
            nearest_sube_point_m=None,
            nearest_school_m=None,
            nearest_health_center_m=None,
            components={},
            risks={},
            evidence={},
            source_signature="test",
        )
        with (
            patch("properties.management.commands.repair_metrics.get_adapter", return_value=FakeAdapter()),
            patch("properties.management.commands.repair_metrics.infer_property_territory", return_value=territory_result),
            patch("properties.management.commands.repair_metrics.load_location_zones", return_value={"features": [], "signature": "test"}),
            patch("properties.management.commands.repair_metrics.score_property_location_intelligence", return_value=score),
        ):
            call_command(
                "repair_metrics",
                "--source",
                "century21-hurlingham",
                "--apply",
                "--infer-location",
                "--infer-territory",
                "--score-territory",
                "--property-id",
                str(property_obj.pk),
                stdout=StringIO(),
            )

        property_obj.refresh_from_db()
        self.assertAlmostEqual(property_obj.location.latitude, -34.59)
        self.assertEqual(property_obj.inferred_locality, "Hurlingham")
        self.assertEqual(property_obj.inferred_zone, "Hurlingham Centro (Barrio Ingles)")
        self.assertEqual(property_obj.location_intelligence.zone_name, "Hurlingham Centro (Barrio Ingles)")

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

    def test_faella_source_is_registered_enabled(self):
        slugs = {adapter.definition.slug for adapter in get_adapter_classes()}
        self.assertIn("faella", slugs)
        self.assertTrue(get_adapter("faella").definition.enabled)

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
            "hollmann-ariel",
            "valenti",
            "oscar-dahbar",
            "gabriel-paris",
            "hgranelli",
            "mudafy",
            "matias-szpira",
            "matias-barbieri",
            "nerina-allo",
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


class MarkMissingFromJobCommandTests(TestCase):
    def setUp(self):
        self.started_at = timezone.now() - timedelta(hours=1)
        self.finished_at = timezone.now()
        self.seen_at = self.started_at + timedelta(minutes=30)
        self.old_seen_at = self.started_at - timedelta(days=1)

    def create_source(self, slug="clean"):
        return Source.objects.create(
            slug=slug,
            name=slug.replace("-", " ").title(),
            base_url=f"https://{slug}.example.com",
        )

    def create_job(self, sources, **overrides):
        defaults = {
            "status": ScrapeJob.Status.SUCCESS,
            "selected_sources": [source.slug for source in sources],
            "worker_config": {source.slug: 1 for source in sources},
            "scrape_mode": ScrapeJob.Mode.COMPLETE,
            "mark_missing": False,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        defaults.update(overrides)
        return ScrapeJob.objects.create(**defaults)

    def create_job_source(self, job, source, **overrides):
        defaults = {
            "source": source,
            "slug": source.slug,
            "name": source.name,
            "status": ScrapeJobSource.Status.SUCCESS,
            "workers": 1,
            "total_discovered": 1,
            "total_to_process": 1,
            "processed": 1,
            "errors": 0,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        defaults.update(overrides)
        return ScrapeJobSource.objects.create(job=job, **defaults)

    def create_listing(self, source, external_id, last_seen_at, missing_runs=0):
        property_obj = Property.objects.create(
            fingerprint=f"mark-missing-job-{source.slug}-{external_id}",
            property_type=Property.Type.HOUSE,
            operation="sale",
            title=f"Casa {external_id}",
            status=Property.Status.ACTIVE,
        )
        listing = Listing.objects.create(
            source=source,
            property=property_obj,
            external_id=external_id,
            url=f"https://{source.slug}.example.com/{external_id}",
            missing_runs=missing_runs,
        )
        Listing.objects.filter(pk=listing.pk).update(last_seen_at=last_seen_at)
        listing.refresh_from_db()
        return listing

    def test_dry_run_reports_missing_without_writing(self):
        source = self.create_source("clean")
        job = self.create_job([source])
        self.create_job_source(job, source)
        seen = self.create_listing(source, "seen", self.seen_at)
        stale = self.create_listing(source, "stale", self.old_seen_at)

        output = StringIO()
        call_command("mark_missing_from_job", "--job-id", str(job.pk), stdout=output)

        seen.refresh_from_db()
        stale.refresh_from_db()
        self.assertTrue(seen.active)
        self.assertEqual(seen.missing_runs, 0)
        self.assertTrue(stale.active)
        self.assertEqual(stale.missing_runs, 0)
        self.assertIn(
            "[DRY] clean: vistas=1 ausentes=1 incrementan=1 desactivan=0",
            output.getvalue(),
        )
        self.assertIn("(sin cambios)", output.getvalue())

    def test_apply_uses_existing_two_missing_runs_policy(self):
        source = self.create_source("clean")
        job = self.create_job([source])
        self.create_job_source(job, source)
        seen = self.create_listing(source, "seen", self.seen_at)
        first_missing = self.create_listing(source, "first-missing", self.old_seen_at)
        second_missing = self.create_listing(
            source,
            "second-missing",
            self.old_seen_at,
            missing_runs=1,
        )

        output = StringIO()
        call_command(
            "mark_missing_from_job",
            "--job-id",
            str(job.pk),
            "--apply",
            stdout=output,
        )

        seen.refresh_from_db()
        first_missing.refresh_from_db()
        first_missing.property.refresh_from_db()
        second_missing.refresh_from_db()
        second_missing.property.refresh_from_db()
        self.assertTrue(seen.active)
        self.assertEqual(seen.missing_runs, 0)
        self.assertTrue(first_missing.active)
        self.assertEqual(first_missing.missing_runs, 1)
        self.assertEqual(first_missing.property.status, Property.Status.ACTIVE)
        self.assertFalse(second_missing.active)
        self.assertEqual(second_missing.missing_runs, 2)
        self.assertEqual(second_missing.property.status, Property.Status.REMOVED)
        self.assertIn(
            "[APPLY] clean: vistas=1 ausentes=2 incrementan=1 desactivan=1",
            output.getvalue(),
        )

    def test_apply_skips_unsafe_sources(self):
        unsafe_cases = [
            ("failed-source", {"status": ScrapeJobSource.Status.FAILED}),
            ("partial-source", {"status": ScrapeJobSource.Status.PARTIAL}),
            ("error-source", {"errors": 1}),
            ("zero-source", {"total_discovered": 0, "total_to_process": 0, "processed": 0}),
            ("incomplete-source", {"total_to_process": 2, "processed": 1}),
        ]
        sources = [self.create_source(slug) for slug, _ in unsafe_cases]
        job = self.create_job(sources)
        listings = []
        for source, (_, overrides) in zip(sources, unsafe_cases):
            self.create_job_source(job, source, **overrides)
            listings.append(self.create_listing(source, "stale", self.old_seen_at))

        output = StringIO()
        call_command(
            "mark_missing_from_job",
            "--job-id",
            str(job.pk),
            "--apply",
            stdout=output,
        )

        for listing in listings:
            listing.refresh_from_db()
            self.assertTrue(listing.active)
            self.assertEqual(listing.missing_runs, 0)
        text = output.getvalue()
        self.assertIn("[SKIP] failed-source: estado failed", text)
        self.assertIn("[SKIP] partial-source: estado partial", text)
        self.assertIn("[SKIP] error-source: 1 errores", text)
        self.assertIn("[SKIP] zero-source: sin fichas procesables", text)
        self.assertIn("[SKIP] incomplete-source: procesadas 1/2", text)

    def test_apply_skips_limited_jobs(self):
        source = self.create_source("limited")
        job = self.create_job([source], max_listings=1)
        self.create_job_source(job, source)
        stale = self.create_listing(source, "stale", self.old_seen_at)

        output = StringIO()
        call_command(
            "mark_missing_from_job",
            "--job-id",
            str(job.pk),
            "--apply",
            stdout=output,
        )

        stale.refresh_from_db()
        self.assertTrue(stale.active)
        self.assertEqual(stale.missing_runs, 0)
        self.assertIn("job tuvo limites de muestra/paginacion", output.getvalue())

    def test_job_166_exclusions_are_conservative_even_if_source_is_clean(self):
        source = self.create_source("remax-datawork")
        job = self.create_job([source], id=166)
        self.create_job_source(job, source)
        stale = self.create_listing(source, "stale", self.old_seen_at)

        output = StringIO()
        call_command(
            "mark_missing_from_job",
            "--job-id",
            str(job.pk),
            "--apply",
            stdout=output,
        )

        stale.refresh_from_db()
        self.assertTrue(stale.active)
        self.assertEqual(stale.missing_runs, 0)
        self.assertIn("exclusion conservadora", output.getvalue())


class RepairZonapropJobsCommandTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            slug="zonaprop",
            name="Zonaprop",
            base_url="https://www.zonaprop.com.ar",
        )
        self.started_at = timezone.now() - timedelta(hours=2)
        self.finished_at = timezone.now() - timedelta(hours=1)
        self.before_window = self.started_at - timedelta(days=1)
        self.job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.CANCELLED,
            selected_sources=[self.source.slug],
            worker_config={self.source.slug: 1},
            scrape_mode=ScrapeJob.Mode.COMPLETE,
            mark_missing=False,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )
        self.job_source = ScrapeJobSource.objects.create(
            job=self.job,
            source=self.source,
            slug=self.source.slug,
            name=self.source.name,
            status=ScrapeJobSource.Status.CANCELLED,
            workers=1,
            total_discovered=3,
            total_to_process=3,
            processed=2,
            created=2,
            updated=0,
            errors=0,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )

    def create_property(self, suffix, **overrides):
        defaults = {
            "fingerprint": f"repair-zonaprop-{suffix}",
            "property_type": Property.Type.HOUSE,
            "operation": "sale",
            "title": f"Casa {suffix}",
            "address": f"Test {suffix} 100",
            "normalized_address": f"test {suffix} 100",
            "locality": "Hurlingham",
            "currency": "USD",
            "price": Decimal("100000"),
            "status": Property.Status.ACTIVE,
        }
        defaults.update(overrides)
        return Property.objects.create(**defaults)

    def create_listing(self, property_obj, external_id, first_seen_at, last_seen_at=None):
        listing = Listing.objects.create(
            source=self.source,
            property=property_obj,
            external_id=external_id,
            url=f"https://www.zonaprop.com.ar/propiedades/clasificado/{external_id}.html",
        )
        Listing.objects.filter(pk=listing.pk).update(
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at or first_seen_at,
        )
        listing.refresh_from_db()
        return listing

    def create_snapshot(self, listing, **payload_overrides):
        payload = {
            "property_type": Property.Type.HOUSE,
            "operation": "sale",
            "title": "Casa original",
            "address": "Original 100",
            "normalized_address": "original 100",
            "locality": "Hurlingham",
            "currency": "USD",
            "price": "120000.00",
            "status": Property.Status.ACTIVE,
        }
        payload.update(payload_overrides)
        return ListingSnapshot.objects.create(
            listing=listing,
            content_hash=f"repair-{listing.pk}",
            price=payload.get("price"),
            currency=payload.get("currency") or "",
            status=payload.get("status") or "",
            payload=payload,
        )

    def test_dry_run_reports_counts_without_writing(self):
        deleteable = self.create_property("deleteable")
        delete_listing = self.create_listing(
            deleteable, "deleteable", self.started_at + timedelta(minutes=1)
        )
        shared = self.create_property("shared")
        survivor = self.create_listing(shared, "survivor", self.before_window)
        rollback = self.create_listing(
            shared, "shared-rollback", self.started_at + timedelta(minutes=2)
        )
        preserved = self.create_property("preserved", personal_notes="Mantener")
        preserved_listing = self.create_listing(
            preserved, "preserved", self.started_at + timedelta(minutes=3)
        )

        output = StringIO()
        call_command("repair_zonaprop_jobs", "--job-id", str(self.job.pk), stdout=output)

        text = output.getvalue()
        self.assertIn("DRY-RUN repair_zonaprop_jobs", text)
        self.assertIn("listings_candidatos=3", text)
        self.assertIn("propiedades_borrables=1", text)
        self.assertIn("propiedades_preservadas=1", text)
        self.assertIn("propiedades_recompuestas=1", text)
        for listing in (delete_listing, survivor, rollback, preserved_listing):
            listing.refresh_from_db()
            self.assertTrue(listing.active)
        self.assertTrue(Property.objects.filter(pk=deleteable.pk).exists())

    def test_apply_deletes_artifacts_without_manual_state(self):
        property_obj = self.create_property("delete")
        listing = self.create_listing(
            property_obj, "delete", self.started_at + timedelta(minutes=1)
        )

        call_command(
            "repair_zonaprop_jobs",
            "--job-id",
            str(self.job.pk),
            "--apply",
            stdout=StringIO(),
        )

        self.assertFalse(Listing.objects.filter(pk=listing.pk).exists())
        self.assertFalse(Property.objects.filter(pk=property_obj.pk).exists())
        identity = ListingIdentity.objects.get(
            source=self.source,
            external_id="delete",
        )
        self.assertEqual(identity.last_seen_reason, "repair_zonaprop_jobs")

    def test_apply_preserves_manual_property_as_removed(self):
        property_obj = self.create_property(
            "manual",
            personal_notes="Mantener esta observacion",
            is_hidden=True,
        )
        listing = self.create_listing(
            property_obj, "manual", self.started_at + timedelta(minutes=1)
        )

        call_command(
            "repair_zonaprop_jobs",
            "--job-id",
            str(self.job.pk),
            "--apply",
            stdout=StringIO(),
        )

        listing.refresh_from_db()
        property_obj.refresh_from_db()
        self.assertFalse(listing.active)
        self.assertEqual(listing.source_status, "removed")
        self.assertEqual(listing.missing_runs, 2)
        self.assertTrue(property_obj.is_hidden)
        self.assertEqual(property_obj.status, Property.Status.REMOVED)
        self.assertIn("Mantener esta observacion", property_obj.personal_notes)
        self.assertIn("Reparacion Zonaprop jobs", property_obj.personal_notes)

    def test_apply_recomposes_shared_property_from_survivor_snapshot(self):
        property_obj = self.create_property(
            "shared-restore",
            title="Titulo Zonaprop",
            address="Zonaprop 999",
            normalized_address="zonaprop 999",
            price=Decimal("999000"),
            personal_notes="Nota manual",
        )
        survivor = self.create_listing(property_obj, "survivor", self.before_window)
        self.create_snapshot(
            survivor,
            title="Titulo original",
            address="Original 123",
            normalized_address="original 123",
            price="120000.00",
        )
        rollback = self.create_listing(
            property_obj, "rollback", self.started_at + timedelta(minutes=1)
        )

        call_command(
            "repair_zonaprop_jobs",
            "--job-id",
            str(self.job.pk),
            "--apply",
            stdout=StringIO(),
        )

        property_obj.refresh_from_db()
        self.assertTrue(Listing.objects.filter(pk=survivor.pk).exists())
        self.assertFalse(Listing.objects.filter(pk=rollback.pk).exists())
        self.assertEqual(property_obj.title, "Titulo original")
        self.assertEqual(property_obj.address, "Original 123")
        self.assertEqual(property_obj.normalized_address, "original 123")
        self.assertEqual(property_obj.price, Decimal("120000.00"))
        self.assertEqual(property_obj.personal_notes, "Nota manual")


class SeedListingIdentitiesCommandTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            slug="zonaprop",
            name="Zonaprop",
            base_url="https://www.zonaprop.com.ar",
        )
        self.urls = [
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-1-111.html",
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-2-222.html",
            "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-1-111.html",
        ]

    class FakeAdapter:
        discovery_stats = {
            "declared_total": 2,
            "urls_discovered": 2,
            "coverage_ratio": 100.0,
        }

        def __init__(self, urls):
            self.urls = urls

        def discover(self):
            yield from self.urls

    @patch("properties.management.commands.seed_listing_identities.get_adapter")
    def test_seed_listing_identities_dry_run_does_not_write(self, get_adapter_mock):
        get_adapter_mock.return_value = self.FakeAdapter(self.urls)

        output = StringIO()
        call_command(
            "seed_listing_identities",
            "--source",
            self.source.slug,
            stdout=output,
        )

        self.assertEqual(ListingIdentity.objects.count(), 0)
        self.assertIn("DRY-RUN seed_listing_identities", output.getvalue())
        self.assertIn("nuevas=2", output.getvalue())

    @patch("properties.management.commands.seed_listing_identities.get_adapter")
    def test_seed_listing_identities_apply_creates_and_updates_memory(self, get_adapter_mock):
        get_adapter_mock.return_value = self.FakeAdapter(self.urls)
        ListingIdentity.objects.create(
            source=self.source,
            external_id="veclcain-casa-1-111.html",
            url="https://old.example/old.html",
            last_seen_reason="old",
        )

        output = StringIO()
        call_command(
            "seed_listing_identities",
            "--source",
            self.source.slug,
            "--apply",
            stdout=output,
        )

        self.assertEqual(ListingIdentity.objects.count(), 2)
        self.assertEqual(Listing.objects.count(), 0)
        existing = ListingIdentity.objects.get(
            source=self.source,
            external_id="veclcain-casa-1-111.html",
        )
        created = ListingIdentity.objects.get(
            source=self.source,
            external_id="veclcain-casa-2-222.html",
        )
        self.assertEqual(existing.last_seen_reason, "seed_discovery")
        self.assertEqual(created.last_seen_reason, "seed_discovery")
        self.assertIn("Identidades sembradas", output.getvalue())


class ScrapeCommandTests(TransactionTestCase):
    def test_inmuebles_clarin_is_permanently_blocked(self):
        slugs = {item["slug"] for item in source_catalog(include_disabled=True)}
        self.assertNotIn("inmuebles-clarin", slugs)
        with self.assertRaisesMessage(ValueError, "bloqueada permanentemente"):
            create_scrape_job(["inmuebles-clarin"], {"inmuebles-clarin": 1})

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

    def test_scrape_job_source_serializes_phase_durations(self):
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
            discovery_stats = {"urls_discovered": 1}

            def discover(self):
                return ["https://example.com/1"]

            def parse(self, url):
                return {
                    "external_id": "1",
                    "url": url,
                    "title": "Casa 1",
                    "address": "Calle 1 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            job = create_scrape_job(
                ["fake"],
                {"fake": 1},
                geocode_limit=0,
                mark_missing=False,
            )
            run_scrape_job(job.pk)

        job.refresh_from_db()
        source = job.sources.get(slug="fake")
        self.assertIsNotNone(source.discovery_started_at)
        self.assertIsNotNone(source.discovery_finished_at)
        self.assertIsNotNone(source.processing_started_at)
        self.assertIsNotNone(source.processing_finished_at)
        self.assertIsNone(source.geocoding_started_at)
        payload = serialize_job(job)
        source_payload = payload["sources"][0]
        self.assertIn("discovery_seconds", source_payload)
        self.assertIn("processing_seconds", source_payload)
        self.assertEqual(source_payload["geocoding_seconds"], 0)
        self.assertEqual(source_payload["processed"], 1)

    def test_source_catalog_exposes_last_run_metrics(self):
        source = Source.objects.create(
            slug="valenti",
            name="Valenti Propiedades",
            base_url="https://www.valentipropiedades.com.ar",
        )
        started_at = timezone.now() - timedelta(minutes=12)
        discovery_started = started_at
        discovery_finished = started_at + timedelta(seconds=20)
        processing_started = discovery_finished
        processing_finished = processing_started + timedelta(seconds=50)
        finished_at = processing_finished
        job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.SUCCESS,
            selected_sources=["valenti"],
            worker_config={"valenti": 2},
            scrape_mode=ScrapeJob.Mode.TRIAL,
            max_pages=2,
            max_listings=10,
            geocode_limit=0,
            mark_missing=False,
            started_at=started_at,
            finished_at=finished_at,
        )
        ScrapeJobSource.objects.create(
            job=job,
            source=source,
            slug="valenti",
            name="Valenti Propiedades",
            status=ScrapeJobSource.Status.SUCCESS,
            workers=2,
            total_discovered=20,
            total_to_process=10,
            processed=10,
            created=3,
            updated=7,
            skipped=1,
            errors=0,
            started_at=started_at,
            discovery_started_at=discovery_started,
            discovery_finished_at=discovery_finished,
            processing_started_at=processing_started,
            processing_finished_at=processing_finished,
            finished_at=finished_at,
        )

        catalog = {item["slug"]: item for item in source_catalog(include_disabled=True)}
        last_run = catalog["valenti"]["last_run"]

        self.assertEqual(last_run["job_id"], job.pk)
        self.assertEqual(last_run["processed"], 10)
        self.assertEqual(last_run["created"], 3)
        self.assertEqual(last_run["updated"], 7)
        self.assertEqual(last_run["skipped"], 1)
        self.assertEqual(last_run["discovery_seconds"], 20)
        self.assertEqual(last_run["processing_seconds"], 50)
        self.assertEqual(last_run["scrape_mode"], ScrapeJob.Mode.TRIAL)
        self.assertEqual(last_run["max_pages"], 2)
        self.assertFalse(last_run["mark_missing"])

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

    def test_incomplete_discovery_does_not_mark_missing(self):
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
                self.discovery_stats = {
                    "declared_total": 2,
                    "pages_seen": 1,
                    "urls_discovered": 1,
                    "coverage_ratio": 50.0,
                    "coverage_complete": False,
                }

            def discover(self):
                return ["https://example.com/fresh"]

            def parse(self, url):
                return {
                    "external_id": "fresh",
                    "url": url,
                    "title": "Casa fresh",
                    "address": "Calle Fresh 100",
                    "locality": "Hurlingham",
                    "currency": "USD",
                    "price": "100000",
                }

        with patch("properties.services.scraping.get_adapter", return_value=FakeAdapter()):
            job = create_scrape_job(["fake"], {"fake": 1})
            source = Source.objects.get(slug="fake")
            stale_property = Property.objects.create(
                fingerprint="stale-fake-listing",
                property_type=Property.Type.HOUSE,
                operation="sale",
                title="Casa stale",
                status=Property.Status.ACTIVE,
            )
            stale = Listing.objects.create(
                source=source,
                property=stale_property,
                external_id="stale",
                url="https://example.com/stale",
                missing_runs=1,
            )
            run_scrape_job(job.pk)

        stale.refresh_from_db()
        stale.property.refresh_from_db()
        job.refresh_from_db()
        job_source = job.sources.get(slug="fake")
        self.assertTrue(stale.active)
        self.assertEqual(stale.missing_runs, 1)
        self.assertEqual(stale.property.status, Property.Status.ACTIVE)
        self.assertEqual(job_source.status, ScrapeJobSource.Status.PARTIAL)
        self.assertIn("no se marcan ausentes", job_source.logs.lower())

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
