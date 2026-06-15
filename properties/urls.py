from django.urls import path

from . import views


app_name = "properties"

urlpatterns = [
    path("", views.search, name="search"),
    path("estadisticas/", views.market_stats, name="stats"),
    path("estadisticas/data/<str:panel>/", views.stats_data_api, name="stats_data"),
    path("territorio/", views.territory_map, name="territory"),
    path("scraping/", views.scraping_dashboard, name="scraping"),
    path("propiedad/<int:pk>/", views.detail, name="detail"),
    path("export/properties.csv", views.export_properties_csv, name="export_csv"),
    path("export/properties.xlsx", views.export_properties_xlsx, name="export_xlsx"),
    path("api/propiedades/", views.properties_geojson, name="geojson"),
    path("api/propiedad/<int:pk>/resumen/", views.property_summary_api, name="property_summary"),
    path("api/propiedad/<int:pk>/estado/", views.update_property_state, name="update_property_state"),
    path("api/propiedad/<int:pk>/nota/", views.update_property_note, name="update_property_note"),
    path("api/propiedad/<int:pk>/datos/", views.update_property_data, name="update_property_data"),
    path("api/propiedad/<int:pk>/ubicacion/", views.update_location, name="update_location"),
    path("api/configuracion-mapa/", views.map_config, name="map_config"),
    path("api/jerarquia-geografica/capas/", views.geo_hierarchy_layers_api, name="geo_hierarchy_layers"),
    path("api/seguridad/capas/", views.security_layers_api, name="security_layers"),
    path("api/crimen/capas/", views.crime_layers_api, name="crime_layers"),
    path(
        "api/inteligencia-territorial/capas/",
        views.location_intelligence_layers_api,
        name="location_intelligence_layers",
    ),
    path("api/scraping/jobs/", views.create_scrape_job_api, name="scrape_job_create"),
    path("api/scraping/jobs/<int:pk>/", views.scrape_job_status_api, name="scrape_job_status"),
    path(
        "api/scraping/jobs/<int:pk>/cancel/",
        views.cancel_scrape_job_api,
        name="scrape_job_cancel",
    ),
    path(
        "api/scraping/jobs/<int:pk>/retry/",
        views.retry_scrape_job_api,
        name="scrape_job_retry",
    ),
    path(
        "api/scraping/jobs/<int:pk>/retry-errors/",
        views.retry_scrape_job_errors_api,
        name="scrape_job_retry_errors",
    ),
    path("api/operations/catalog/", views.operation_catalog_api, name="operation_catalog"),
    path("api/operations/jobs/", views.create_operation_job_api, name="operation_job_create"),
    path("api/operations/jobs/<int:pk>/", views.operation_job_status_api, name="operation_job_status"),
    path(
        "api/operations/jobs/<int:pk>/cancel/",
        views.cancel_operation_job_api,
        name="operation_job_cancel",
    ),
    path(
        "api/operations/jobs/<int:pk>/retry/",
        views.retry_operation_job_api,
        name="operation_job_retry",
    ),
    path(
        "api/operations/jobs/<int:pk>/apply-from-dry-run/",
        views.apply_operation_dry_run_api,
        name="operation_job_apply_from_dry_run",
    ),
]
