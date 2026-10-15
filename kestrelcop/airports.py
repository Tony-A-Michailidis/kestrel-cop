"""Airports for the watch officer: the same vendored OurAirports layer the console draws, searched by distance.

The file is small (large and medium airports plus small ones with scheduled service), public domain, and
loaded once on first use. Nothing here touches the network."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .geo import KM_PER_NM, bearing_deg, haversine_km

AIRPORTS_FILE = Path(__file__).parent / "web" / "static" / "basemap" / "airports.json"


@lru_cache(maxsize=1)
def load_airports(path: Path = AIRPORTS_FILE) -> list[dict[str, Any]]:
    """[{icao, iata?, name, kind, muni?, cc?, elev_ft?, lat, lon}, ...], largest first."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for f in data.get("features", []):
        p = dict(f.get("properties") or {})
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or not p.get("icao"):
            continue
        p["lat"], p["lon"] = float(coords[1]), float(coords[0])
        out.append(p)
    return out


def nearest_airports(lat: float, lon: float, radius_km: float = 100.0, limit: int = 5,
                     kinds: tuple[str, ...] = ("large", "medium", "small"), text: str | None = None) -> list[dict[str, Any]]:
    """Airports within radius_km of a point, nearest first, each with distance and bearing from the point."""
    needle = (text or "").strip().lower()
    rows: list[tuple[float, dict[str, Any]]] = []
    for a in load_airports():
        if a["kind"] not in kinds:
            continue
        if needle and needle not in f"{a.get('icao', '')} {a.get('iata', '')} {a.get('name', '')} {a.get('muni', '')}".lower():
            continue
        d = haversine_km(lat, lon, a["lat"], a["lon"])
        if d <= radius_km:
            rows.append((d, a))
    rows.sort(key=lambda r: r[0])
    out = []
    for d, a in rows[: max(1, limit)]:
        row = {k: a[k] for k in ("icao", "iata", "name", "kind", "muni", "cc", "elev_ft") if k in a}
        row.update({"lat": round(a["lat"], 4), "lon": round(a["lon"], 4), "distance_km": round(d, 1),
                    "distance_nm": round(d / KM_PER_NM, 1), "bearing_deg": round(bearing_deg(lat, lon, a["lat"], a["lon"]))})
        out.append(row)
    return out


def airport_by_code(code: str) -> dict[str, Any] | None:
    code = code.strip().upper()
    for a in load_airports():
        if a.get("icao") == code or a.get("iata") == code:
            return a
    return None
