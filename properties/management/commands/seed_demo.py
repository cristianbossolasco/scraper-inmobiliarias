from django.core.management.base import BaseCommand

from properties.models import Source
from properties.services.ingestion import ingest_listing


DEMO = [
    {
        "external_id": "demo-1",
        "url": "https://example.com/demo-1",
        "title": "Casa con jardín en Parque Johnston",
        "description": "Casa luminosa con pileta, quincho y amplio parque.",
        "address": "Gurruchaga 1733",
        "locality": "Hurlingham",
        "neighborhood": "Parque Johnston",
        "property_type": "house",
        "currency": "USD",
        "price": 175000,
        "rooms": 5,
        "bedrooms": 3,
        "bathrooms": 2,
        "garages": 2,
        "covered_area": 165,
        "land_area": 420,
        "features": ["Pileta", "Quincho", "Jardín"],
        "latitude": -34.5871,
        "longitude": -58.6378,
        "location_precision": "exact",
        "agency": "Datos de demostración",
    },
    {
        "external_id": "demo-2",
        "url": "https://example.com/demo-2",
        "title": "Dúplex de tres dormitorios en Villa Tesei",
        "description": "Dúplex con patio, parrilla y cochera.",
        "address": "La Patria al 3500",
        "locality": "Villa Tesei",
        "neighborhood": "Villa Tesei",
        "property_type": "duplex",
        "currency": "USD",
        "price": 109000,
        "rooms": 4,
        "bedrooms": 3,
        "bathrooms": 1,
        "garages": 1,
        "covered_area": 115,
        "land_area": 180,
        "features": ["Parrilla", "Patio"],
        "latitude": -34.6192,
        "longitude": -58.6335,
        "location_precision": "street",
        "agency": "Datos de demostración",
    },
    {
        "external_id": "demo-3",
        "url": "https://example.com/demo-3",
        "title": "Chalet sobre gran lote en William C. Morris",
        "description": "Chalet a reciclar sobre lote arbolado.",
        "address": "Cañuelas y Villegas",
        "locality": "William C. Morris",
        "neighborhood": "William C. Morris",
        "property_type": "house",
        "currency": "USD",
        "price": 82000,
        "rooms": 4,
        "bedrooms": 2,
        "bathrooms": 1,
        "covered_area": 90,
        "land_area": 360,
        "features": ["Jardín", "A reciclar"],
        "latitude": -34.6502,
        "longitude": -58.6688,
        "location_precision": "intersection",
        "agency": "Datos de demostración",
    },
]


class Command(BaseCommand):
    help = "Carga tres propiedades ficticias para probar la interfaz."

    def handle(self, *args, **options):
        source, _ = Source.objects.get_or_create(
            slug="demo",
            defaults={
                "name": "Demostración",
                "base_url": "https://example.com",
                "enabled": False,
            },
        )
        for data in DEMO:
            ingest_listing(source, data)
        self.stdout.write(self.style.SUCCESS("Datos de demostración cargados."))
