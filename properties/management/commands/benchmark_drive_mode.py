import gzip
import json
import math
import statistics
import time

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.test.utils import CaptureQueriesContext

from properties.services.drive_mode import (
    DriveModeValidationError,
    drive_property_card,
    nearby_drive_properties,
)


def percentile(values, percentage):
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentage) - 1)
    return ordered[index]


class Command(BaseCommand):
    help = "Mide en modo lectura la latencia, consultas y tamano del API de recorrido."

    def add_arguments(self, parser):
        parser.add_argument("--latitude", type=float, default=-34.59)
        parser.add_argument("--longitude", type=float, default=-58.64)
        parser.add_argument("--radius", type=int, default=350)
        parser.add_argument("--iterations", type=int, default=10)
        parser.add_argument("--property-type", action="append", default=[])
        parser.add_argument("--price-min", type=int)
        parser.add_argument("--price-max", type=int)
        parser.add_argument("--bedrooms-min", type=int)
        parser.add_argument("--covered-area-min", type=int)
        parser.add_argument("--land-area-min", type=int)
        parser.add_argument("--card-id", type=int)

    def handle(self, *args, **options):
        iterations = options["iterations"]
        if not 1 <= iterations <= 100:
            raise CommandError("--iterations debe estar entre 1 y 100.")
        payload = {
            "latitude": options["latitude"],
            "longitude": options["longitude"],
            "radius_m": options["radius"],
            "property_types": options["property_type"],
            "price_min": options["price_min"],
            "price_max": options["price_max"],
            "bedrooms_min": options["bedrooms_min"],
            "covered_area_min_m2": options["covered_area_min"],
            "land_area_min_m2": options["land_area_min"],
        }
        try:
            nearby_drive_properties(payload)
        except DriveModeValidationError as exc:
            raise CommandError(str(exc)) from exc

        elapsed_ms = []
        query_counts = []
        result = None
        for _index in range(iterations):
            started_at = time.perf_counter()
            with CaptureQueriesContext(connection) as captured:
                result = nearby_drive_properties(payload)
            elapsed_ms.append((time.perf_counter() - started_at) * 1000)
            query_counts.append(len(captured))

        encoded = json.dumps(result, cls=DjangoJSONEncoder, separators=(",", ":")).encode()
        report = {
            "nearby": {
                "iterations": iterations,
                "p50_ms": round(statistics.median(elapsed_ms), 1),
                "p95_ms": round(percentile(elapsed_ms, 0.95), 1),
                "max_ms": round(max(elapsed_ms), 1),
                "queries_min": min(query_counts),
                "queries_max": max(query_counts),
                "properties": result["count"],
                "groups": len({item["group_id"] for item in result["properties"]}),
                "json_bytes": len(encoded),
                "gzip_bytes": len(gzip.compress(encoded)),
                "filters": result["applied_filters"],
            }
        }
        card_id = options.get("card_id")
        if card_id:
            started_at = time.perf_counter()
            with CaptureQueriesContext(connection) as captured:
                card = drive_property_card(card_id)
            report["card"] = {
                "id": card_id,
                "available": card is not None,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                "queries": len(captured),
            }
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
