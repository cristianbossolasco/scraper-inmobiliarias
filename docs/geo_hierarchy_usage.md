# Geo Hierarchy Usage

Generated at: 2026-06-15T02:04:23.824601+00:00

## Layers

- `data/geo/01_partido_hurlingham.geojson`: level 1, Partido de Hurlingham.
- `data/geo/02_localidades_hurlingham.geojson`: level 2, Hurlingham, Villa Tesei, William C. Morris.
- `data/geo/03_zonas_hurlingham_final.geojson`: level 3, 43 zonas with assigned locality.
- `data/geo/03b_microzonas_hurlingham_final.geojson`: compatibility layer, currently empty.
- `data/geo/04_gaps_zonas_hurlingham_final.geojson`: diagnostic gaps from partido minus final zones.
- `data/geo/zone_aliases_hurlingham.csv`: textual aliases for source labels.

## Inference Order For A Future Integration

1. Check that the point falls inside the partido.
2. Assign locality from `02_localidades_hurlingham.geojson`.
3. Assign zone from `03_zonas_hurlingham_final.geojson`.
4. Preserve the source text as `source_zone_raw` and compare aliases against inferred geography.

Barrio Ingles and Hurlingham Centro are a single zone: `Hurlingham Centro (Barrio Ingles)`.

Los Troncos is included as a manual operational zone in William C. Morris.
