import math

from django.db import connection


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(value))


def point_in_polygon(latitude, longitude, polygon):
    inside = False
    j = len(polygon) - 1
    for i, (lng_i, lat_i) in enumerate(polygon):
        lng_j, lat_j = polygon[j]
        intersects = ((lat_i > latitude) != (lat_j > latitude)) and (
            longitude
            < (lng_j - lng_i) * (latitude - lat_i) / ((lat_j - lat_i) or 1e-12)
            + lng_i
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def rtree_property_ids(south, west, north, east):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id FROM property_location_rtree
            WHERE max_lat >= %s AND min_lat <= %s
              AND max_lng >= %s AND min_lng <= %s
            """,
            [south, north, west, east],
        )
        return [row[0] for row in cursor.fetchall()]


def radius_bbox(latitude, longitude, radius_km):
    lat_delta = radius_km / 111.32
    lng_delta = radius_km / (111.32 * max(math.cos(math.radians(latitude)), 0.01))
    return (
        latitude - lat_delta,
        longitude - lng_delta,
        latitude + lat_delta,
        longitude + lng_delta,
    )
