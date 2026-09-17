"""Start an isolated, disposable Radar server for the browser regression check."""

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
database_path = Path(sys.argv[1]).resolve()
artifact_dir = (ROOT / "tmp" / "drive-block4").resolve()
if database_path.parent != artifact_dir or database_path.exists():
    raise SystemExit("Use a new SQLite file directly under tmp/drive-block4.")
artifact_dir.mkdir(parents=True, exist_ok=True)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.mobile_settings"
os.environ["DJANGO_MOBILE_STRICT"] = "0"

from config import mobile_settings  # noqa: E402

mobile_settings.DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": database_path}
}
mobile_settings.DEBUG = True
mobile_settings.STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402
from properties.models import Listing, Property, PropertyLocation, Source  # noqa: E402

call_command("migrate", interactive=False, verbosity=0)
get_user_model().objects.create_user("browser-test", password="Browser-test-only-42!")
source = Source.objects.create(
    slug="browser-test", name="Avisos de prueba", base_url="https://example.com"
)
property_obj = Property.objects.create(
    fingerprint="browser-drive-test",
    title="Casa de prueba navegador",
    address="Calle de prueba 123",
    property_type=Property.Type.HOUSE,
    operation="sale",
    status=Property.Status.ACTIVE,
    currency="USD",
    price=125000,
    bedrooms=3,
    covered_area=140,
    land_area=300,
    location_confidence=Property.LocationConfidence.HIGH,
)
PropertyLocation.objects.create(
    property=property_obj,
    latitude=-34.5900,
    longitude=-58.6400,
    precision=PropertyLocation.Precision.EXACT,
    provider="source-map",
    confidence=0.9,
)
Listing.objects.create(
    source=source,
    property=property_obj,
    external_id="browser-drive-test",
    url="https://example.com/casa-de-prueba",
    active=True,
)
for suffix, latitude in [("far", -34.5878), ("wide", -34.5792)]:
    extra = Property.objects.create(
        fingerprint=f"browser-{suffix}", title=f"Casa {suffix}", address=f"Calle {suffix} 250",
        property_type=Property.Type.HOUSE, operation="sale", status=Property.Status.ACTIVE,
        currency="USD", price=140000, bedrooms=3, location_confidence=Property.LocationConfidence.HIGH,
    )
    PropertyLocation.objects.create(property=extra, latitude=latitude, longitude=-58.64,
        precision=PropertyLocation.Precision.EXACT, provider="source-map", confidence=0.9)
    Listing.objects.create(source=source, property=extra, external_id=f"browser-{suffix}",
        url=f"https://example.com/{suffix}", active=True)
print("BROWSER_TEST_DATABASE_READY", flush=True)
call_command("runserver", f"127.0.0.1:{sys.argv[2]}", use_reloader=False, verbosity=0)
