"""Small spherical-earth geodesy helpers. Accurate enough for a COP (errors well under 0.5%)."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088
KM_PER_NM = 1.852
MS_PER_KT = 0.514444


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing: float, dist_km: float) -> tuple[float, float]:
    """Point reached travelling `dist_km` along `bearing` from (lat, lon)."""
    d = dist_km / EARTH_RADIUS_KM
    b = math.radians(bearing)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    lon2 = (math.degrees(l2) + 540.0) % 360.0 - 180.0
    return math.degrees(p2), lon2


def turn_toward(current: float, target: float, max_step: float) -> float:
    """Rotate `current` heading toward `target` by at most `max_step` degrees (shortest way)."""
    diff = (target - current + 540.0) % 360.0 - 180.0
    if abs(diff) <= max_step:
        return target % 360.0
    return (current + math.copysign(max_step, diff)) % 360.0


def point_in_polygon(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """Ray-casting test. `ring` is a GeoJSON ring: [[lon, lat], ...]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def geometry_centroid(geometry: dict) -> tuple[float, float]:
    """Rough centroid (mean of vertices) of a GeoJSON Point/Polygon/MultiPolygon. Returns (lat, lon)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    pts: list[list[float]] = []
    if gtype == "Point":
        return coords[1], coords[0]
    if gtype == "Polygon":
        pts = coords[0] if coords else []
    elif gtype == "MultiPolygon":
        for poly in coords:
            if poly:
                pts.extend(poly[0])
    elif gtype == "LineString":
        pts = coords
    if not pts:
        return 0.0, 0.0
    lat = sum(p[1] for p in pts) / len(pts)
    lon = sum(p[0] for p in pts) / len(pts)
    return lat, lon
