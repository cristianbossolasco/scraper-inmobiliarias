from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from properties.models import Property
from properties.services.zone_inference import apply_zone_inference, infer_property_zone


class Command(BaseCommand):
    help = "Infiere barrio/zona por coordenadas y poligonos GeoJSON."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Guarda los cambios.")
        parser.add_argument("--dry-run", action="store_true", help="Muestra cambios sin guardar.")
        parser.add_argument("--source", help="Filtra por slug de fuente.")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--limit", type=int, default=0, help="0 procesa todas.")
        parser.add_argument("--geojson", help="Ruta alternativa al GeoJSON de zonas.")
        parser.add_argument("--max-distance-m", type=float)
        parser.add_argument(
            "--geocode-missing",
            action="store_true",
            help="Permite llamar al geocoder para direcciones no cacheadas. Solo se ejecuta con --apply.",
        )
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("Usa --apply o --dry-run, no ambos.")
        if options["limit"] is not None and options["limit"] < 0:
            raise CommandError("--limit debe ser positivo o 0 para procesar todo.")

        dry_run = not options["apply"]
        allow_external_geocoding = options["geocode_missing"] and not dry_run
        if options["geocode_missing"] and dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "dry-run activo: no se haran llamadas externas de geocoding."
                )
            )

        queryset = Property.objects.select_related("location").all().order_by("id")
        if options["source"]:
            queryset = queryset.filter(listings__source__slug=options["source"])
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        queryset = queryset.distinct()

        total_candidates = queryset.count()
        limit = options["limit"]
        if limit:
            queryset = queryset[:limit]
            total = min(total_candidates, limit)
        else:
            total = total_candidates

        if not options["quiet"]:
            mode = "dry-run" if dry_run else "apply"
            self.stdout.write(f"Modo: {mode}")
            self.stdout.write(f"Candidatas: {total_candidates}; a procesar: {total}")
            if options["source"]:
                self.stdout.write(f"Fuente: {options['source']}")

        stats = Counter()
        for index, property_obj in enumerate(queryset, start=1):
            try:
                result = infer_property_zone(
                    property_obj,
                    geojson_path=options["geojson"],
                    max_distance_m=options["max_distance_m"],
                    geocode_missing=allow_external_geocoding,
                )
            except Exception as exc:
                stats["errors"] += 1
                self.stderr.write(f"propiedad {property_obj.pk}: {exc}")
                continue

            stats["processed"] += 1
            stats[f"geocode_{result.geocoding_status}"] += 1
            if result.inferred_neighborhood:
                stats["inferred"] += 1
            if result.method.endswith("_polygon"):
                stats["strict_polygon"] += 1
            if result.method.endswith("_nearest"):
                stats["nearest_fallback"] += 1
            if result.zone_conflict:
                stats["conflicts"] += 1
            if result.needs_review:
                stats["needs_review"] += 1
            if result.method == "no_coordinates":
                stats["no_data"] += 1

            if not dry_run:
                apply_zone_inference(property_obj, result)

            if not options["quiet"]:
                status = result.inferred_neighborhood or "sin zona"
                suffix = " conflicto" if result.zone_conflict else ""
                self.stdout.write(
                    self._safe(
                        f"{index}/{total} propiedad {property_obj.pk}: "
                        f"{status} ({result.method}){suffix}"
                    )
                )

        suffix = " (dry-run)" if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                self._safe(
                    "Inferencia terminada"
                    f"{suffix}: {stats['inferred']} inferidas; "
                    f"{stats['strict_polygon']} estrictas; "
                    f"{stats['nearest_fallback']} por cercania; "
                    f"{stats['conflicts']} conflictos; "
                    f"{stats['needs_review']} para revisar; "
                    f"{stats['no_data']} sin datos; "
                    f"{stats['errors']} errores."
                )
            )
        )

    def _safe(self, value):
        return str(value).encode("cp1252", errors="replace").decode("cp1252")
