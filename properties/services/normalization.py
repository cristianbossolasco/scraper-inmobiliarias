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


def fold_text(value):
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in value if not unicodedata.combining(char)).lower()


def normalize_whitespace(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalize_address(value):
    text = fold_text(value)
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


def normalize_locality(value):
    folded = normalize_address(value)
    for candidate, canonical in LOCALITY_ALIASES.items():
        if candidate in folded:
            return canonical
    return normalize_whitespace(value).title()


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


def build_fingerprint(data):
    address = normalize_address(data.get("address"))
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
    folded = fold_text(address)
    if re.search(r"\bal\s+\d{2,5}\b", folded):
        return "street"
    if re.search(r"\b\d{2,5}\b", folded):
        return "exact"
    if re.search(r"\b(esq|esquina|y|entre)\b", folded):
        return "intersection"
    if normalize_whitespace(address):
        return "street"
    return "neighborhood"
