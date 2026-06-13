# Location Intelligence Sources

Generated at: 2026-06-13T02:11:01.209890+00:00

## Base And Existing Layers

- Zones: `data/geo/Zonas_Hurlingham_polygons.geojson`, generated from OpenStreetMap relations by the existing local pipeline.
- Security: municipal WP Google Maps markers, police-station seed, OSM zone polygons, and existing local scoring artifacts.
- Crime: SNIC/SAT/PBA municipal datasets. Crime remains municipality scope and low spatial precision; no neighborhood distribution is inferred.
- ARBA GeoARBA: cadastral parcels, blocks, cadastral hierarchy, and side-measure points from local raw archives and GeoARBA/WFS metadata.

## Phase 2 Official Sources

- Census 2022 radios: https://catalogo.datos.gba.gob.ar/dataset/radios-censales
- Education establishments: https://catalogo.datos.gba.gob.ar/dataset/establecimientos-educativos
- Public health establishments 2025: https://catalogo.datos.gba.gob.ar/dataset/establecimientos-salud
- AMBA bus routes: https://datos.transporte.gob.ar/dataset/recorridos-de-servicios-de-colectivos-amba
- SUBE charge points: https://datos.transporte.gob.ar/dataset/puntos-carga-sube
- ADA Reconquista flood-risk polygons: https://ada.gba.gov.ar/cartas-de-riesgo-hidrico/
- RENABAP: https://datos.gob.ar/dataset/desarrollo-social-registro-nacional-barrios-populares

## Fallbacks And Limitations

- RENABAP primary host failed in this environment; the public mirror was used and marked as `medium_low` confidence.
- Census 2022 PBA downloadable resources inspected here provide radio geometry and identifiers, not population or household totals.
- Gas segments are listed in the national catalog but the download URL returned HTTP 404 during this build.
- ENRE outage pages were researched, but no stable reusable historical CSV/GeoJSON endpoint was confirmed.
- Official zoning/FOT/FOS remains unavailable as vector data; keep zoning attributes null until a reliable source or manual digitization workflow is approved.
