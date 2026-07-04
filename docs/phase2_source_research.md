# Phase 2 Source Research

Generated at: 2026-06-29T00:24:57.845820+00:00

## Confirmed Sources

- `census_2022_geojson_zip`: Datos Abiertos PBA / INDEC - Radios censales 2022 - https://catalogo.datos.gba.gob.ar/dataset/radios-censales
- `education_official_zip`: Datos Abiertos PBA - Establecimientos educativos - https://catalogo.datos.gba.gob.ar/dataset/establecimientos-educativos
- `health_official_2025_zip`: Datos Abiertos PBA - Establecimientos de salud publicos - https://catalogo.datos.gba.gob.ar/dataset/establecimientos-salud
- `transport_routes_kml`: Datos Transporte - Recorridos de servicios de colectivos AMBA - https://datos.transporte.gob.ar/dataset/recorridos-de-servicios-de-colectivos-amba
- `sube_points_geojson`: Datos Transporte / SUBE - Puntos de carga - https://datos.transporte.gob.ar/dataset/puntos-carga-sube
- `flood_reconquista_zip`: Autoridad del Agua PBA - Peligrosidad Cuenca Reconquista - https://ada.gba.gov.ar/cartas-de-riesgo-hidrico/
- `renabap_official_geojson`: RENABAP - Registro Nacional de Barrios Populares - https://datos.gob.ar/dataset/desarrollo-social-registro-nacional-barrios-populares
- `gas_segments_zip`: Secretaria de Energia - Usuarios de gas por segmento de calle - https://datos.gob.ar/dataset/energia-cantidad-usuarios-gas-natural-red-por-segmento-calle

## Unavailable Or Fallback Sources

- `gas_segments_zip`: HTTPError: 404 Client Error: Not Found for url: http://datos.energia.gob.ar/dataset/d850c7a4-e2cb-4a2e-9b15-666dd9e27398/resource/ae05ccf6-b486-44ea-89a1-1d315cdbd5bd/download/cantidad-de-usuarios-de-gas-de-red-por-segmento-de-calle.zip (http://datos.minem.gob.ar/dataset/d850c7a4-e2cb-4a2e-9b15-666dd9e27398/resource/ae05ccf6-b486-44ea-89a1-1d315cdbd5bd/download/cantidad-de-usuarios-de-gas-de-red-por-segmento-de-calle.zip)

## Notes

- Census 2022 PBA resource contains radio geometries and identifiers; population and households remain null until an official attribute table is found.
- RENABAP primary host failed DNS resolution in this environment; fallback mirror is marked in metadata.
- Gas official catalog exposes a resource URL, but the resource returned an HTTP error during this build.
- ENRE exposes current AMBA outage pages, but no stable reusable historical endpoint was confirmed.
