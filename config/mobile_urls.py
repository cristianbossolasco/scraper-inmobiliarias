from django.contrib.auth import views as auth_views
from django.urls import path

from properties import drive_views


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
    path(
        "api/recorrido/cercanas/",
        drive_views.nearby_drive_properties_api,
        name="drive-nearby",
    ),
    path(
        "api/recorrido/propiedad/<int:pk>/favorito/",
        drive_views.drive_favorite_api,
        name="drive-favorite",
    ),
    path("salud/", drive_views.mobile_health, name="mobile-health"),
]
