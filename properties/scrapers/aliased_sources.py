from .base import SourceDefinition


AUDIT_ONLY_SOURCES = [
    SourceDefinition(
        "fincas",
        "Fincas Bienes Raices",
        "https://www.fincasbienesraices.com.ar",
        "https://www.haurie.argencasas.com",
        enabled=False,
        notes="Su catalogo deriva a Argencasas y queda cubierto por ese adaptador.",
    ),
    SourceDefinition(
        "riquelme",
        "Riquelme Propiedades",
        "https://www.riquelmepropiedades.com.ar",
        "https://www.riquelmepropiedades.com.ar",
        enabled=False,
        notes="Oferta tambien sindicada en Mapaprop.",
    ),
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
