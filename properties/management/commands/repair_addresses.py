from django.core.management.base import BaseCommand

from properties.models import Property
from properties.services.geocoding import Geocoder, address_number, street_key
from properties.services.location_enrichment import clean_detected_address
from properties.services.normalization import (
    extract_embedded_neighborhood,
    normalize_address,
)
from properties.services.zone_names import UNIFIED_HURLINGHAM_CENTRO_ZONE


def same_geocoding_target(before, after):
    return (
        bool(before and after)
        and address_number(before) == address_number(after)
        and street_key(before) == street_key(after)
    )


KNOWN_ADDRESS_CORRECTIONS = {
    3454: {"address": "BUSTAMANTE 2600", "locality": "Hurlingham"},
    4510: {"address": "Tambo Nuevo 800", "locality": "Hurlingham", "neighborhood": "Villa Alemania"},
    2987: {"address": "Rossini 2000", "locality": "Hurlingham"},
    1068: {"address": "GRANADA 500"},
    845: {"address": "Bonorino 634", "locality": "Villa Tesei", "neighborhood": "Santos Tesei"},
    2792: {"address": "NECOCHEA 1300", "locality": "Hurlingham", "neighborhood": UNIFIED_HURLINGHAM_CENTRO_ZONE},
    698: {"address": "José de Andonaegui 2600", "locality": "William C. Morris"},
    4409: {"address": "Las Araucarias 1900", "locality": "Hurlingham", "neighborhood": "Los Troncos"},
    4548: {"address": "Juan Díaz de Solís 1686", "locality": "William C. Morris"},
    4571: {"address": "Eduardo Acevedo 329", "locality": "William C. Morris", "neighborhood": "Villa Club"},
    1559: {"address": "Maestra Catalina G. de Pizzagalli 700", "locality": "Villa Tesei"},
    4514: {"address": "Rolland 1200", "locality": "Hurlingham"},
    1207: {"address": "Maestra A. González de Hecht 1200", "locality": "Villa Tesei", "neighborhood": "Santos Tesei"},
    1154: {"address": "Carhué 391", "locality": "Villa Tesei", "neighborhood": "Santos Tesei"},
    1140: {"address": "Einstein 100", "locality": "Villa Tesei"},
    4401: {"address": "Diego de Carvajal 600", "locality": "Hurlingham", "neighborhood": "Parque Quirno"},
    4393: {"address": "Waksman 404", "locality": "Villa Tesei", "neighborhood": "Barrio Italia"},
    1093: {"address": "José Batlle y Ordoñez esquina Lima", "locality": "Villa Tesei", "neighborhood": "Santos Tesei"},
    1086: {"address": "Ginebra esquina Atuel", "locality": "Hurlingham"},
    1085: {"address": "Cañuelas esquina Dolores de Huici", "locality": "William C. Morris"},
    5542: {"address": "Doctor Delfor Díaz 1700", "locality": "Hurlingham"},
    713: {"address": "José Garibaldi 3000", "locality": "William C. Morris"},
    720: {"address": "Dip. Hector Finochietto 1700", "locality": "Hurlingham"},
    37: {"address": "El Maestro Argentino 1800", "locality": "William C. Morris"},
    163: {"address": "Dip. Hector Finochietto 2000", "locality": "Hurlingham", "neighborhood": "Parque Johnston"},
    719: {"address": "Gral. Simón Bolívar 1700", "locality": "Hurlingham", "neighborhood": "Parque Johnston"},
    1400: {"address": "Dip. Hector Finochietto 2000", "locality": "Hurlingham", "neighborhood": "Parque Johnston"},
    3758: {"address": "El Maestro Argentino 1900", "locality": "William C. Morris"},
    979: {"address": "Isabel del Maestro 3500", "locality": "William C. Morris"},
    1037: {"address": "Adrián de Rosario Luna 800", "locality": "Hurlingham"},
    4507: {"address": "Argerich esquina Marqués de Avilés", "locality": "Hurlingham"},
    5693: {"address": "Vasco Núñez de Balboa 379", "locality": "Villa Tesei"},
    5692: {"address": "Gral. Pedro Díaz 2400", "locality": "William C. Morris"},
    5680: {"address": "Félix Frías 2500", "locality": "Hurlingham"},
    5679: {"address": "Valentín Alsina 2400", "locality": "Hurlingham"},
    5678: {"address": "Gral. Martín Güemes 1000", "locality": "Hurlingham"},
    5677: {"address": "Tte. Gral. Julio Argentino Roca 1940", "locality": "Hurlingham"},
    5674: {"address": "Tte. Gral. Julio Argentino Roca 1276", "locality": "Hurlingham"},
    5643: {"address": "Gral. Francisco Miranda 1700", "locality": "Hurlingham"},
    5630: {"address": "Tte. Gral. Pablo Ricchieri 1400", "locality": "Hurlingham"},
    5623: {"address": "Conscripto Bernardi 1900", "locality": "Hurlingham"},
    5616: {"address": "Tte. Gral. Julio Argentino Roca 2700", "locality": "William C. Morris"},
    5613: {"address": "Nilda Figueira 1400", "locality": "Hurlingham"},
    5611: {"address": "Tte. Gral. Julio Argentino Roca 1686", "locality": "Hurlingham"},
    5566: {"address": "Manuel A. Ocampo 1900", "locality": "Hurlingham"},
    5563: {"address": "Gral. Bernardo O'Higgins 1918", "locality": "Hurlingham"},
    5561: {"address": "Diego de Carvajal 800", "locality": "Hurlingham", "neighborhood": "Parque Quirno"},
    5558: {"address": "Maestra A. González de Hecht 1100", "locality": "Villa Tesei", "neighborhood": "Santos Tesei"},
    5544: {"address": "Pablo Pizzurno 441", "locality": "Hurlingham"},
    5543: {"address": "José Garibaldi 2600", "locality": "William C. Morris"},
    5540: {"address": "Av. Gdor. Vergara 3604", "locality": "Hurlingham"},
    5539: {"address": "Gral. Martín Güemes 1668", "locality": "Hurlingham"},
    5537: {"address": "Eva Perón 2200 esquina Guevara", "locality": "Hurlingham"},
}


STREET_METADATA_RULES = (
    ("maestra a gonzalez de hecht", {"locality": "Villa Tesei", "neighborhood": "Santos Tesei"}),
    ("maestra catalina g de pizzagalli", {"locality": "Villa Tesei"}),
    ("pizzagalli", {"locality": "Villa Tesei", "neighborhood": "Santos Tesei"}),
    ("carhue", {"locality": "Villa Tesei", "neighborhood": "Santos Tesei"}),
    ("einstein", {"locality": "Villa Tesei"}),
    ("waksman", {"locality": "Villa Tesei", "neighborhood": "Barrio Italia"}),
    ("diego de carvajal", {"locality": "Hurlingham", "neighborhood": "Parque Quirno"}),
    ("rolland", {"locality": "Hurlingham"}),
    ("jose batlle y ordonez", {"locality": "Villa Tesei", "neighborhood": "Santos Tesei"}),
    ("canuelas esquina dolores de huici", {"locality": "William C. Morris"}),
    ("ginebra esquina atuel", {"locality": "Hurlingham"}),
    ("doctor delfor diaz", {"locality": "Hurlingham"}),
    ("jose garibaldi", {"locality": "William C. Morris"}),
    ("gral jose garibaldi", {"locality": "William C. Morris"}),
    ("isabel del maestro", {"locality": "William C. Morris"}),
    ("el maestro argentino", {"locality": "William C. Morris"}),
    ("maestro argentino", {"locality": "William C. Morris"}),
    ("maestra argentino", {"locality": "William C. Morris"}),
    ("dip hector finochietto", {"locality": "Hurlingham"}),
    ("finochietto", {"locality": "Hurlingham"}),
    ("adrian de rosario luna", {"locality": "Hurlingham"}),
    ("argerich esquina marques de aviles", {"locality": "Hurlingham"}),
    ("vasco nunez de balboa", {"locality": "Villa Tesei"}),
)


def metadata_for_address(address):
    normalized = normalize_address(address)
    for prefix, metadata in STREET_METADATA_RULES:
        if normalized.startswith(prefix):
            return metadata
    return {}


class Command(BaseCommand):
    help = "Limpia direcciones existentes que contienen metadata pegada."

    def add_arguments(self, parser):
        parser.add_argument("--source")
        parser.add_argument("--property-id", action="append", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--geocode", action="store_true")

    def handle(self, *args, **options):
        queryset = Property.objects.all()
        if options["source"]:
            queryset = queryset.filter(listings__source__slug=options["source"]).distinct()
        if options["property_id"]:
            queryset = queryset.filter(pk__in=options["property_id"])
        changed = 0
        changed_ids = []
        for property_obj in queryset.order_by("id"):
            cleaned = clean_detected_address(property_obj.address)
            detected_cleaned = clean_detected_address(property_obj.detected_address)
            embedded_neighborhood = extract_embedded_neighborhood(
                property_obj.address or property_obj.detected_address
            )
            updates = {}
            known = KNOWN_ADDRESS_CORRECTIONS.get(property_obj.pk, {})
            if cleaned and cleaned != property_obj.address:
                updates["address"] = cleaned
                updates["normalized_address"] = normalize_address(cleaned)
            elif cleaned and normalize_address(cleaned) != property_obj.normalized_address:
                updates["normalized_address"] = normalize_address(cleaned)
            if detected_cleaned and detected_cleaned != property_obj.detected_address:
                updates["detected_address"] = detected_cleaned
            if known.get("address"):
                cleaned_known = str(known["address"]).strip()
                normalized_known = normalize_address(cleaned_known)
                if updates.get("address", property_obj.address) != cleaned_known:
                    updates["address"] = cleaned_known
                if updates.get("detected_address", property_obj.detected_address) != cleaned_known:
                    updates["detected_address"] = cleaned_known
                if updates.get("normalized_address", property_obj.normalized_address) != normalized_known:
                    updates["normalized_address"] = normalized_known
            if known.get("locality"):
                if updates.get("locality", property_obj.locality) != known["locality"]:
                    updates["locality"] = known["locality"]
                if property_obj.detected_locality and updates.get("detected_locality", property_obj.detected_locality) != known["locality"]:
                    updates["detected_locality"] = known["locality"]
            if known.get("neighborhood"):
                if updates.get("neighborhood", property_obj.neighborhood) != known["neighborhood"]:
                    updates["neighborhood"] = known["neighborhood"]
                if property_obj.detected_neighborhood and updates.get("detected_neighborhood", property_obj.detected_neighborhood) != known["neighborhood"]:
                    updates["detected_neighborhood"] = known["neighborhood"]
            elif embedded_neighborhood and property_obj.neighborhood in {"", "Hurlingham"}:
                updates["neighborhood"] = embedded_neighborhood
                if property_obj.detected_neighborhood in {"", "Hurlingham"}:
                    updates["detected_neighborhood"] = embedded_neighborhood
            inferred_metadata = metadata_for_address(updates.get("address") or cleaned or property_obj.address)
            if inferred_metadata.get("locality") and not known.get("locality"):
                current_locality = updates.get("locality", property_obj.locality)
                if current_locality in {"", "Hurlingham"} or inferred_metadata["locality"] != "Hurlingham":
                    updates["locality"] = inferred_metadata["locality"]
                    if property_obj.detected_locality:
                        updates["detected_locality"] = inferred_metadata["locality"]
            if inferred_metadata.get("neighborhood") and not known.get("neighborhood"):
                current_neighborhood = updates.get("neighborhood", property_obj.neighborhood)
                if current_neighborhood in {"", "Hurlingham"}:
                    updates["neighborhood"] = inferred_metadata["neighborhood"]
                    if property_obj.detected_neighborhood in {"", "Hurlingham"}:
                        updates["detected_neighborhood"] = inferred_metadata["neighborhood"]
            if not updates:
                continue
            changed += 1
            changed_ids.append(property_obj.pk)
            self.stdout.write(f"id={property_obj.pk} {updates}")
            if not options["dry_run"]:
                old_address = property_obj.address or property_obj.detected_address or ""
                address_changed = "address" in updates or "detected_address" in updates
                for field, value in updates.items():
                    setattr(property_obj, field, value)
                property_obj.save(update_fields=list(updates))
                location = getattr(property_obj, "location", None)
                new_address = property_obj.address or property_obj.detected_address or ""
                if (
                    address_changed
                    and location
                    and not location.manually_corrected
                    and not same_geocoding_target(old_address, new_address)
                ):
                    location.delete()
        if options["geocode"] and changed_ids and not options["dry_run"]:
            geocoder = Geocoder()
            located = 0
            no_result = 0
            errors = 0
            for property_obj in Property.objects.filter(pk__in=changed_ids).order_by("id"):
                try:
                    if geocoder.geocode_property(property_obj, force=True):
                        located += 1
                    else:
                        no_result += 1
                except Exception as exc:
                    errors += 1
                    self.stderr.write(f"{property_obj.pk}: {exc}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"{located} propiedades geolocalizadas; {no_result} sin resultado; {errors} errores."
                )
            )
        suffix = " (dry-run)" if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{changed} direcciones corregidas{suffix}"))
