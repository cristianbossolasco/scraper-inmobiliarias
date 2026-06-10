from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.geocoding import Geocoder, best_address, geocodable_address_q


class Command(BaseCommand):
    help = "Geocodifica propiedades que todavia no tienen ubicacion."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--source")
        parser.add_argument("--only-with-address", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        quiet = options["quiet"]
        if limit is not None and limit < 0:
            self.stderr.write("--limit debe ser positivo o 0 para procesar todo.")
            return

        queryset = Property.objects.all().order_by("-last_seen_at")
        if not options["force"]:
            queryset = queryset.filter(location__isnull=True)
        if options["source"]:
            queryset = queryset.filter(listings__source__slug=options["source"])
        if options["only_with_address"]:
            queryset = queryset.filter(geocodable_address_q())
        queryset = queryset.distinct()

        total_candidates = queryset.count()
        effective_total = total_candidates if limit == 0 else min(total_candidates, limit)
        if not quiet:
            self.stdout.write(f"Candidatas totales: {total_candidates}")
            if options["source"]:
                self.stdout.write(f"Fuente: {options['source']}")
            self.stdout.write(
                "Limite aplicado: "
                + ("sin limite" if limit == 0 else str(limit))
            )
            self.stdout.write(
                f"A procesar: {effective_total} "
                f"(estimado hasta {effective_total} segundos si no hay cache)."
            )

        if limit == 0:
            batch = queryset
        else:
            batch = queryset[:limit]

        geocoder = Geocoder()
        located = 0
        no_result = 0
        errors = 0
        for index, property_obj in enumerate(batch, start=1):
            address = best_address(property_obj) or "sin direccion"
            try:
                if geocoder.geocode_property(property_obj, force=options["force"]):
                    located += 1
                    result = "OK"
                else:
                    no_result += 1
                    result = "sin resultado"
            except Exception as exc:
                errors += 1
                result = f"error: {exc}"
                self.stderr.write(f"{property_obj.pk}: {exc}")
            if not quiet:
                self.stdout.write(
                    f"{index}/{effective_total} propiedad {property_obj.pk}: "
                    f"{address} -> {result}"
                )
                if index % 10 == 0 or index == effective_total:
                    percent = (index / effective_total * 100) if effective_total else 100
                    self.stdout.write(
                        f"Parcial {percent:.0f}%: {located} geolocalizadas; "
                        f"{no_result} sin resultado; {errors} errores."
                    )
        self.stdout.write(
            self.style.SUCCESS(
                f"{located} propiedades geolocalizadas; {no_result} sin resultado; {errors} errores."
            )
        )
