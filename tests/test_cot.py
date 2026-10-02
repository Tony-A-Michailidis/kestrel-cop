"""Cursor-on-Target encoding and the 2525 / CoT code tables."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from kestrelcop.cot import air_codes, alert_to_cot, event_to_cot, sea_codes, track_to_cot
from kestrelcop.cot.sidc import adsb_function, ais_function
from kestrelcop.models import Alert, Event, Track

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_track(**overrides) -> Track:
    base = dict(uid="adsb-abc123", domain="air", source="adsb", callsign="UAL123", lat=36.95, lon=-76.30,
                alt_m=10000.0, speed_kts=450.0, course_deg=90.0, affiliation="neutral", function="fixed_wing",
                sidc="SNAPCF---------", cot_type="a-n-A-C-F", attrs={"squawk": "1200", "category": "A3"}, ts=NOW)
    base.update(overrides)
    return Track(**base)


def test_air_and_sea_codes_follow_the_standard():
    assert air_codes("friend", "mil_fixed_wing") == ("SFAPMF---------", "a-f-A-M-F")
    assert air_codes("neutral", "rotary") == ("SNAPCH---------", "a-n-A-C-H")
    assert air_codes("unknown", "unknown") == ("SUAP-----------", "a-u-A")
    assert sea_codes("neutral", "cargo") == ("SNSPXMC--------", "a-n-S-X-M-C")
    assert sea_codes("friend", "law_enforcement") == ("SFSPXL---------", "a-f-S-X-L")
    for aff, fn in (("friend", "fixed_wing"), ("hostile", "mil_rotary")):
        sidc, _ = air_codes(aff, fn)
        assert len(sidc) == 15


def test_ais_and_adsb_function_mapping():
    assert ais_function(30) == "fishing"
    assert ais_function(52) == "tow"
    assert ais_function(55) == "law_enforcement"
    assert ais_function(70) == "cargo"
    assert ais_function(84) == "tanker"
    assert ais_function(36) == "leisure"
    assert ais_function(None) == "unknown"
    assert adsb_function("A7", False) == "rotary"
    assert adsb_function("A3", True) == "mil_fixed_wing"


def test_track_to_cot_is_well_formed():
    xml = track_to_cot(make_track(), stale_seconds=120)
    ev = ET.fromstring(xml)
    assert ev.tag == "event" and ev.get("version") == "2.0"
    assert ev.get("uid") == "adsb-abc123" and ev.get("type") == "a-n-A-C-F" and ev.get("how") == "m-g"
    assert ev.get("time") == "2026-10-01T12:00:00.000Z"
    assert ev.get("stale") == "2026-10-01T12:02:00.000Z"
    point = ev.find("point")
    assert point.get("lat") == "36.950000" and point.get("lon") == "-76.300000" and point.get("hae") == "10000.0"
    assert ev.find("detail/contact").get("callsign") == "UAL123"
    track = ev.find("detail/track")
    assert track.get("course") == "90.0"
    assert abs(float(track.get("speed")) - 450 * 0.514444) < 0.01  # knots on the picture, m/s on the wire
    assert "squawk=1200" in ev.find("detail/remarks").text
    assert ev.find("detail/__kestrel").get("sidc") == "SNAPCF---------"


def test_track_without_altitude_uses_unknown_hae():
    ev = ET.fromstring(track_to_cot(make_track(alt_m=None, speed_kts=None, course_deg=None)))
    assert ev.find("point").get("hae") == "9999999"
    assert ev.find("detail/track") is None


def test_polygon_event_carries_atak_links_and_classic_shape():
    ring = [[-76.0, 36.5], [-75.0, 36.5], [-75.0, 37.0], [-76.0, 37.0], [-76.0, 36.5]]
    event = Event(uid="nws-1", kind="alert", source="nws", title="Severe Thunderstorm Warning", lat=36.75, lon=-75.5,
                  geometry={"type": "Polygon", "coordinates": [ring]}, severity="Severe", cot_type="u-d-f",
                  attrs={"headline": "until 7 PM"}, ts=NOW, expires=NOW + timedelta(hours=2))
    ev = ET.fromstring(event_to_cot(event, now=NOW))
    assert ev.get("type") == "u-d-f" and ev.get("how") == "h-e"
    links = ev.findall("detail/link")
    assert len(links) == len(ring)
    assert links[0].get("point") == "36.500000,-76.000000,0"
    assert ev.find("detail/strokeColor") is not None and ev.find("detail/fillColor") is not None
    assert int(ev.find("detail/strokeColor").get("value")) < 0  # signed ARGB, alpha 255 => negative
    assert len(ev.findall("detail/shape/polyline/vertex")) == len(ring)
    assert ev.get("stale") == "2026-10-01T14:00:00.000Z"
    assert "severity=Severe" in ev.find("detail/remarks").text


def test_circle_zone_uses_ellipse_shape():
    event = Event(uid="zone-roz-alpha", kind="zone", source="config", title="ROZ ALPHA", lat=36.85, lon=-75.7,
                  cot_type="u-d-c-c", attrs={"radius_km": 18.0, "zone_type": "circle"}, ts=NOW)
    ev = ET.fromstring(event_to_cot(event, now=NOW))
    ellipse = ev.find("detail/shape/ellipse")
    assert ellipse.get("major") == "18000" and ellipse.get("minor") == "18000"


def test_alert_to_cot_includes_spotrep():
    alert = Alert(id="alt-7", severity="HIGH", rule="emergency_squawk", title="GENERAL EMERGENCY (7700) — UAL123",
                  text="UAL123 [adsb-abc123] is squawking 7700.", subject_uid="adsb-abc123", lat=36.95, lon=-76.3,
                  ts=NOW, triage="SPOTREP alt-7\n1. WHAT: emergency")
    ev = ET.fromstring(alert_to_cot(alert))
    assert ev.get("uid") == "kestrel-alt-7"
    assert ev.find("detail/contact").get("callsign").startswith("ALERT HIGH")
    assert "SPOTREP" in ev.find("detail/remarks").text
    assert ev.find("detail/__kestrel").get("subject") == "adsb-abc123"
