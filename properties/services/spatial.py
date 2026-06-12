import math

from django.db import connection


EARTH_RADIUS_KM = 6371.0088
EARTH_RADIUS_M = EARTH_RADIUS_KM * 1000


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
    if point_on_polygon_boundary(latitude, longitude, polygon):
        return True
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


def _project_m(latitude, longitude, origin_latitude):
    lat = math.radians(latitude)
    lng = math.radians(longitude)
    origin_lat = math.radians(origin_latitude)
    return (
        EARTH_RADIUS_M * lng * math.cos(origin_lat),
        EARTH_RADIUS_M * lat,
    )


def point_segment_distance_m(latitude, longitude, start, end):
    start_lng, start_lat = start
    end_lng, end_lat = end
    point_x, point_y = _project_m(latitude, longitude, latitude)
    start_x, start_y = _project_m(start_lat, start_lng, latitude)
    end_x, end_y = _project_m(end_lat, end_lng, latitude)
    segment_x = end_x - start_x
    segment_y = end_y - start_y
    segment_len_sq = segment_x * segment_x + segment_y * segment_y
    if not segment_len_sq:
        return math.hypot(point_x - start_x, point_y - start_y)
    t = max(
        0,
        min(
            1,
            ((point_x - start_x) * segment_x + (point_y - start_y) * segment_y)
            / segment_len_sq,
        ),
    )
    closest_x = start_x + t * segment_x
    closest_y = start_y + t * segment_y
    return math.hypot(point_x - closest_x, point_y - closest_y)


def point_on_polygon_boundary(latitude, longitude, polygon, tolerance_m=0.5):
    if len(polygon) < 2:
        return False
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if point_segment_distance_m(latitude, longitude, start, end) <= tolerance_m:
            return True
    return False


def point_to_polygon_distance_m(latitude, longitude, polygon):
    if point_in_polygon(latitude, longitude, polygon):
        return 0
    if len(polygon) < 2:
        return None
    return min(
        point_segment_distance_m(latitude, longitude, start, polygon[(index + 1) % len(polygon)])
        for index, start in enumerate(polygon)
    )


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
