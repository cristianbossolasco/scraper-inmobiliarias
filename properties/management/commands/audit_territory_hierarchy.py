from collections import Counter

from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.normalization import normalize_neighborhood_name
from properties.services.zone_names import UNIFIED_HURLINGHAM_CENTRO_ZONE


LEGACY_ZONE_ALIASES = {
    "barrio ingles",
    "barrio inglés",
    "ingles",
    "inglés",
    "hurlingham centro",
    "parque jhonston",
}


class Command(BaseCommand):
    help = "Audita calidad de la jerarquia territorial inferida en propiedades."

    def add_arguments(self, parser):
        parser.add_argument("--sample", type=int, default=20)

    def handle(self, *args, **options):
        sample_size = max(0, options["sample"])
        queryset = Property.objects.select_related("location").order_by("pk")
        counters = Counter()
        samples = {
            "sin_coordenadas": [],
            "fuera_partido": [],
            "sin_zona": [],
            "conflicto_texto_poligono": [],
            "alias_viejo": [],
        }

        for property_obj in queryset.iterator(chunk_size=500):
            has_coordinates = bool(getattr(property_obj, "location", None)) or (
                property_obj.detected_latitude is not None
                and property_obj.detected_longitude is not None
            )
            if not has_coordinates:
                self._mark(counters, samples, "sin_coordenadas", property_obj.pk, sample_size)
            if property_obj.territory_inferred_at and not property_obj.inferred_partido:
                self._mark(counters, samples, "fuera_partido", property_obj.pk, sample_size)
            if property_obj.inferred_partido and not property_obj.inferred_zone:
                self._mark(counters, samples, "sin_zona", property_obj.pk, sample_size)
            if property_obj.zone_conflict:
                self._mark(counters, samples, "conflicto_texto_poligono", property_obj.pk, sample_size)
            if self._has_legacy_alias(property_obj):
                self._mark(counters, samples, "alias_viejo", property_obj.pk, sample_size)

        self.stdout.write("Auditoria territorial")
        for key in samples:
            self.stdout.write(f"- {key}: {counters[key]} sample={samples[key]}")

    def _mark(self, counters, samples, key, property_id, sample_size):
        counters[key] += 1
        if len(samples[key]) < sample_size:
            samples[key].append(property_id)

    def _has_legacy_alias(self, property_obj):
        values = [
            property_obj.neighborhood,
            property_obj.detected_neighborhood,
            property_obj.inferred_neighborhood,
            property_obj.inferred_zone,
        ]
        for raw in values:
            text = (raw or "").strip().lower()
            if text in LEGACY_ZONE_ALIASES:
                return True
            if text == UNIFIED_HURLINGHAM_CENTRO_ZONE.lower():
                continue
            normalized = normalize_neighborhood_name(raw)
            if normalized == UNIFIED_HURLINGHAM_CENTRO_ZONE and text in LEGACY_ZONE_ALIASES:
                return True
        return False
