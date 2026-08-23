import os

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F403


ROOT_URLCONF = "config.mobile_urls"
WSGI_APPLICATION = "config.mobile_wsgi.application"
DEBUG = False

mobile_strict = os.environ.get("DJANGO_MOBILE_STRICT", "1") == "1"
mobile_secret = os.environ.get("DJANGO_SECRET_KEY", "")
if mobile_strict and not mobile_secret:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY es obligatorio para iniciar el Radar movil."
    )
if mobile_secret:
    SECRET_KEY = mobile_secret

mobile_host = os.environ.get("DJANGO_MOBILE_HOST", "").strip()
if mobile_strict and not mobile_host:
    raise ImproperlyConfigured(
        "DJANGO_MOBILE_HOST es obligatorio para iniciar el Radar movil."
    )

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
CSRF_TRUSTED_ORIGINS = []
if mobile_host:
    ALLOWED_HOSTS.append(mobile_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{mobile_host}")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = mobile_strict
SESSION_COOKIE_SECURE = mobile_strict
CSRF_COOKIE_SECURE = mobile_strict
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(os.environ.get("DJANGO_MOBILE_SESSION_AGE", "86400"))
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/recorrido/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

STATIC_ROOT = BASE_DIR / "staticfiles"  # noqa: F405

MIDDLEWARE = list(MIDDLEWARE)  # noqa: F405
security_index = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
MIDDLEWARE.insert(security_index + 1, "whitenoise.middleware.WhiteNoiseMiddleware")

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

