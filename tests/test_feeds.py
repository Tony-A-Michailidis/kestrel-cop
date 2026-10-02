"""Feed parsers: one real-looking record per source goes in, a normalised Track or Event comes out."""

from __future__ import annotations

from datetime import datetime, timezone

from helpers import RecordingContext, demo_settings
from kestrelcop.feeds.adsb import classify_aircraft, track_from_opensky, track_from_readsb
from kestrelcop.feeds.ais import AisFeed, classify_vessel
from kestrelcop.feeds.firms import event_from_row
from kestrelcop.feeds.nws import event_from_feature as nws_event
from kestrelcop.feeds.usgs import event_from_feature as usgs_event

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

READSB_RECORD = {
    "hex": "a1b2c3", "flight": "DAL2171 ", "r": "N123DL", "t": "B739", "category": "A3", "squawk": "7700",
    "lat": 36.9, "lon": -76.2, "alt_baro": 12000, "gs": 310.5, "track": 185.2, "true_heading": 183.0,
    "baro_rate": -1536, "seen_pos": 2.1, "rssi": -12.3,
}


def test_readsb_record_becomes_a_track():
    t = track_from_readsb(READSB_RECORD, now=NOW)
    assert t.uid == "adsb-a1b2c3" and t.callsign == "DAL2171" and t.domain == "air"
    assert abs(t.alt_m - 12000 * 0.3048) < 0.01
    assert t.speed_kts == 310.5 and t.course_deg == 185.2 and t.heading_deg == 183.0
    assert t.attrs["squawk"] == "7700" and t.attrs["registration"] == "N123DL" and t.attrs["aircraft_type"] == "B739"
    assert t.affiliation == "neutral" and t.function == "fixed_wing"
    assert t.sidc == "SNAPCF---------" and t.cot_type == "a-n-A-C-F"
    assert (NOW - t.ts).total_seconds() == 2.1  # position age is honoured


def test_readsb_on_ground_and_missing_position():
    assert track_from_readsb({"hex": "abc", "lat": None, "lon": None}, now=NOW) is None
    t = track_from_readsb({"hex": "abc", "lat": 1.0, "lon": 2.0, "alt_baro": "ground"}, now=NOW)
    assert t.alt_m == 0.0 and t.attrs["on_ground"] is True
    assert track_from_readsb({"hex": "abc", "lat": 1.0, "lon": 2.0, "seen_pos": 500}, now=NOW) is None  # too old


def test_military_classification():
    assert classify_aircraft("ae1234", None, "A3") == ("friend", True)      # US military hex block
    assert classify_aircraft("a1b2c3", "RCH441", "A5") == ("friend", True)  # callsign prefix
    assert classify_aircraft("a1b2c3", "UAL1", "A3") == ("neutral", False)
    assert classify_aircraft("a1b2c3", None, None) == ("unknown", False)
    t = track_from_readsb({**READSB_RECORD, "hex": "ae1234", "flight": "NAVY01", "category": "A7"}, now=NOW)
    assert t.function == "mil_rotary" and t.sidc == "SFAPMH---------" and "MILITARY" in t.flags


def test_opensky_state_vector():
    state = ["3c6444", "DLH9CK  ", "Germany", 1759320000, 1759320000, 8.5, 50.03, 10972.8, False, 230.1, 270.0, -2.6, None, 11277.6, "1000", False, 0, 4]
    t = track_from_opensky(state, now=NOW)
    assert t.uid == "adsb-3c6444" and t.callsign == "DLH9CK" and t.lat == 50.03 and t.lon == 8.5
    assert abs(t.speed_kts - 230.1 / 0.514444) < 0.1 and t.course_deg == 270.0
    assert t.attrs["origin_country"] == "Germany" and t.attrs["category"] == "A3"


def test_nws_feature_becomes_a_polygon_event():
    feature = {
        "properties": {"id": "urn:oid:2.49.0.1.840.0.abc", "event": "Small Craft Advisory", "severity": "Minor",
                       "headline": "Small Craft Advisory until 10 PM", "areaDesc": "Coastal waters", "urgency": "Expected",
                       "certainty": "Likely", "senderName": "NWS Wakefield VA", "sent": "2026-10-01T10:00:00+00:00",
                       "ends": "2026-10-01T22:00:00+00:00"},
        "geometry": {"type": "Polygon", "coordinates": [[[-76.0, 36.5], [-75.0, 36.5], [-75.0, 37.0], [-76.0, 37.0], [-76.0, 36.5]]]},
    }
    e = nws_event(feature)
    assert e.kind == "alert" and e.uid.startswith("nws-") and e.title == "Small Craft Advisory"
    assert abs(e.lat - 36.7) < 0.01 and abs(e.lon - (-75.6)) < 0.01  # centroid of the ring's vertices
    assert e.expires.isoformat() == "2026-10-01T22:00:00+00:00"
    assert e.attrs["sender"] == "NWS Wakefield VA"
    assert nws_event({"properties": {"id": "x"}, "geometry": None}) is None  # zone-only alerts are skipped


def test_usgs_feature_becomes_a_quake_event():
    feature = {"id": "us7000abcd", "properties": {"mag": 6.1, "place": "112 km E of Ishinomaki, Japan", "time": 1759320000000,
                                                   "tsunami": 0, "url": "https://earthquake.usgs.gov/x"},
               "geometry": {"type": "Point", "coordinates": [142.69, 38.32, 34.0]}}
    e = usgs_event(feature)
    assert e.uid == "usgs-us7000abcd" and e.kind == "quake" and e.magnitude == 6.1 and e.severity == "high"
    assert e.lat == 38.32 and e.lon == 142.69 and e.attrs["depth_km"] == 34.0
    assert e.ts == datetime.fromtimestamp(1759320000, tz=timezone.utc)
    assert usgs_event({"properties": {"mag": None}, "geometry": {"coordinates": [1, 2]}}) is None


def test_firms_row_becomes_a_fire_event():
    row = {"latitude": "36.6072", "longitude": "-76.4642", "bright_ti4": "341.2", "acq_date": "2026-10-01", "acq_time": "0612",
           "satellite": "N", "confidence": "n", "frp": "72.4", "daynight": "D"}
    e = event_from_row(row, "VIIRS_SNPP_NRT")
    assert e.kind == "fire" and e.severity == "high" and e.attrs["frp"] == 72.4
    assert e.ts.isoformat() == "2026-10-01T06:12:00+00:00" and e.title == "VIIRS fire detection"
    assert event_from_row({"latitude": "x"}, "VIIRS_SNPP_NRT") is None


def test_ais_static_data_is_merged_into_position_reports():
    ctx = RecordingContext()
    feed = AisFeed(ctx, demo_settings())
    feed.handle({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 367123456, "ShipName": "LADY CAROLINE"},
                 "Message": {"ShipStaticData": {"Type": 30, "Destination": "NORFOLK", "Name": "LADY CAROLINE", "CallSign": "WDA1234", "ImoNumber": 0}}})
    assert ctx.tracks == []  # static data alone is not a position
    feed.handle({"MessageType": "PositionReport", "MetaData": {"MMSI": 367123456, "ShipName": "LADY CAROLINE", "latitude": 36.9, "longitude": -75.5,
                                                                "time_utc": "2026-10-01 12:00:00.123 +0000 UTC"},
                 "Message": {"PositionReport": {"Latitude": 36.9, "Longitude": -75.5, "Sog": 4.2, "Cog": 45.0, "TrueHeading": 44, "NavigationalStatus": 7}}})
    t = ctx.tracks[0]
    assert t.uid == "ais-367123456" and t.callsign == "LADY CAROLINE" and t.function == "fishing"
    assert t.sidc == "SNSPXF---------" and t.attrs["destination"] == "NORFOLK" and t.attrs["nav_status"] == "engaged in fishing"
    assert t.speed_kts == 4.2 and t.course_deg == 45.0 and t.heading_deg == 44.0
    assert t.ts.isoformat() == "2026-10-01T12:00:00.123000+00:00"


def test_vessel_classification():
    assert classify_vessel(55, "SOME BOAT") == ("friend", "law_enforcement")
    assert classify_vessel(70, "USCGC FORWARD") == ("friend", "law_enforcement")
    assert classify_vessel(35, "WARSHIP") == ("unknown", "combatant")
    assert classify_vessel(80, "STENA CONQUEST") == ("neutral", "tanker")
    assert classify_vessel(None, None) == ("unknown", "unknown")
