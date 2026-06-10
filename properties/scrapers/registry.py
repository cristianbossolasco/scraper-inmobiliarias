import inspect

from .aliased_sources import AUDIT_ONLY_SOURCES
from .argenprop import ArgenpropScraper
from .argencasas import ArgencasasScraper
from .local_sites import AliagaScraper, BecerraScraper
from .local_wordpress import MiglieriniScraper, OdriozolaScraper
from .mapaprop import MapapropScraper
from .mercadoprop import MercadoPropScraper
from .pending_sources import (
    AnaliaFernandezScraper,
    Century21Scraper,
    FincasScraper,
    GABienesScraper,
    GuarnieriScraper,
    InmueblesClarinScraper,
    LopezCombaScraper,
    MarceloRussoScraper,
    MercadoLibreScraper,
    PatagonPropScraper,
    PaulaFossatiScraper,
    RemaxDataworkScraper,
    RiquelmeScraper,
    ZonapropScraper,
)


ADAPTERS = {
    adapter.definition.slug: adapter
    for adapter in (
        MapapropScraper,
        ArgencasasScraper,
        ArgenpropScraper,
        MercadoPropScraper,
        BecerraScraper,
        AliagaScraper,
        MiglieriniScraper,
        OdriozolaScraper,
        AnaliaFernandezScraper,
        MarceloRussoScraper,
        LopezCombaScraper,
        RiquelmeScraper,
        FincasScraper,
        GuarnieriScraper,
        InmueblesClarinScraper,
        PatagonPropScraper,
        GABienesScraper,
        PaulaFossatiScraper,
        RemaxDataworkScraper,
        Century21Scraper,
        MercadoLibreScraper,
        ZonapropScraper,
    )
}


def get_adapter(slug, **kwargs):
    try:
        adapter_class = ADAPTERS[slug]
    except KeyError as exc:
        raise ValueError(f"Fuente desconocida: {slug}") from exc
    signature = inspect.signature(adapter_class)
    if not any(parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in signature.parameters
        }
    return adapter_class(**kwargs)


def get_adapter_classes(enabled_only=False):
    adapters = ADAPTERS.values()
    if enabled_only:
        adapters = [
            adapter for adapter in adapters if adapter.definition.enabled
        ]
    return list(adapters)


def source_definitions():
    definitions = [adapter.definition for adapter in get_adapter_classes()]
    return definitions + AUDIT_ONLY_SOURCES
