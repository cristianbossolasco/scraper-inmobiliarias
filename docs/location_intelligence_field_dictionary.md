# Diccionario de inteligencia territorial

Este documento describe los campos de `data/geo/integrated_location_value_zones_hurlingham.geojson` que consume la app en la fase 3.

## Score operativo

- `overall_location_value_score`: score territorial principal persistido como `overall_score`.
- `location_value_level`: nivel textual usado para filtros y badges.
- `zone_name`: zona territorial asociada.
- `data_confidence`, `generated_at`, `score_methodology`: evidencia y auditoria.

## Componentes

- `transport_access_score`: accesibilidad por transporte oficial/SUBE.
- `education_access_score`: cercania y densidad educativa.
- `health_access_score`: cercania y densidad sanitaria.
- `environmental_penalty_score`: penalidad ambiental agregada.
- `development_potential_score`: potencial territorial/catastral agregado.
- `urban_informality_score`: contexto urbano e infraestructura asociado a RENABAP.

## Riesgos y contexto

- `flood_penalty_score`, `in_flood_risk_zone`, `flood_risk_level`, `flood_risk_overlap_pct`: contexto hidrico de ADA.
- `nearest_renabap_m`, `inside_renabap`, `renabap_area_overlap_m2`, `renabap_families_nearby`: contexto urbano RENABAP. No debe presentarse como juicio de valor.
- `reported_crime_score` y campos de crimen municipal no se usan para score operativo de propiedad; se muestran separados.

## Distancias y conteos

- `nearest_sube_point_m`, `nearest_school_m`, `nearest_health_center_m`: distancias principales para ficha/preview.
- `schools_count`, `health_points_count`, `sube_points_count`, `official_bus_lines_count`: conteos resumidos por zona.
- `parcel_count`, `parcel_area_m2_median`, `parcels_per_km2`: contexto catastral por zona.

## Faltantes documentados

- Gas, ENRE historico, zoning/FOT/FOS y atributos censales poblacionales completos siguen como faltantes no bloqueantes.
