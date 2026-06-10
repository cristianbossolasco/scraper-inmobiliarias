import html
import re


INVALID_MARKERS = (
    "ubicacion",
    "ubicación",
    "caracteristicas",
    "características",
    "ampliar mapa",
)

CUT_MARKERS = (
    r"\s+\d+[\d.,]*\s*m2\b",
    r"\s+\d+[\d.,]*\s*mÂ²\b",
    r"\s+Operation\s*:",
    r"\s+Address\s*:",
    r"\s+Location\s*:",
    r"\s+Code\s*:",
    r"\s+Price\s*:",
    r"\s+Venta\s+Address\s*:",
)


def normalize_agency_name(value):
    text = html.unescape(value or "")
    text = re.sub(r"\s+", " ", text).strip(" -–|·")
    if not text:
        return ""
    folded = text.lower()
    if any(marker in folded for marker in INVALID_MARKERS):
        return ""
    for marker in CUT_MARKERS:
        text = re.split(marker, text, maxsplit=1, flags=re.I)[0].strip(" -–|·")
    if len(text) > 90:
        return ""
    return text
