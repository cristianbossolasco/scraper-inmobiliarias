from django.contrib import admin

from .models import (
    Agency,
    Listing,
    Property,
    PropertyLocation,
    ScrapeJob,
    ScrapeJobListing,
    ScrapeJobSource,
    ScrapeRun,
    Source,
)


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "property_type",
        "price",
        "currency",
        "locality",
        "detected_locality",
        "location_confidence",
        "is_favorite",
        "is_hidden",
        "status",
    )
    search_fields = ("title", "address", "detected_address", "description", "personal_notes")
    list_filter = (
        "status",
        "property_type",
        "currency",
        "locality",
        "detected_locality",
        "location_confidence",
        "is_favorite",
        "is_hidden",
    )


admin.site.register(Agency)
admin.site.register(Source)
admin.site.register(Listing)
admin.site.register(PropertyLocation)
admin.site.register(ScrapeRun)
admin.site.register(ScrapeJob)
admin.site.register(ScrapeJobSource)
admin.site.register(ScrapeJobListing)
