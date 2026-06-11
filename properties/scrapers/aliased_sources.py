from .base import SourceDefinition


AUDIT_ONLY_SOURCES = [
    SourceDefinition(
        "aurellana",
        "Aurellana Desarrollos Inmobiliarios",
        "https://www.aurellana.com.ar",
        "https://www.aurellana.com.ar",
        enabled=False,
        notes="Oferta tambien sindicada en Mapaprop.",
    ),
    SourceDefinition(
        "beaudroit",
        "Santiago Beaudroit Propiedades",
        "https://beaudroitpropiedades.com.ar",
        "https://beaudroitpropiedades.com.ar",
        enabled=False,
        notes="La home no expone fichas de forma directa; requiere exploracion especifica.",
    ),
    SourceDefinition(
        "buscainmueble",
        "Buscainmueble",
        "https://www.buscainmueble.com",
        "https://www.buscainmueble.com/casas/venta/hurlingham",
        enabled=False,
        notes="Replica catalogo de Argenprop; excluido para evitar duplicados.",
    ),
]
