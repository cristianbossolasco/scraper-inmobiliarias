import re
import unicodedata


UNIFIED_HURLINGHAM_CENTRO_ZONE = "Hurlingham Centro (Barrio Ingles)"


def zone_text_key(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


UNIFIED_HURLINGHAM_CENTRO_ALIASES = (
    UNIFIED_HURLINGHAM_CENTRO_ZONE,
    "Hurlingham Centro",
    "Hurlingham Centro Barrio Ingles",
    "Barrio Ingles",
    "Barrio Inglés",
    "B Ingles",
    "B Inglés",
    "Ingles",
    "Inglés",
)

HURLINGHAM_CENTRO_ALIAS_KEYS = {
    zone_text_key(alias) for alias in UNIFIED_HURLINGHAM_CENTRO_ALIASES
}
ZONE_CANONICAL_ALIASES = {
    "Parque Johnston": ("Parque Johnston", "Parque Jhonston", "Johnston", "Jhonston"),
}
ZONE_CANONICAL_BY_KEY = {
    zone_text_key(alias): canonical
    for canonical, aliases in ZONE_CANONICAL_ALIASES.items()
    for alias in aliases
}


def is_unified_hurlingham_centro_alias(value):
    return zone_text_key(value) in HURLINGHAM_CENTRO_ALIAS_KEYS


def canonicalize_unified_zone_name(value):
    if is_unified_hurlingham_centro_alias(value):
        return UNIFIED_HURLINGHAM_CENTRO_ZONE
    text = str(value or "").strip()
    return ZONE_CANONICAL_BY_KEY.get(zone_text_key(text), text)
