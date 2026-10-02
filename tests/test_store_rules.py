"""The in-memory picture and the built-in detectors."""

from __future__ import annotations

from datetime import timedelta

from helpers import demo_settings
from kestrelcop.ai import WatchOfficer
from kestrelcop.config import RulesConfig, ZoneConfig
from kestrelcop.models import Event, FeedStatus, Track, utcnow
from kestrelcop.pipeline import Pipeline, zone_events
from kestrelcop.rules import RuleEngine
from kestrelcop.store import Store


def track(uid="adsb-1", lat=36.95, lon=-76.30, **kw) -> Track:
    base = dict(uid=uid, domain="air", source="adsb", callsign=uid.upper(), lat=lat, lon=lon, alt_m=3000.0, speed_kts=120.0,
                course_deg=90.0, affiliation="neutral", function="fixed_wing", sidc="SNAPCF---------", cot_type="a-n-A-C-F")
    base.update(kw)
    return Track(**base)


def test_store_keeps_first_seen_and_builds_a_trail():
    store = Store(trail_length=3)
    first, prev = store.upsert_track(track())
    assert prev is None and first.trail == [[-76.3, 36.95]]
    for i in range(1, 5):
        t, prev = store.upsert_track(track(lon=-76.30 + 0.01 * i))
        assert prev is not None
    assert t.first_seen == first.first_seen
    assert len(t.trail) == 3 and t.trail[-1] == [-76.26, 36.95]
    assert store.counts()["by_domain"]["air"] == 1


def test_sweep_drops_stale_tracks_and_expired_events():
    store = Store(stale_seconds_air=60, stale_seconds_sea=600)
    now = utcnow()
    store.upsert_track(track("adsb-old", ts=now - timedelta(seconds=120)))
    store.upsert_track(track("adsb-new", ts=now))
    store.upsert_track(track("ais-1", domain="sea", ts=now - timedelta(seconds=120)))  # sea tracks live longer
    store.upsert_event(Event(uid="e-old", kind="fire", source="firms", title="x", lat=0, lon=0, expires=now - timedelta(minutes=1)))
    store.upsert_event(Event(uid="e-live", kind="fire", source="firms", title="x", lat=0, lon=0, expires=now + timedelta(minutes=1)))
    dead_tracks, dead_events = store.sweep(now)
    assert dead_tracks == ["adsb-old"] and dead_events == ["e-old"]
    assert set(store.tracks) == {"adsb-new", "ais-1"} and set(store.events) == {"e-live"}


def test_emergency_squawk_alerts_once_and_flags_the_track():
    store = Store()
    engine = RuleEngine(store, RulesConfig())
    t, prev = store.upsert_track(track(attrs={"squawk": "7700"}))
    alerts = engine.on_track(t, prev)
    assert len(alerts) == 1 and alerts[0].severity == "HIGH" and alerts[0].rule == "emergency_squawk"
    assert "adsb-1" in alerts[0].text and "EMERGENCY" in t.flags
    t2, prev2 = store.upsert_track(track(attrs={"squawk": "7700"}))
    assert engine.on_track(t2, prev2) == []  # no re-alerting while the condition persists
    t3, prev3 = store.upsert_track(track(attrs={"squawk": "1200"}))
    engine.on_track(t3, prev3)
    assert "EMERGENCY" not in t3.flags


def test_zone_entry_alert_and_flag():
    zone = ZoneConfig(name="ROZ ALPHA", type="circle", center=(36.85, -75.70), radius_km=18.0, severity="MEDIUM")
    store = Store()
    engine = RuleEngine(store, RulesConfig(zones=[zone]))
    outside, prev = store.upsert_track(track(lat=36.0, lon=-77.0))
    assert engine.on_track(outside, prev) == []
    inside, prev = store.upsert_track(track(lat=36.85, lon=-75.70))
    alerts = engine.on_track(inside, prev)
    assert len(alerts) == 1 and alerts[0].rule == "zone_entry" and "IN_ZONE:ROZ ALPHA" in inside.flags
    again, prev = store.upsert_track(track(lat=36.86, lon=-75.71))
    assert engine.on_track(again, prev) == []  # still inside: one alert per visit
    left, prev = store.upsert_track(track(lat=36.0, lon=-77.0))
    engine.on_track(left, prev)
    assert "IN_ZONE:ROZ ALPHA" not in left.flags


def test_event_rules_quake_weather_fire():
    store = Store()
    engine = RuleEngine(store, RulesConfig(quake_alert_magnitude=4.0))
    quake = Event(uid="usgs-1", kind="quake", source="usgs", title="Somewhere", lat=1, lon=2, magnitude=6.3, attrs={"depth_km": 10})
    small = Event(uid="usgs-2", kind="quake", source="usgs", title="Elsewhere", lat=1, lon=2, magnitude=2.0)
    severe = Event(uid="nws-1", kind="alert", source="nws", title="Tornado Warning", lat=1, lon=2, severity="Extreme", attrs={"headline": "now"})
    fire = Event(uid="firms-1", kind="fire", source="firms", title="fire", lat=36.6, lon=-76.4, attrs={"frp": 80.0})
    fire2 = Event(uid="firms-2", kind="fire", source="firms", title="fire", lat=36.61, lon=-76.41, attrs={"frp": 90.0})
    results = [engine.on_event(*store.upsert_event(e)) for e in (quake, small, severe, fire, fire2)]
    assert [len(r) for r in results] == [1, 0, 1, 1, 0]  # the second fire shares a 10 km cell with the first
    assert results[0][0].severity == "HIGH" and results[2][0].severity == "HIGH"
    assert engine.on_event(*store.upsert_event(quake)) == []  # re-ingesting an existing event is not new


def test_ais_gap_and_stale_feed_are_periodic():
    store = Store()
    engine = RuleEngine(store, RulesConfig(ais_gap_minutes=10, stale_feed_seconds=120))
    now = utcnow()
    store.upsert_track(track("ais-1", domain="sea", speed_kts=12.0, ts=now - timedelta(minutes=15)))
    store.upsert_track(track("ais-2", domain="sea", speed_kts=0.2, ts=now - timedelta(minutes=15)))  # anchored: no gap alert
    feeds = [FeedStatus(name="adsb", label="ADS-B", mode="live", status="degraded", last_message=now - timedelta(minutes=5)),
             FeedStatus(name="ais", label="AIS", mode="off", status="off")]
    alerts = engine.periodic(feeds)
    rules = sorted(a.rule for a in alerts)
    assert rules == ["ais_gap", "feed_stale"]
    assert "DARK" in store.tracks["ais-1"].flags and "DARK" not in store.tracks["ais-2"].flags
    assert engine.periodic(feeds) == []  # deduplicated
    back, prev = store.upsert_track(track("ais-1", domain="sea", speed_kts=12.0, ts=now))
    engine.on_track(back, prev)
    assert "DARK" not in back.flags  # it reported again


def test_slow_polled_feed_is_not_stale_between_polls():
    """A feed polled every 180 s must not flap against the 120 s global window: feed_starting derives a per-feed
    window from the poll interval (two polls plus slack) and the stale rule honours it."""
    settings = demo_settings()
    store = Store()
    pipeline = Pipeline(settings, store=store, watch_officer=WatchOfficer(store, settings, force_mock=True))
    pipeline.feed_starting("nws", "NWS", "live", poll_seconds=180)
    fs = store.feeds["nws"]
    assert fs.stale_after_s == max(settings.rules.stale_feed_seconds, 2 * 180 + 30)
    engine = RuleEngine(store, RulesConfig(stale_feed_seconds=120))
    now = utcnow()
    fs.status, fs.last_message = "degraded", now - timedelta(seconds=150)
    assert engine.periodic([fs]) == []  # quiet for less than two polls: not stale
    fs.last_message = now - timedelta(seconds=400)
    assert [a.rule for a in engine.periodic([fs])] == ["feed_stale"]


def test_zone_events_and_pipeline_ingest_raise_alerts():
    settings = demo_settings()
    store = Store()
    pipeline = Pipeline(settings, store=store, watch_officer=WatchOfficer(store, settings, force_mock=True))
    zones = zone_events(settings)
    assert len(zones) == 1 and zones[0].uid == "zone-roz-alpha" and zones[0].geometry["type"] == "Polygon"
    pipeline.ingest_event(zones[0])
    pipeline.ingest_track(track(attrs={"squawk": "7500"}))
    assert len(pipeline.store.alerts) == 1 and "HIJACK" in pipeline.store.alerts[0].title
    assert pipeline.status()["counts"]["tracks"] == 1
