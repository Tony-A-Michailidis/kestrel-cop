"""Tools the watch officer can call to look at the picture. The same functions serve Claude (via tool use)
and the mock officer, so a tool trace is real in both modes."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..airports import airport_by_code, nearest_airports
from ..geo import KM_PER_NM, bearing_deg, haversine_km
from ..models import Alert, Event, Track, utcnow
from ..store import Store

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "picture_summary",
        "description": "Overview of the current picture: area, DTG, counts by domain/affiliation, feed health, "
                       "number of events and alerts. Call this first.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_tracks",
        "description": "Find tracks (aircraft or vessels) matching filters. Returns up to `limit` compact tracks, "
                       "nearest first when `near` is given, otherwise flagged/fastest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": ["air", "sea"]},
                "near": {"type": "object", "description": "Centre point and radius",
                         "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "km": {"type": "number"}},
                         "required": ["lat", "lon", "km"]},
                "near_uid": {"type": "string", "description": "Alternative to `near`: centre the search on this track"},
                "flag": {"type": "string", "description": "EMERGENCY, DARK, MILITARY, or IN_ZONE:<zone name>"},
                "affiliation": {"type": "string", "enum": ["friend", "neutral", "unknown", "suspect", "hostile"]},
                "function": {"type": "string", "description": "e.g. fixed_wing, rotary, mil_fixed_wing, cargo, tanker, fishing, law_enforcement"},
                "min_speed_kts": {"type": "number"},
                "max_speed_kts": {"type": "number"},
                "min_alt_m": {"type": "number"},
                "max_alt_m": {"type": "number"},
                "text": {"type": "string", "description": "Case-insensitive substring of callsign/name or UID"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "get_track",
        "description": "Full detail for one track by UID or exact callsign, including its recent trail length and age.",
        "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}, "required": ["uid"]},
    },
    {
        "name": "list_alerts",
        "description": "Alerts raised by the rules engine, newest first.",
        "input_schema": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
    },
    {
        "name": "list_events",
        "description": "Non-track events on the picture: fire detections, earthquakes, weather alerts, zones. Cameras are "
                       "listed only when kind=camera; prefer find_cameras for cameras near something.",
        "input_schema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["fire", "quake", "alert", "zone", "camera"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
    },
    {
        "name": "find_cameras",
        "description": "Traffic/roadside cameras nearest to a track, event, place or point, with distance and bearing. "
                       "Then use view_camera on a result's uid to look at its current image.",
        "input_schema": {"type": "object", "properties": {
            "near_uid": {"type": "string", "description": "a track or event uid, or a callsign"},
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "radius_km": {"type": "number", "minimum": 1, "maximum": 500},
            "text": {"type": "string", "description": "match against the camera name, road or place"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25}}},
    },
    {
        "name": "find_airports",
        "description": "Airports nearest to a track, event, place or point (ICAO/IATA codes, names, size class, distance and "
                       "bearing), or look one up by code or name with `text`. Use it to say where an aircraft is likely going "
                       "or coming from, or to anchor a position to a known airport.",
        "input_schema": {"type": "object", "properties": {
            "near_uid": {"type": "string", "description": "a track or event uid, or a callsign"},
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "radius_km": {"type": "number", "minimum": 1, "maximum": 1000},
            "text": {"type": "string", "description": "an ICAO or IATA code, or part of the airport or city name"},
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["large", "medium", "small"]}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
    },
    {
        "name": "view_camera",
        "description": "Fetch the current still image from a camera on the picture and look at it. Describe what is "
                       "visible: traffic, weather, visibility, light, incidents. The frame may be a few minutes old.",
        "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}, "required": ["uid"]},
    },
    {
        "name": "measure",
        "description": "Distance (km and NM) and true bearing from one track/event UID to another UID or to a lat/lon.",
        "input_schema": {"type": "object", "properties": {
            "from_uid": {"type": "string"}, "to_uid": {"type": "string"},
            "to_lat": {"type": "number"}, "to_lon": {"type": "number"}}, "required": ["from_uid"]},
    },
    {
        "name": "feed_health",
        "description": "Status of every data feed: mode (live/demo/off), status, message rate, last message age, errors.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def compact_track(t: Track, now=None) -> dict[str, Any]:
    now = now or utcnow()
    d: dict[str, Any] = {
        "uid": t.uid, "callsign": t.label, "domain": t.domain, "function": t.function, "affiliation": t.affiliation,
        "lat": round(t.lat, 4), "lon": round(t.lon, 4),
        "alt_m": round(t.alt_m) if t.alt_m is not None else None,
        "speed_kts": round(t.speed_kts, 1) if t.speed_kts is not None else None,
        "course_deg": round(t.course_deg) if t.course_deg is not None else None,
        "age_s": round(t.age_seconds(now)), "flags": t.flags,
    }
    for key in ("squawk", "category", "aircraft_type", "registration", "mmsi", "ship_type", "nav_status", "destination"):
        if key in t.attrs:
            d[key] = t.attrs[key]
    return d


def compact_event(e: Event, now=None) -> dict[str, Any]:
    now = now or utcnow()
    d: dict[str, Any] = {
        "uid": e.uid, "kind": e.kind, "title": e.title, "lat": round(e.lat, 4), "lon": round(e.lon, 4),
        "severity": e.severity, "magnitude": e.magnitude, "age_min": round((now - e.ts).total_seconds() / 60),
        "expires_in_min": round((e.expires - now).total_seconds() / 60) if e.expires else None,
    }
    for key in ("headline", "area", "frp", "depth_km", "regional", "radius_km", "confidence"):
        if key in e.attrs:
            d[key] = e.attrs[key]
    return d


def compact_camera(e: Event, distance_km: float | None = None, bearing: float | None = None) -> dict[str, Any]:
    a = e.attrs
    d: dict[str, Any] = {"uid": e.uid, "name": e.title, "lat": round(e.lat, 4), "lon": round(e.lon, 4), "source": e.source}
    for key in ("road", "direction", "place", "owner", "refresh_seconds"):
        if key in a:
            d[key] = a[key]
    if a.get("stream_url"):
        d["has_live_stream"] = True
    if distance_km is not None:
        d["distance_km"] = round(distance_km, 1)
        d["distance_nm"] = round(distance_km / KM_PER_NM, 1)
        d["bearing_deg"] = round(bearing or 0)
    return d


def compact_alert(a: Alert, now=None) -> dict[str, Any]:
    now = now or utcnow()
    return {
        "id": a.id, "severity": a.severity, "rule": a.rule, "title": a.title, "text": a.text,
        "subject_uid": a.subject_uid, "age_min": round((now - a.ts).total_seconds() / 60),
        "triaged": bool(a.triage),
    }


class PictureTools:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}

    def summarize(self, name: str, result: dict[str, Any]) -> str:
        if "error" in result:
            return result["error"]
        if "airports" in result:
            return f"{len(result['airports'])} airport(s) within {result.get('radius_km')} km"
        if "cameras" in result:
            return f"{len(result['cameras'])} of {result.get('matched', 0)} camera(s) within {result.get('radius_km')} km"
        if "image_url" in result:
            return "camera located"
        if "counts" in result:
            c = result["counts"]
            return f"{c['by_domain'].get('air', 0)} air / {c['by_domain'].get('sea', 0)} sea / {c['events']} events / {c['alerts']} alerts"
        if "tracks" in result:
            return f"{len(result['tracks'])} track(s)"
        if "alerts" in result:
            return f"{len(result['alerts'])} alert(s)"
        if "events" in result:
            return f"{len(result['events'])} event(s)"
        if "distance_km" in result:
            return f"{result['distance_km']} km, bearing {result['bearing_deg']}°"
        if "feeds" in result:
            return f"{len(result['feeds'])} feed(s)"
        if "track" in result:
            return result["track"].get("callsign", "track")
        return "ok"

    # ---- tools --------------------------------------------------------------------------------
    def tool_picture_summary(self) -> dict[str, Any]:
        now = utcnow()
        return {
            "dtg": now.strftime("%d%H%MZ %b %y").upper(),
            "utc": now.isoformat(timespec="seconds"),
            "area": {"name": self.settings.area.name, "center": list(self.settings.area.center),
                     "radius_nm": self.settings.area.radius_nm},
            "counts": self.store.counts(),
            "cameras_on_picture": sum(1 for e in self.store.events.values() if e.kind == "camera"),
            "feeds": [{"name": f.name, "mode": f.mode, "status": f.status, "items_last_poll": f.items,
                       "rate_per_min": round(f.rate_per_min, 1),
                       "last_message_age_s": round((now - f.last_message).total_seconds()) if f.last_message else None,
                       "error": f.error} for f in self.store.feeds.values()],
        }

    def tool_search_tracks(self, domain: str | None = None, near: dict | None = None, near_uid: str | None = None,
                           flag: str | None = None, affiliation: str | None = None, function: str | None = None,
                           min_speed_kts: float | None = None, max_speed_kts: float | None = None,
                           min_alt_m: float | None = None, max_alt_m: float | None = None,
                           text: str | None = None, limit: int = 20) -> dict[str, Any]:
        now = utcnow()
        centre = None
        exclude_uid = None
        if near_uid:
            ref = self._find(near_uid)
            if ref is None:
                return {"error": f"no track or event {near_uid}", "tracks": []}
            exclude_uid = ref.uid
            centre = (ref.lat, ref.lon, float((near or {}).get("km", 50)))
        elif near:
            centre = (float(near["lat"]), float(near["lon"]), float(near["km"]))
        rows: list[tuple[float, Track]] = []
        for t in self.store.tracks.values():
            if exclude_uid and t.uid == exclude_uid:
                continue
            if domain and t.domain != domain:
                continue
            if flag and not any(f.upper().startswith(flag.upper()) for f in t.flags):
                continue
            if affiliation and t.affiliation != affiliation:
                continue
            if function and t.function != function:
                continue
            if min_speed_kts is not None and (t.speed_kts or 0) < min_speed_kts:
                continue
            if max_speed_kts is not None and (t.speed_kts or 0) > max_speed_kts:
                continue
            if min_alt_m is not None and (t.alt_m or 0) < min_alt_m:
                continue
            if max_alt_m is not None and (t.alt_m or 0) > max_alt_m:
                continue
            if text and text.lower() not in (t.label.lower() + " " + t.uid.lower()):
                continue
            dist = haversine_km(centre[0], centre[1], t.lat, t.lon) if centre else 0.0
            if centre and dist > centre[2]:
                continue
            rows.append((dist, t))
        if centre:
            rows.sort(key=lambda r: r[0])
        else:
            rows.sort(key=lambda r: (-len(r[1].flags), -(r[1].speed_kts or 0)))
        out = []
        for dist, t in rows[: max(1, min(int(limit), 50))]:
            c = compact_track(t, now)
            if centre:
                c["distance_km"] = round(dist, 1)
                c["bearing_from_centre_deg"] = round(bearing_deg(centre[0], centre[1], t.lat, t.lon))
            out.append(c)
        return {"tracks": out, "total_matching": len(rows)}

    def _find(self, uid: str) -> Track | Event | None:
        t = self.store.tracks.get(uid)
        if t:
            return t
        for cand in self.store.tracks.values():
            if cand.label.lower() == uid.lower():
                return cand
        return self.store.events.get(uid)

    def tool_get_track(self, uid: str) -> dict[str, Any]:
        t = self._find(uid)
        if t is None or not isinstance(t, Track):
            return {"error": f"no track {uid}"}
        c = compact_track(t)
        c["attrs"] = t.attrs
        c["first_seen"] = t.first_seen.isoformat(timespec="seconds")
        c["last_report"] = t.ts.isoformat(timespec="seconds")
        c["trail_points"] = len(t.trail)
        c["source"] = t.source
        c["sidc"] = t.sidc
        c["cot_type"] = t.cot_type
        return {"track": c}

    def tool_list_alerts(self, severity: str | None = None, limit: int = 15) -> dict[str, Any]:
        rows = [a for a in reversed(self.store.alerts) if not severity or a.severity == severity]
        return {"alerts": [compact_alert(a) for a in rows[: max(1, min(int(limit), 50))]], "total": len(rows)}

    def tool_list_events(self, kind: str | None = None, limit: int = 20) -> dict[str, Any]:
        rows = [e for e in self.store.events.values() if (e.kind == kind if kind else e.kind != "camera")]
        rows.sort(key=lambda e: e.ts, reverse=True)
        return {"events": [compact_event(e) for e in rows[: max(1, min(int(limit), 50))]], "total": len(rows)}

    # ---- cameras ----------------------------------------------------------------------------
    def tool_find_cameras(self, near_uid: str | None = None, lat: float | None = None, lon: float | None = None,
                          radius_km: float | None = None, text: str | None = None, limit: int = 8) -> dict[str, Any]:
        if near_uid:
            ref = self._find(near_uid)
            if ref is None:
                return {"error": f"no track or event {near_uid}", "cameras": []}
            centre = (ref.lat, ref.lon)
        elif lat is not None and lon is not None:
            centre = (float(lat), float(lon))
        else:
            centre = (float(self.settings.area.center[0]), float(self.settings.area.center[1]))
        reach = float(radius_km) if radius_km else 50.0
        needle = (text or "").strip().lower()
        rows: list[tuple[float, Event]] = []
        total = 0
        for e in self.store.events.values():
            if e.kind != "camera":
                continue
            total += 1
            if needle and needle not in f"{e.title} {e.attrs.get('road', '')} {e.attrs.get('place', '')}".lower():
                continue
            d = haversine_km(centre[0], centre[1], e.lat, e.lon)
            if d <= reach:
                rows.append((d, e))
        rows.sort(key=lambda r: r[0])
        return {
            "centre": {"lat": round(centre[0], 4), "lon": round(centre[1], 4)}, "radius_km": reach,
            "cameras": [compact_camera(e, d, bearing_deg(centre[0], centre[1], e.lat, e.lon))
                        for d, e in rows[: max(1, min(int(limit), 25))]],
            "matched": len(rows), "cameras_on_picture": total,
        }

    def tool_find_airports(self, near_uid: str | None = None, lat: float | None = None, lon: float | None = None,
                           radius_km: float | None = None, text: str | None = None, kinds: list[str] | None = None,
                           limit: int = 5) -> dict[str, Any]:
        """Nearest airports from the vendored OurAirports layer; a code or name in `text` searches instead."""
        if text and not near_uid and lat is None:
            a = airport_by_code(text)
            if a:
                lat, lon = a["lat"], a["lon"]
                return {"centre": {"lat": a["lat"], "lon": a["lon"]}, "radius_km": 0,
                        "airports": nearest_airports(lat, lon, 1.0, 1)}
        if near_uid:
            ref = self._find(near_uid)
            if ref is None:
                return {"error": f"no track or event {near_uid}", "airports": []}
            centre = (ref.lat, ref.lon)
        elif lat is not None and lon is not None:
            centre = (float(lat), float(lon))
        else:
            centre = (float(self.settings.area.center[0]), float(self.settings.area.center[1]))
        reach = float(radius_km) if radius_km else 150.0
        wanted = tuple(k for k in (kinds or ["large", "medium", "small"]) if k in ("large", "medium", "small")) or ("large", "medium", "small")
        rows = nearest_airports(centre[0], centre[1], reach, max(1, min(int(limit), 20)), wanted, text)
        return {"centre": {"lat": round(centre[0], 4), "lon": round(centre[1], 4)}, "radius_km": reach, "airports": rows}

    def tool_view_camera(self, uid: str) -> dict[str, Any]:
        """Validates the camera and returns its metadata; the watch officer fetches the image itself (async)."""
        e = self.store.events.get(uid)
        if e is None or e.kind != "camera":
            return {"error": f"no camera {uid}; use find_cameras to get camera uids"}
        return {"camera": compact_camera(e), "image_url": e.attrs.get("image_url")}

    def tool_measure(self, from_uid: str, to_uid: str | None = None, to_lat: float | None = None,
                     to_lon: float | None = None) -> dict[str, Any]:
        a = self._find(from_uid)
        if a is None:
            return {"error": f"unknown {from_uid}"}
        if to_uid:
            b = self._find(to_uid)
            if b is None:
                return {"error": f"unknown {to_uid}"}
            lat2, lon2, label = b.lat, b.lon, getattr(b, "label", getattr(b, "title", to_uid))
        elif to_lat is not None and to_lon is not None:
            lat2, lon2, label = float(to_lat), float(to_lon), f"{to_lat:.3f}, {to_lon:.3f}"
        else:
            return {"error": "give to_uid or to_lat/to_lon"}
        km = haversine_km(a.lat, a.lon, lat2, lon2)
        return {"from": from_uid, "to": label, "distance_km": round(km, 1), "distance_nm": round(km / KM_PER_NM, 1),
                "bearing_deg": round(bearing_deg(a.lat, a.lon, lat2, lon2))}

    def tool_feed_health(self) -> dict[str, Any]:
        now = utcnow()
        return {"feeds": [{
            "name": f.name, "label": f.label, "mode": f.mode, "status": f.status, "items_last_poll": f.items,
            "messages_total": f.messages_total, "rate_per_min": round(f.rate_per_min, 1),
            "last_message_age_s": round((now - f.last_message).total_seconds()) if f.last_message else None,
            "error": f.error, "detail": f.detail,
        } for f in self.store.feeds.values()]}
