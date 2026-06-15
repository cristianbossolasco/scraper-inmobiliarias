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


def is_unified_hurlingham_centro_alias(value):
    return zone_text_key(value) in HURLINGHAM_CENTRO_ALIAS_KEYS


def canonicalize_unified_zone_name(value):
    if is_unified_hurlingham_centro_alias(value):
        return UNIFIED_HURLINGHAM_CENTRO_ZONE
    return str(value or "").strip()
