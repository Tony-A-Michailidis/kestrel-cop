"""The HTTP surface, driven through ASGI directly (no httpx needed) against the demo world."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from helpers import AsgiClient, demo_settings
from kestrelcop.ai import WatchOfficer
from kestrelcop.pipeline import Pipeline
from kestrelcop.store import Store
from kestrelcop.web import server


def build():
    settings = demo_settings()
    store = Store()
    pipeline = Pipeline(settings, store=store, watch_officer=WatchOfficer(store, settings, force_mock=True))
    return settings, pipeline, server.create_app(settings, pipeline)


async def test_health_snapshot_tracks_and_cot():
    settings, pipeline, app = build()
    async with AsgiClient(app) as client:
        await asyncio.sleep(0.3)  # let the demo world's first tick land
        health = await client.get_json("/api/health")
        assert health["ok"] is True and health["demo"] is True and health["ai"]["mode"] == "mock"
        assert {f["name"] for f in health["feeds"]} == {"adsb", "ais", "nws", "firms", "usgs"}
        snap = await client.get_json("/api/snapshot")
        assert snap["counts"]["tracks"] > 30 and snap["counts"]["events_by_kind"]["zone"] == 1
        uid = snap["tracks"][0]["uid"]
        one = await client.get_json(f"/api/tracks/{uid}")
        assert one["uid"] == uid and len(one["sidc"]) == 15
        code, headers, body = await client.request("GET", f"/api/cot/{uid}")
        assert code == 200 and headers["content-type"].startswith("application/xml") and body.startswith(b"<event ")
        code, _, _ = await client.request("GET", "/api/cot/nope")
        assert code == 404
        alerts = await client.get_json("/api/alerts")
        assert any(a["rule"] == "earthquake" for a in alerts)  # the demo world's M6.1
        settings_json = await client.get_json("/api/settings")
        assert settings_json["area"]["center"] == [36.95, -76.3] and settings_json["zones"][0]["name"] == "ROZ ALPHA"


async def test_sse_stream_starts_with_a_hello_snapshot():
    _, _, app = build()
    async with AsgiClient(app) as client:
        await asyncio.sleep(0.3)
        events = await client.sse("/api/stream", max_events=1)
        assert events and events[0]["t"] == "hello"
        hello = events[0]
        assert hello["settings"]["demo"] is True and len(hello["snapshot"]["tracks"]) > 30
        assert hello["snapshot"]["feeds"]


async def test_watch_officer_endpoints():
    _, pipeline, app = build()
    async with AsgiClient(app) as client:
        await asyncio.sleep(0.3)
        code, _, body = await client.request("POST", "/api/ai/ask", {"question": "How many aircraft are on the picture?"})
        assert code == 200 and b"aircraft" in body
        code, _, _ = await client.request("POST", "/api/ai/ask", {"question": ""})
        assert code == 400
        events = await client.sse("/api/ai/ask/stream?q=Any%20alerts%3F", max_events=100)
        kinds = [e["t"] for e in events]
        assert "tool" in kinds and "tool_result" in kinds and "text" in kinds and kinds[-1] == "done"
        assert events[-1]["answer"]["mode"] == "mock"
        code, _, _ = await client.request("GET", "/api/ai/sitrep")
        assert code == 404  # none yet
        events = await client.sse("/api/ai/sitrep/stream", max_events=400)
        assert events[-1]["t"] == "done" and "KESTREL SITREP" in events[-1]["sitrep"]["text"]
        last = await client.get_json("/api/ai/sitrep")
        assert last["text"] == events[-1]["sitrep"]["text"]
        assert pipeline.watch_officer.last_sitrep is not None


async def test_vendor_files_fall_back_to_the_pinned_cdn():
    _, _, app = build()
    original = server.VENDOR_DIR
    with tempfile.TemporaryDirectory() as tmp:
        server.VENDOR_DIR = Path(tmp)
        try:
            async with AsgiClient(app) as client:
                code, headers, _ = await client.request("GET", "/static/vendor/maplibre-gl.js")
                assert code == 302 and headers["location"] == server.VENDOR_CDN["maplibre-gl.js"]
                code, _, _ = await client.request("GET", "/static/vendor/unknown.js")
                assert code == 404
                (Path(tmp) / "milsymbol.js").write_text("window.ms = {};")
                code, _, body = await client.request("GET", "/static/vendor/milsymbol.js")
                assert code == 200 and body == b"window.ms = {};"
        finally:
            server.VENDOR_DIR = original


async def test_index_and_static_assets_are_served():
    _, _, app = build()
    async with AsgiClient(app) as client:
        code, headers, body = await client.request("GET", "/")
        assert code == 200 and b"<title>Kestrel COP</title>" in body
        for path in ("/static/app.js", "/static/style.css", "/static/basemap/land.json", "/static/glyphs/Inter%20SemiBold/0-255.pbf", "/static/fonts/Inter-Regular.woff"):
            code, _, body = await client.request("GET", path)
            assert code == 200 and len(body) > 100, path
