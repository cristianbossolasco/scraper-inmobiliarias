import re

from properties.models import Property
from .normalization import (
    clean_address_for_storage,
    extract_embedded_neighborhood,
    fold_text,
    infer_neighborhood_from_address,
    is_plausible_property_address,
    normalize_locality,
    normalize_neighborhood_name,
    normalize_street_number_address,
    normalize_whitespace,
)


LOCALITY_PATTERNS = (
    ("William C. Morris", (r"\bwilliam\s+c\.?\s*morris\b", r"\bwilliam\s+morris\b")),
    ("Villa Tesei", (r"\bvilla\s+tesei\b", r"\bsantos\s+tesei\b", r"\btesei\b")),
    ("Hurlingham", (r"\bhurlingham\b",)),
)

NEIGHBORHOOD_PATTERNS = (
    ("Santos Tesei", (r"\bsantos\s+tesei\b",)),
    ("Parque Johnston", (r"\bparque\s+johnston\b", r"\bjohnston\b")),
    ("Barrio Inglés", (r"\bbarrio\s+ingles\b", r"\bingles\b")),
)

REFERENCE_PATTERNS = (
    "Vergara",
    "Pedro Diaz",
    "Jauretche",
    "Gorriti",
    "Av. Roca",
    "Roca",
    "Bustamante",
    "Camargo",
    "Kiernan",
    "Cuzco",
    "Malarredo",
    "Concepcion Arenal",
    "Gral. Pedro Diaz",
)

ADDRESS_RE = re.compile(
    r"\b((?:av\.?|avenida|calle|boulevard|bvd\.?|pasaje)?\s*"
    r"[a-zA-ZÁÉÍÓÚÜÑáéíóúüñ][a-zA-ZÁÉÍÓÚÜÑáéíóúüñ .'-]{2,}"
    r"(?:\s+\d{2,5}|\s+al\s+\d{2,5}|\s+y\s+[a-zA-ZÁÉÍÓÚÜÑáéíóúüñ][a-zA-ZÁÉÍÓÚÜÑáéíóúüñ .'-]{2,}))",
    re.I,
)

ADDRESS_STOP_PATTERN = re.compile(
    r"\b(?:Ubicaci(?:on|ón)|Agua\s+Corriente|Alumbrado|Cloaca|Pavimento|"
    r"INFORMACI(?:ON|ÓN)\s+B(?:ASICA|ÁSICA)|Ambientes?\s*:|Dormitorios?\s*:|"
    r"Ba(?:n|ñ)os?\s*:|Condici(?:on|ón)\s*:|Plantas?\s*:|Antig(?:u|ü)edad\s*:|"
    r"Situaci(?:on|ón)\s*:|Caracter(?:isticas|ísticas)|Descripci(?:on|ón)|Venta|"
    r"USD|U\$S|US\$|ARS|\$)",
    re.I,
)


def clean_detected_address(value):
    text = normalize_whitespace(value or "")
    if not text:
        return ""
    text = ADDRESS_STOP_PATTERN.split(text, maxsplit=1)[0]
    text = re.sub(r"^(?:Direccion|Direcci(?:on|ón)|Ubicaci(?:on|ón))\s*:?\s*", "", text, flags=re.I)
    text = clean_address_for_storage(text.strip(" -–|,")) or normalize_street_number_address(text.strip(" -–|,"))
    return text if is_plausible_property_address(text) else ""


def _find_named(patterns, text):
    folded = fold_text(text)
    for label, candidates in patterns:
        for pattern in candidates:
            if re.search(pattern, folded):
                return label
    return ""


def _find_all_named(patterns, text):
    folded = fold_text(text)
    found = []
    for label, candidates in patterns:
        if any(re.search(pattern, folded) for pattern in candidates):
            found.append(label)
    return found


def _first_address(text):
    for match in ADDRESS_RE.finditer(text or ""):
        value = clean_detected_address(match.group(1))
        if len(value) > 80:
            continue
        folded = fold_text(value)
        if any(word in folded for word in ("venta", "casa", "propiedad", "ambiente")):
            continue
        return value
    return ""


def _coordinates(data):
    try:
        lat = data.get("latitude")
        lng = data.get("longitude")
        if lat is not None and lng is not None:
            return float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None
    return None, None


def enrich_location_data(data):
    data = dict(data)
    raw = data.get("raw_data") if isinstance(data.get("raw_data"), dict) else {}
    text_parts = [
        data.get("title"),
        data.get("address"),
        data.get("locality"),
        data.get("neighborhood"),
        data.get("description"),
        raw.get("location_text"),
        raw.get("detail_text"),
    ]
    text = normalize_whitespace(" ".join(str(part) for part in text_parts if part))
    detected_localities = _find_all_named(LOCALITY_PATTERNS, text)
    locality = normalize_locality(data.get("locality") or (detected_localities[0] if detected_localities else ""))
    if locality and locality not in {"Hurlingham", "Villa Tesei", "William C. Morris"}:
        locality = _find_named(LOCALITY_PATTERNS, text) or locality
    neighborhood = normalize_neighborhood_name(
        data.get("neighborhood") or _find_named(NEIGHBORHOOD_PATTERNS, text)
    )
    embedded_neighborhood = extract_embedded_neighborhood(data.get("address") or raw.get("address") or "")
    if embedded_neighborhood and not neighborhood:
        neighborhood = embedded_neighborhood
    address = clean_detected_address(data.get("address") or raw.get("address")) or _first_address(text)
    address_neighborhood = infer_neighborhood_from_address(
        data.get("address") or address or raw.get("address")
    )
    if address_neighborhood and (not neighborhood or neighborhood in {"Hurlingham", "Hurlingham Centro"}):
        neighborhood = address_neighborhood
    lat, lng = _coordinates(data)

    folded_text = fold_text(text)
    references = [ref for ref in REFERENCE_PATTERNS if fold_text(ref) in folded_text]
    evidence = {
        "listing": {
            "address": data.get("address") or "",
            "locality": data.get("locality") or "",
            "neighborhood": data.get("neighborhood") or "",
        },
        "detected_references": references,
    }
    notes = []
    if data.get("locality") and locality and normalize_locality(data.get("locality")) != locality:
        notes.append(f"Contradiccion localidad listado/detalle: {data.get('locality')} / {locality}")
    if len(set(detected_localities)) > 1:
        notes.append(f"Contradiccion de zonas detectadas: {', '.join(detected_localities)}")
    if data.get("address") and address and clean_detected_address(data.get("address")) != address:
        notes.append(f"Contradiccion direccion listado/detalle: {data.get('address')} / {address}")

    if lat is not None and lng is not None:
        source = Property.LocationSource.MAP
        confidence = Property.LocationConfidence.HIGH
    elif address and locality:
        source = Property.LocationSource.DETAIL if not data.get("address") else Property.LocationSource.LISTING
        confidence = Property.LocationConfidence.HIGH if re.search(r"\d{2,5}", address) else Property.LocationConfidence.MEDIUM
    elif neighborhood or locality or references:
        source = Property.LocationSource.DESCRIPTION if data.get("description") else Property.LocationSource.INFERRED
        confidence = Property.LocationConfidence.MEDIUM if neighborhood or locality else Property.LocationConfidence.LOW
    else:
        source = Property.LocationSource.UNKNOWN
        confidence = Property.LocationConfidence.UNKNOWN

    data["detected_locality"] = locality if locality in {"Hurlingham", "Villa Tesei", "William C. Morris"} else ""
    data["detected_neighborhood"] = neighborhood or ""
    data["detected_address"] = address or ""
    data["detected_latitude"] = lat
    data["detected_longitude"] = lng
    data["location_source"] = source
    data["location_confidence"] = confidence
    data["location_notes"] = " | ".join(notes)
    data["location_evidence"] = evidence

    if not data.get("locality") and data["detected_locality"]:
        data["locality"] = data["detected_locality"]
    if data.get("neighborhood"):
        data["neighborhood"] = neighborhood
    elif neighborhood:
        data["neighborhood"] = neighborhood
    if not data.get("address") and address:
        data["address"] = address
    if lat is not None and lng is not None:
        data["latitude"] = lat
        data["longitude"] = lng
        data.setdefault("location_precision", "exact")
    return data
