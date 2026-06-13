# Location Intelligence Recommendations

Generated at: 2026-06-13T02:11:01.209890+00:00

## Useful Maps

- Price per m2 against `overall_location_value_score`.
- Official transport access by zone: AMBA bus routes plus SUBE charge points.
- Official education and health access by zone.
- ADA Reconquista flood-risk polygons and zone-level `flood_penalty_score`.
- RENABAP overlap/proximity as urban informality and infrastructure-deficit context.
- Amenities, green access, walkability proxy, and externalities.
- Parcel-size distribution by zone from ARBA cadastre.

## Useful KPIs

- Median USD/m2 by zone compared with Hurlingham median.
- Listing count by zone and publication age.
- `overall_location_value_score` versus median USD/m2.
- `transport_access_score`, `education_access_score`, `health_access_score`.
- `flood_penalty_score`, `urban_informality_score`, `environmental_penalty_score`.
- `security_infrastructure_score` beside `crime_spatial_precision`.
- `parcel_area_m2_median`, `parcels_per_km2`, and `census_tract_count`.

## Alerts

- Property priced below zone median with high location-value score.
- Property intersecting or near official ADA flood-risk polygons.
- High price but weak official transport, education, or health access.
- Property near RENABAP polygons; use as context, not as a value judgement.
- High security infrastructure but municipal crime context remains high.
- Large parcel in high-access zone for possible development review.
- Property near railway, major road, industrial area, fuel station, or waterway.

## Data Notes

- Do not use municipal crime totals as neighborhood crime maps.
- Census 2022 PBA radios currently provide geometry and identifiers; population and households remain null until an official attribute table is found.
- RENABAP uses a public mirror because the primary official host failed DNS resolution in this environment; it is marked `medium_low`.
- Gas segment and ENRE historical outage layers remain unavailable or unconfirmed; utility score stays null.
- Official zoning/FOT/FOS remains unavailable as vector data.
