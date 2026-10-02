"""Cursor-on-Target XML encoding. One function per Kestrel type; all produce bytes ready for the wire.

CoT reference: the ATAK/TAK "CoT 2.0" event schema (event/point/detail). Speeds on the wire are m/s,
altitudes are metres HAE. Unknown accuracy is signalled with the conventional 9999999.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from ..geo import MS_PER_KT
from ..models import Alert, Event, Track, utcnow
from .sidc import EVENT_COT_TYPES

UNKNOWN_ACCURACY = "9999999"


def _argb(a: int, r: int, g: int, b: int) -> int:
    """ATAK colours are signed 32-bit ARGB integers (what Android's Color class produces)."""
    value = (a << 24) | (r << 16) | (g << 8) | b
    return value - (1 << 32) if value >= (1 << 31) else value


def _shape_colors(event: Event) -> tuple[int, int]:
    """(stroke, fill) for a drawn area, by kind and severity: zones cyan, severe weather red, else amber."""
    sev = (event.severity or "").lower()
    if event.kind == "zone":
        r, g, b = 57, 198, 255
    elif sev in ("extreme", "severe", "high"):
        r, g, b = 255, 77, 77
    elif sev == "moderate":
        r, g, b = 255, 176, 0
    else:
        r, g, b = 255, 230, 128
    return _argb(255, r, g, b), _argb(48, r, g, b)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _event_element(uid: str, cot_type: str, how: str, time: datetime, stale_after: int,
                   lat: float, lon: float, hae: float | None) -> tuple[ET.Element, ET.Element]:
    ev = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": cot_type,
        "how": how,
        "time": _iso(time),
        "start": _iso(time),
        "stale": _iso(time + timedelta(seconds=stale_after)),
    })
    ET.SubElement(ev, "point", {
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "hae": f"{hae:.1f}" if hae is not None else UNKNOWN_ACCURACY,
        "ce": UNKNOWN_ACCURACY,
        "le": UNKNOWN_ACCURACY,
    })
    detail = ET.SubElement(ev, "detail")
    return ev, detail


def track_to_cot(track: Track, stale_seconds: int = 120, now: datetime | None = None) -> bytes:
    ev, detail = _event_element(track.uid, track.cot_type, "m-g", track.ts, stale_seconds,
                                track.lat, track.lon, track.alt_m)
    ET.SubElement(detail, "contact", {"callsign": track.label})
    track_attrs: dict[str, str] = {}
    if track.course_deg is not None:
        track_attrs["course"] = f"{track.course_deg:.1f}"
    if track.speed_kts is not None:
        track_attrs["speed"] = f"{track.speed_kts * MS_PER_KT:.2f}"
    if track_attrs:
        ET.SubElement(detail, "track", track_attrs)
    remarks = [f"source={track.source}", f"function={track.function}", f"affiliation={track.affiliation}"]
    for key in ("squawk", "category", "registration", "aircraft_type", "mmsi", "ship_type", "nav_status", "destination"):
        if key in track.attrs and track.attrs[key] not in (None, ""):
            remarks.append(f"{key}={track.attrs[key]}")
    if track.flags:
        remarks.append("flags=" + ",".join(track.flags))
    ET.SubElement(detail, "remarks").text = " ".join(remarks)
    ET.SubElement(detail, "__kestrel", {
        "source": track.source,
        "sidc": track.sidc,
        "domain": track.domain,
        "flags": ",".join(track.flags),
    })
    return ET.tostring(ev, encoding="utf-8", xml_declaration=False)


def event_to_cot(event: Event, now: datetime | None = None) -> bytes:
    now = now or utcnow()
    stale = int((event.expires - now).total_seconds()) if event.expires else 6 * 3600
    stale = max(60, stale)
    cot_type = event.cot_type or EVENT_COT_TYPES.get(event.kind, "b-m-p-s-m")
    ev, detail = _event_element(event.uid, cot_type, "h-e", event.ts, stale, event.lat, event.lon, None)
    ET.SubElement(detail, "contact", {"callsign": event.title[:60]})
    bits = [f"kind={event.kind}", f"source={event.source}"]
    if event.severity:
        bits.append(f"severity={event.severity}")
    if event.magnitude is not None:
        bits.append(f"magnitude={event.magnitude}")
    for key, value in event.attrs.items():
        if isinstance(value, (str, int, float)) and len(str(value)) < 120:
            bits.append(f"{key}={value}")
    ET.SubElement(detail, "remarks").text = " ".join(bits)
    if event.geometry and event.geometry.get("type") in ("Polygon", "MultiPolygon"):
        rings = [event.geometry["coordinates"][0]] if event.geometry["type"] == "Polygon" \
            else [poly[0] for poly in event.geometry["coordinates"] if poly]
        ring = max(rings, key=len)[:400]  # ATAK draws one closed shape per event; keep the largest ring
        # ATAK's freeform-shape convention: one <link point="lat,lon,hae"/> per vertex plus stroke/fill
        for lon, lat in ring:
            ET.SubElement(detail, "link", {"point": f"{lat:.6f},{lon:.6f},0"})
        stroke, fill = _shape_colors(event)
        ET.SubElement(detail, "strokeColor", {"value": str(stroke)})
        ET.SubElement(detail, "strokeWeight", {"value": "2.0"})
        ET.SubElement(detail, "fillColor", {"value": str(fill)})
        # the classic CoT shape schema as well, for consumers that read it (FreeTAKServer, GoATAK)
        shape = ET.SubElement(detail, "shape")
        poly = ET.SubElement(shape, "polyline", {"closed": "true"})
        for lon, lat in ring:
            ET.SubElement(poly, "vertex", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"})
    if event.kind == "zone" and event.attrs.get("radius_km"):
        shape = ET.SubElement(detail, "shape")
        ellipse = ET.SubElement(shape, "ellipse", {
            "major": f"{float(event.attrs['radius_km']) * 1000:.0f}",
            "minor": f"{float(event.attrs['radius_km']) * 1000:.0f}",
            "angle": "0",
        })
        ellipse.tail = None
    ET.SubElement(detail, "__kestrel", {"kind": event.kind, "source": event.source})
    return ET.tostring(ev, encoding="utf-8", xml_declaration=False)


def alert_to_cot(alert: Alert, stale_seconds: int = 1800) -> bytes:
    lat = alert.lat if alert.lat is not None else 0.0
    lon = alert.lon if alert.lon is not None else 0.0
    ev, detail = _event_element(f"kestrel-{alert.id}", "b-m-p-s-m", "h-e", alert.ts, stale_seconds, lat, lon, None)
    ET.SubElement(detail, "contact", {"callsign": f"ALERT {alert.severity}: {alert.title}"[:80]})
    text = alert.text
    if alert.triage:
        text += "\n\nSPOTREP: " + alert.triage
    ET.SubElement(detail, "remarks").text = text
    ET.SubElement(detail, "__kestrel", {
        "alert_id": alert.id, "rule": alert.rule, "severity": alert.severity,
        "subject": alert.subject_uid or "",
    })
    return ET.tostring(ev, encoding="utf-8", xml_declaration=False)
