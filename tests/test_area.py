"""Moving the picture at runtime: settings, feeds, store, the HTTP endpoint and persistence follow the new area."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from helpers import AsgiClient, demo_settings
from kestrelcop.ai import WatchOfficer
from kestrelcop.config import AreaConfig, Settings, area_override_path
from kestrelcop.geo import haversine_km
from kestrelcop.pipeline import Pipeline
from kestrelcop.store import Store
from kestrelcop.web import server

OTTAWA = (45.42, -75.69)


def build_pipeline():
    settings = demo_settings()
    store = Store()
    return settings, store, Pipeline(settings, store=store, watch_officer=WatchOfficer(store, settings, force_mock=True))


async def test_set_area_moves_feeds_and_clears_the_old_picture():
    settings, store, pipeline = build_pipeline()
    await pipeline.start()
    try:
        await asyncio.sleep(0.3)  # the demo world's first tick, around Norfolk
        assert len(store.tracks) > 30 and any(e.kind == "zone" for e in store.events.values())
        old_tasks = list(pipeline._feed_tasks)

        result = await pipeline.set_area("Ottawa", OTTAWA, 100)

        assert settings.area.name == "Ottawa" and tuple(settings.area.center) == OTTAWA and settings.area.radius_nm == 100
        assert result["removed"]["tracks"] > 30 and result["zones"] == "cleared" and result["persisted_to"] is None
        assert settings.rules.zones == [] and not any(e.kind == "zone" for e in store.events.values())
        reach = settings.area.radius_km * 1.2
        assert all(haversine_km(*OTTAWA, t.lat, t.lon) <= reach for t in store.tracks.values())
        assert all(t.done() for t in old_tasks) and pipeline._feed_tasks and all(not t.done() for t in pipeline._feed_tasks)

        await asyncio.sleep(0.3)  # the rebuilt demo world ticks around the new centre
        assert store.tracks and all(haversine_km(*OTTAWA, t.lat, t.lon) <= settings.area.radius_km * 2 for t in store.tracks.values())
    finally:
        await pipeline.stop()


async def test_set_area_can_keep_zones_and_rejects_bad_input():
    settings, store, pipeline = build_pipeline()
    await pipeline.start()
    try:
        await asyncio.sleep(0.2)
        with pytest.raises(ValueError):
            await pipeline.set_area("x", (95.0, 0.0), 100)
        with pytest.raises(ValueError):
            await pipeline.set_area("x", (0.0, 0.0), 2)
        assert settings.area.name == demo_settings().area.name  # untouched after a rejected change
        result = await pipeline.set_area(None, OTTAWA, None, keep_zones=True)
        assert result["zones"] == "kept" and result["area"]["name"] == "45.42, -75.69"
        assert settings.rules.zones and any(e.kind == "zone" for e in store.events.values())
        assert settings.area.radius_nm == demo_settings().area.radius_nm  # radius unchanged when omitted
    finally:
        await pipeline.stop()


async def test_area_endpoint_moves_the_picture_and_the_stream_reports_it():
    settings, store, pipeline = build_pipeline()
    app = server.create_app(settings, pipeline)
    async with AsgiClient(app) as client:
        await asyncio.sleep(0.3)
        code, _, _ = await client.request("POST", "/api/area", {"center": [1]})
        assert code == 400
        code, _, _ = await client.request("POST", "/api/area", {"name": "Ottawa", "center": [95, 0]})
        assert code == 400
        code, _, body = await client.request("POST", "/api/area", {"name": "Ottawa", "center": list(OTTAWA), "radius_nm": 100})
        assert code == 200, body
        area = await client.get_json("/api/area")
        assert area["area"]["name"] == "Ottawa" and area["area"]["radius_nm"] == 100 and area["zones"] == []
        assert area["persisted_to"] is None  # demo settings come from no file
        events = await client.sse("/api/stream", max_events=1)
        assert events[0]["t"] == "hello" and events[0]["settings"]["area"]["center"] == list(OTTAWA)


def test_area_override_round_trip(tmp_path: Path):
    cfg = tmp_path / "kestrel.yaml"
    cfg.write_text("area:\n  name: Norfolk\n  center: [36.95, -76.30]\n  radius_nm: 150\n"
                   "rules:\n  zones:\n    - name: ROZ ALPHA\n      center: [36.85, -75.70]\n      radius_km: 18\n", encoding="utf-8")
    s = Settings.load(cfg)
    assert s.source_path == cfg and s.area.name == "Norfolk" and len(s.rules.zones) == 1

    s.area = AreaConfig(name="Ottawa", center=OTTAWA, radius_nm=100)
    s.rules.zones = []
    path = s.save_area_override()
    assert path == area_override_path(cfg) == tmp_path / "kestrel.area.yaml"

    again = Settings.load(cfg)
    assert again.area.name == "Ottawa" and tuple(again.area.center) == OTTAWA and again.rules.zones == []
    path.unlink()
    back = Settings.load(cfg)
    assert back.area.name == "Norfolk" and len(back.rules.zones) == 1
    assert Settings.for_demo().save_area_override() is None
