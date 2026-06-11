from django.urls import path

from . import views


app_name = "properties"

urlpatterns = [
    path("", views.search, name="search"),
    path("estadisticas/", views.market_stats, name="stats"),
    path("scraping/", views.scraping_dashboard, name="scraping"),
    path("propiedad/<int:pk>/", views.detail, name="detail"),
    path("export/properties.csv", views.export_properties_csv, name="export_csv"),
    path("export/properties.xlsx", views.export_properties_xlsx, name="export_xlsx"),
    path("api/propiedades/", views.properties_geojson, name="geojson"),
    path("api/propiedad/<int:pk>/estado/", views.update_property_state, name="update_property_state"),
    path("api/propiedad/<int:pk>/nota/", views.update_property_note, name="update_property_note"),
    path("api/propiedad/<int:pk>/ubicacion/", views.update_location, name="update_location"),
    path("api/configuracion-mapa/", views.map_config, name="map_config"),
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
]
