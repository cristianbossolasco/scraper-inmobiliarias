from django.db import connection


def sync_property_fts(property_obj):
    content = " ".join(
        filter(
            None,
            [
                property_obj.title,
                property_obj.description,
                property_obj.address,
                property_obj.locality,
                property_obj.neighborhood,
                " ".join(property_obj.features or []),
            ],
        )
    )
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM property_fts WHERE rowid = %s", [property_obj.pk])
        cursor.execute(
            "INSERT INTO property_fts(rowid, content) VALUES (%s, %s)",
            [property_obj.pk, content],
        )


def remove_property_fts(property_id):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM property_fts WHERE rowid = %s", [property_id])


def sync_location_rtree(location):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM property_location_rtree WHERE id = %s", [location.property_id])
        cursor.execute(
            """
            INSERT INTO property_location_rtree
                (id, min_lat, max_lat, min_lng, max_lng)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                location.property_id,
                location.latitude,
                location.latitude,
                location.longitude,
                location.longitude,
            ],
        )


def remove_location_rtree(property_id):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM property_location_rtree WHERE id = %s", [property_id])
