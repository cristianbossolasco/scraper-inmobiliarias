import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from properties.models import Property


LOCALITY_ALIASES = {
    "hurlingham": "Hurlingham",
    "villa tesei": "Villa Tesei",
    "tesei": "Villa Tesei",
    "william c morris": "William C. Morris",
    "william morris": "William C. Morris",
    "morris": "William C. Morris",
}

VALID_LOCALITIES = tuple(sorted(set(LOCALITY_ALIASES.values())))

NEIGHBORHOOD_LOCALITY_MAP = {
    "Villa Tesei": "Villa Tesei",
    "Villa Tesei Centro": "Villa Tesei",
    "Santos Tesei": "Villa Tesei",
    "Barrio Italia": "Villa Tesei",
    "William C. Morris": "William C. Morris",
}

GEOCODING_LOCALITIES = {
    "hurlingham",
    "villa tesei",
    "william c morris",
    "william morris",
    "morris",
}

ADMIN_ADDRESS_PARTS = {
    "argentina",
    "buenos aires",
    "provincia de buenos aires",
    "partido de hurlingham",
}

POSTAL_CODE_PART_RE = re.compile(r"^(?:b)?\d{4}[a-z]{0,4}$", re.I)

TYPE_KEYWORDS = (
    ("duplex", Property.Type.DUPLEX),
    ("dúplex", Property.Type.DUPLEX),
    ("departamento", Property.Type.APARTMENT),
    ("depto", Property.Type.APARTMENT),
    ("terreno", Property.Type.LAND),
    ("lote", Property.Type.LAND),
    ("quinta", Property.Type.COUNTRY_HOUSE),
    ("ph", Property.Type.PH),
    ("casa", Property.Type.HOUSE),
    ("chalet", Property.Type.HOUSE),
)

GENERIC_ADDRESS_PARTS = (
    "argentina",
    "buenos aires",
    "provincia de buenos aires",
    "partido de hurlingham",
    "hurlingham",
    "villa tesei",
    "villa santos tesei",
    "william c morris",
    "william morris",
    "morris",
    "villa alemania",
    "villa club",
    "parque johnston",
    "parque quirno",
    "barrio ingles",
    "hurlingham centro",
    "km 18",
)

ADDRESS_NOISE_PATTERNS = (
    r"^\s*ciudad\s*:",
    r"\bcontacto\s+buscador\b",
    r"\btipo\s+de\s+propiedad\b",
    r"\botras\s+operaciones\b",
    r"\bmapa\s+de\s+sitio\b",
    r"\binicio\s+propiedades\b",
    r"\bclick\s+para\s+llamar\b",
    r"\bcurso\s+de\s+agente\b",
    r"\bdesarrollos\s+inmobiliarios\b",
)

NEIGHBORHOOD_NOISE_PATTERNS = (
    r"\bconsultanos\b",
    r"\bmartillero\b",
    r"\bcolegiado\b",
    r"\btodos\s+los\s+detalles\b",
    r"\bcategoria\s*:",
    r"\bestado\s*:",
    r"\bventa\s*:",
    r"\busd\b",
)

NEIGHBORHOOD_ALIASES = (
    ("Barrio Inglés", (r"\bbarrio\s+ingles\b", r"\bingles\b")),
    ("William C. Morris", (r"\bwilliam\s+c\.?\s*morris\b", r"\bwilliam\s+morris\b", r"^morris$")),
    ("Santos Tesei", (r"\bvilla\s+santos\s+tes", r"\bsantos\s+tesei\b")),
    ("5 esquinas", (r"\b5\s+esquinas\b",)),
    ("Barrio Cartero", (r"\bbarrio\s+cartero\b", r"^cartero$")),
    ("Los Troncos", (r"\bbarrio\s+los\s+troncos\b", r"\bb[°º]\.?\s*los\s+troncos\b", r"\blos\s+troncos\b")),
    ("Parque Johnston", (r"\bparque\s+(?:johnston|jhonston)\b", r"\b(?:johnston|jhonston)\b")),
    ("Parque Quirno", (r"\bparque\s+quirno\b",)),
    ("Villa Alemania", (r"\bvilla\s+alemania\b",)),
    ("Villa Club", (r"\bvilla\s+club\b",)),
    ("Villa Tesei Centro", (r"\bvilla\s+tesei\s+centro\b",)),
    ("Villa Tesei", (r"^villa\s+tesei$",)),
    ("Km 18", (r"\bkm\s*18\b",)),
    ("Zona Curupayti", (r"\bzona\s+curupayti\b", r"\bcurupayti\b", r"\bcurapayti\b")),
    ("Zona Iglesia", (r"\bzona\s+iglesia\b",)),
    ("Zona Municipalidad", (r"\bzona\s+municipalidad\b",)),
    ("Barrio Luna", (r"\bbarrio\s+luna\b", r"^luna$")),
    ("Barrio Destino", (r"\bbarrio\s+destino\b", r"^el\s+destino$")),
    ("Barrio Italia", (r"\bbarrio\s+italia\b",)),
    ("Hurlingham Centro", (r"\bhurlingham\s+centro\b",)),
    ("Hurlingham", (r"^hurlingham$",)),
)

KNOWN_NEIGHBORHOODS = tuple(canonical for canonical, _patterns in NEIGHBORHOOD_ALIASES)

ADDRESS_NEIGHBORHOOD_RULES = (
    ("Santos Tesei", (r"\bveragua\b",)),
    ("Villa Club", (r"\bvilla\s+club\b",)),
    ("Barrio Ingl\u00e9s", (r"\bbarrio\s+ingles\b", r"\bingles\b")),
    ("Parque Johnston", (r"\bparque\s+johnston\b", r"\bjohnston\b")),
    ("William C. Morris", (r"\bwilliam\s+c\.?\s*morris\b", r"\bwilliam\s+morris\b")),
    ("Los Troncos", (r"\bbarrio\s+los\s+troncos\b", r"\bb[°º]\.?\s*los\s+troncos\b", r"\blos\s+troncos\b")),
)


def fold_text(value):
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in value if not unicodedata.combining(char)).lower()


def normalize_whitespace(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalize_street_number_address(value):
    text = normalize_whitespace(value)
    if not text:
        return ""
    text = re.sub(r"\s*,?\s*\bpiso\s+(?:pb|bajo|\d+)\b\.?", "", text, flags=re.I)
    text = re.sub(r"\s*,?\s*\bp\.?\s*b\.?\b\.?", "", text, flags=re.I)
    text = re.sub(r"\s+\bal\s+(\d{1,2})[.,](\d{3})(?=\b)", r" \1\2", text, flags=re.I)
    text = re.sub(r"\s+\bal\s+(\d{2,5})(?=\b)", r" \1", text, flags=re.I)
    return normalize_whitespace(text)


STREET_ALIAS_PATTERNS = (
    (r"\bAcevedo\s+Eduardo\b", "Eduardo Acevedo"),
    (r"(?<!Eduardo\s)\bAcevedo\b(?!\s+Eduardo)", "Eduardo Acevedo"),
    (r"\bCarhue\b", "Carhué"),
    (r"\bAlberto\s+Einstein\b", "Einstein"),
    (r"\bDiego\s+de\s+Carbajal\b", "Diego de Carvajal"),
    (r"\bDiego\s+Carbajal\b", "Diego de Carvajal"),
    (r"(?<!Diego de )\bCarbajal\b", "Diego de Carvajal"),
    (r"\bGonzalez\s+De\s+Hecht\s+A\.?\s+Maestra\b", "Maestra A. González de Hecht"),
    (r"\bMaestra\s+A\.?\s+Gonzalez\s+De\s+Hecht\b", "Maestra A. González de Hecht"),
    (r"\bMaestra\s+Gonzalez\s+de\s+Hecht\b", "Maestra A. González de Hecht"),
    (r"\bMaestra\s+Hecht\b", "Maestra A. González de Hecht"),
    (r"\bJ\.?\s+Batlle\s+y\s+Ordo(?:ñ|n)ez\b", "José Batlle y Ordoñez"),
    (r"(?<!José\s)\bBatlle\s+y\s+Ordo(?:ñ|n)ez\b", "José Batlle y Ordoñez"),
    (r"\bJ\.?\s+De\s+Andonaegui\b", "José de Andonaegui"),
    (r"(?<!de\s)\bAndonaegui\b", "José de Andonaegui"),
    (r"\bGral\.?\s+Mariano\s+Necochea\b", "General Mariano Necochea"),
    (r"(?<!Esteban\s)\bBonorino\b", "Esteban Bonorino"),
    (r"(?<!Mariano\s)\bNecochea\b", "General Mariano Necochea"),
    (r"\bDip\.?\s+Hector\s+Finochietto\b", "Finochietto"),
    (r"\bDiputado\s+Hector\s+Finochietto\b", "Finochietto"),
    (r"\bDoctor\s+Finochietto\b", "Finochietto"),
    (r"\bDr\.?\s+Finochietto\b", "Finochietto"),
)


def _solis_alias_variant(value):
    folded = fold_text(value)
    match = re.match(r"\s*(?:juan\s+diaz\s+(?:de\s+)?)?solis\b", folded)
    if match:
        return normalize_whitespace(f"Juan Díaz de Solís{value[match.end():]}")
    return ""


def address_alias_variants(value):
    base = normalize_street_number_address(value)
    variants = []
    for pattern, replacement in STREET_ALIAS_PATTERNS:
        candidate = normalize_whitespace(re.sub(pattern, replacement, base, flags=re.I))
        if candidate and candidate != base and candidate not in variants:
            variants.append(candidate)
    solis_candidate = _solis_alias_variant(base)
    if solis_candidate and solis_candidate != base and solis_candidate not in variants:
        variants.append(solis_candidate)
    return variants


def canonical_address_alias(value):
    variants = address_alias_variants(value)
    return variants[0] if variants else normalize_street_number_address(value)


def canonical_geocoding_locality(*values):
    for value in values:
        normalized = normalize_locality(value or "")
        if normalize_address(normalized) in GEOCODING_LOCALITIES:
            return normalized
    return "Hurlingham"


def _without_accents_and_punctuation(value):
    text = normalize_address(value)
    text = re.sub(r"\bb?\d{4}[a-z]{0,4}\b", " ", text)
    return normalize_whitespace(text)


def extract_embedded_neighborhood(value):
    text = normalize_whitespace(value or "")
    if not text:
        return ""
    matches = re.findall(
        r"\b(?:barrio|b[°º]\.?)\s+([^,;/|]+)",
        text,
        flags=re.I,
    )
    for match in matches:
        neighborhood = normalize_neighborhood_name(match)
        if neighborhood:
            return neighborhood
    return ""


def clean_address_for_storage(value):
    text = normalize_street_number_address(value)
    if not text:
        return ""
    text = re.sub(
        r"(\b\d{2,5}\b)(?:\s*[-–,]?\s*hurlingham)?\s*[-–,]?\s+(?:barrio|b[°º]\.?)\s+[^,;/|]+",
        r"\1",
        text,
        flags=re.I,
    )
    parts = []
    for part in (normalize_whitespace(item.strip(" -–|.")) for item in text.split(",")):
        folded = fold_text(part)
        if not part:
            continue
        if POSTAL_CODE_PART_RE.fullmatch(folded):
            continue
        if folded in ADMIN_ADDRESS_PARTS or folded in GEOCODING_LOCALITIES:
            continue
        parts.append(part)
    if not parts:
        return ""
    first = parts[0]
    first = re.sub(r"\.?\s+\bentre\b.*$", "", first, flags=re.I)
    first = re.sub(r"\s+\b(?:hurlingham|villa\s+tesei|william\s+c\.?\s+morris|william\s+morris)\b\s*$", "", first, flags=re.I)
    first = canonical_address_alias(first)
    return normalize_whitespace(first.strip(" ,-"))


def is_plausible_property_address(value):
    text = normalize_street_number_address(value)
    if not text:
        return False
    if len(text) > 180:
        return False
    folded = fold_text(text)
    if any(re.search(pattern, folded, re.I) for pattern in ADDRESS_NOISE_PATTERNS):
        return False
    simplified = _without_accents_and_punctuation(text)
    for part in sorted(GENERIC_ADDRESS_PARTS, key=len, reverse=True):
        simplified = re.sub(rf"\b{re.escape(part)}\b", " ", simplified)
    simplified = re.sub(r"\b(?:cp|codigo\s+postal)\b", " ", simplified)
    simplified = normalize_whitespace(simplified.strip(" ,-"))
    return bool(re.search(r"[a-z]", simplified))


def normalize_address(value):
    text = fold_text(normalize_street_number_address(value))
    text = re.sub(r"[.,;#]", " ", text)
    replacements = {
        r"\bav(?:enida)?\b": "avenida",
        r"\bgdor\b": "gobernador",
        r"\bgral\b": "general",
        r"\bpte\b": "presidente",
        r"\bprov\.?\b": "provincia",
        r"\bbs\.?\s*as\.?\b": "buenos aires",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    return normalize_whitespace(text)


def normalize_neighborhood_name(value):
    text = normalize_whitespace(value or "").strip(" -–|,.")
    if not text:
        return ""
    if len(text) > 80:
        return ""
    folded = fold_text(text)
    if any(re.search(pattern, folded, re.I) for pattern in NEIGHBORHOOD_NOISE_PATTERNS):
        return ""
    first_part = normalize_whitespace(re.split(r",|/|\|", text, maxsplit=1)[0])
    folded_first = fold_text(first_part)
    for canonical, patterns in NEIGHBORHOOD_ALIASES:
        if any(re.search(pattern, folded_first, re.I) for pattern in patterns):
            return canonical
    for canonical, patterns in NEIGHBORHOOD_ALIASES:
        if any(re.search(pattern, folded, re.I) for pattern in patterns):
            return canonical
    return text


def infer_neighborhood_from_address(value):
    folded = fold_text(normalize_street_number_address(value or ""))
    if not folded:
        return ""
    for canonical, patterns in ADDRESS_NEIGHBORHOOD_RULES:
        if any(re.search(pattern, folded, re.I) for pattern in patterns):
            return canonical
    return ""


def known_neighborhood_name(value):
    neighborhood = normalize_neighborhood_name(value)
    return neighborhood if neighborhood in KNOWN_NEIGHBORHOODS else ""


def normalize_locality(value):
    folded = normalize_address(value)
    if not folded:
        return ""
    for candidate, canonical in sorted(LOCALITY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if candidate in folded:
            return canonical
    return ""


def locality_from_neighborhood(value):
    neighborhood = known_neighborhood_name(value)
    if not neighborhood:
        return ""
    return NEIGHBORHOOD_LOCALITY_MAP.get(neighborhood, "Hurlingham")


def parse_decimal(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    raw = re.sub(r"[^\d,.-]", "", str(value))
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[-1]
        raw = raw.replace(",", ".") if len(tail) <= 2 else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif "." in raw and len(raw.rsplit(".", 1)[-1]) == 3:
        raw = raw.replace(".", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_int(value):
    decimal = parse_decimal(value)
    return int(decimal) if decimal is not None else None


def normalize_currency(value):
    folded = fold_text(value)
    if (
        "usd" in folded
        or "u$s" in folded
        or "u$d" in folded
        or "us$" in folded
        or "u$s" in (value or "")
        or "u$d" in (value or "").lower()
    ):
        return "USD"
    if "ars" in folded or "$" in (value or ""):
        return "ARS"
    return normalize_whitespace(value).upper()[:8]


def infer_property_type(*values):
    text = fold_text(" ".join(filter(None, values)))
    matches = []
    for keyword, property_type in TYPE_KEYWORDS:
        position = text.find(fold_text(keyword))
        if position >= 0:
            matches.append((position, property_type))
    if matches:
        return min(matches, key=lambda item: item[0])[1]
    return Property.Type.OTHER


def build_fingerprint(data, source=None):
    raw_address = data.get("address")
    address = normalize_address(raw_address) if is_plausible_property_address(raw_address) else ""
    locality = normalize_locality(data.get("locality"))
    if address:
        identity = "|".join(
            [
                address,
                locality,
                str(data.get("property_type") or ""),
                str(data.get("covered_area") or data.get("total_area") or ""),
            ]
        )
    elif source and (data.get("external_id") or data.get("url")):
        identity = "|".join(
            [
                "listing",
                getattr(source, "slug", str(source)),
                str(data.get("external_id") or ""),
                str(data.get("url") or ""),
            ]
        )
    else:
        identity = "|".join(
            [
                normalize_whitespace(data.get("title")).lower(),
                locality,
                str(data.get("price") or ""),
                str(data.get("bedrooms") or ""),
            ]
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def classify_address_precision(address):
    folded = fold_text(normalize_street_number_address(address))
    if re.search(r"\b\d{2,5}\b", folded):
        return "exact"
    if re.search(r"\b(esq|esquina|y|entre)\b", folded):
        return "intersection"
    if normalize_whitespace(address):
        return "street"
    return "neighborhood"
