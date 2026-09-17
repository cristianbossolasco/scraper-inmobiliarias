from django.contrib.auth import views as auth_views
from django.urls import path

from properties import drive_history_views, drive_notebook_views, drive_views


urlpatterns = [
    path("", drive_views.mobile_home, name="mobile-home"),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            redirect_authenticated_user=True,
        ),
        name="mobile-login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(next_page="/accounts/login/"),
        name="mobile-logout",
    ),
    path("recorrido/", drive_views.drive_mode, name="drive-mode"),
    path("api/recorrido/memoria/", drive_notebook_views.drive_memory, name="drive-memory"),
    path("api/recorrido/notas/", drive_notebook_views.notebook_collection, name="drive-notebook"),
    path("api/recorrido/notas/<uuid:pk>/", drive_notebook_views.notebook_detail, name="drive-note"),
    path("api/recorrido/notas/<uuid:pk>/foto/", drive_notebook_views.notebook_photo, name="drive-note-photo"),
    path(
        "api/recorrido/historial/",
        drive_history_views.drive_history_collection,
        name="drive-history",
    ),
    path(
        "api/recorrido/historial/<uuid:pk>/",
        drive_history_views.drive_history_detail,
        name="drive-history-detail",
    ),
    path(
        "api/recorrido/cercanas/",
        drive_views.nearby_drive_properties_api,
        name="drive-nearby",
    ),
    path(
        "api/recorrido/propiedad/<int:pk>/ficha/",
        drive_views.drive_property_card_api,
        name="drive-property-card",
    ),
    path(
        "api/recorrido/propiedad/<int:pk>/favorito/",
        drive_views.drive_favorite_api,
        name="drive-favorite",
    ),
    path("salud/", drive_views.mobile_health, name="mobile-health"),
]
