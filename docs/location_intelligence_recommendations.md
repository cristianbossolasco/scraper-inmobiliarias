# Location Intelligence Recommendations

Generated at: 2026-06-13T00:57:25.261787+00:00

## Useful Maps

- Price per m2 against `overall_location_value_score`.
- Transport access by zone with train/bus proximity.
- Security infrastructure score with municipal crime context shown separately.
- Flood and waterway proximity proxy.
- Amenities, green access, and walkability proxy.
- Externality proximity: rail, major roads, waterways, fuel and industrial areas.
- Parcel-size distribution by zone for development or subdivision context.

## Useful KPIs

- Median USD/m2 by zone compared with Hurlingham median.
- Listing count by zone and publication age.
- `overall_location_value_score` versus median USD/m2.
- `transport_access_score`, `amenity_density_score`, `green_access_score`.
- `security_infrastructure_score` beside `crime_spatial_precision`.
- `parcel_area_m2_median` and `parcels_per_km2`.

## Alerts

- Property priced below zone median with high location-value score.
- Property near waterways or high flood-proxy zone.
- High price but weak transport or amenity access.
- High security infrastructure but municipal crime context remains high.
- Large parcel in high-access zone for possible development review.
- Property near railway, major road, industrial area, fuel station, or waterway.

## Data Notes

- Do not use municipal crime totals as neighborhood crime maps.
- Treat OSM as medium-confidence operational context, not a complete official inventory.
- Keep missing official layers as null rather than inventing zoning, flood, utility, or census attributes.
