"""Traffic cameras: one real record per source through its parser, the area filter, the watch officer tools, the
image fetch, and the HTTP proxy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from helpers import AsgiClient, RecordingContext, demo_settings
from kestrelcop.ai import WatchOfficer
from kestrelcop.ai.mock import mock_ask
from kestrelcop.ai.tools import PictureTools
from kestrelcop.feeds import cameras as cams
from kestrelcop.feeds.cameras import (CamerasFeed, camera_from_caltrans, camera_from_cars, camera_from_drivebc,
                                      camera_from_ottawa, camera_from_toronto, camera_from_wsdot, fetch_camera_image)
from kestrelcop.pipeline import Pipeline
from kestrelcop.store import Store
from kestrelcop.web import server

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

DRIVEBC = {"id": 286, "name": "Chimdemash", "caption": "Highway 16 near Chimdemash Creek, looking north.",
           "links": {"imageDisplay": "/images/286.jpg?t=1792086169"}, "highway_display": "16", "orientation": "N",
           "location": {"type": "Point", "coordinates": [-128.38556, 54.657221]}, "is_on": True, "update_period_mean": 899,
           "name_override": "", "caption_override": "", "credit": ""}
CALTRANS = {"cctv": {"index": "1", "inService": "true",
                     "location": {"district": "4", "locationName": "TV102 -- I-580 : West of SR-24", "nearbyPlace": "Oakland",
                                  "longitude": "-122.27291", "latitude": "37.82539", "direction": "West", "county": "Alameda", "route": "I-580"},
                     "imageData": {"streamingVideoURL": "https:\\/\\/wzmedia.dot.ca.gov\\/D4\\/W580_JWO_24_IC.stream\\/playlist.m3u8",
                                   "static": {"currentImageUpdateFrequency": "5",
                                              "currentImageURL": "https:\\/\\/cwwp2.dot.ca.gov\\/data\\/d4\\/cctv\\/image\\/tv102i580westofsr24\\/tv102i580westofsr24.jpg"}}}}
TORONTO = {"type": "Feature", "properties": {"_id": 1, "REC_ID": 8001, "IMAGEURL": "https://opendata.toronto.ca/transportation/tmc/rescucameraimages/CameraImages/loc8001.jpg",
                                             "MAINROAD": "YORK ST", "CROSSROAD": "BREMNER BLVD / RAPTORS WAY"},
           "geometry": {"type": "MultiPoint", "coordinates": [[-79.381386, 43.643120]]}}
OTTAWA = {"cameraOwner": "CITY", "latitude": 45.416354, "name": "Booth & Kichi Zībī Mīkan / Wellington", "camera_number": 49, "id": 33, "longitude": -75.714726}
WSDOT = {"CameraID": 1001, "Title": "I-5 at Mercer St", "ImageURL": "https://images.wsdot.wa.gov/nw/005vc16743.jpg", "IsActive": True,
         "DisplayLatitude": 47.624, "DisplayLongitude": -122.329, "CameraLocation": {"RoadName": "I-5", "Direction": "N", "Description": "Mercer St"},
         "CameraOwner": "WSDOT", "Region": "NW"}
CARS_V2 = {"Id": "3391", "Source": "MTO", "Roadway": "Highway 417", "Direction": "East", "Latitude": 45.3473, "Longitude": -75.9218,
           "Location": "Highway 417 near Moodie Drive", "Views": [{"Id": 1, "Url": "https://511on.ca/map/Cctv/3391", "Status": "Enabled", "Description": "Hwy 417 @ Moodie"}]}


def test_each_source_record_becomes_a_camera_event():
    bc = camera_from_drivebc(DRIVEBC, now=NOW)
    assert bc.uid == "cam-bc-286" and bc.kind == "camera" and bc.lat == 54.657221 and bc.lon == -128.38556
    assert bc.attrs["image_url"] == "https://www.drivebc.ca/images/286.jpg?t=1792086169" and bc.attrs["road"] == "Hwy 16"
    assert bc.expires > NOW and bc.cot_type == "b-m-p-s-m"
    assert camera_from_drivebc({**DRIVEBC, "is_on": False}, now=NOW) is None

    ca = camera_from_caltrans(CALTRANS, now=NOW)
    assert ca.uid.startswith("cam-ca-d4-") and ca.title.startswith("TV102") and ca.attrs["direction"] == "West"
    assert ca.attrs["image_url"].startswith("https://cwwp2.dot.ca.gov/") and "\\" not in ca.attrs["image_url"]
    assert ca.attrs["stream_url"].endswith("playlist.m3u8") and ca.attrs["refresh_seconds"] == 300
    assert camera_from_caltrans({"cctv": {**CALTRANS["cctv"], "inService": "false"}}, now=NOW) is None

    to = camera_from_toronto(TORONTO, now=NOW)
    assert to.uid == "cam-to-8001" and to.title == "York St / Bremner Blvd / Raptors Way" and to.lat == 43.643120
    assert to.attrs["image_url"].startswith("https://opendata.toronto.ca/")

    ot = camera_from_ottawa(OTTAWA, now=NOW)
    assert ot.uid == "cam-ott-49" and ot.attrs["image_url"] == "https://traffic.ottawa.ca/map/camera?id=49" and ot.attrs["owner"] == "City of Ottawa"
    assert camera_from_ottawa({**OTTAWA, "camera_number": 2101, "cameraOwner": "MTO"}, now=NOW).attrs["owner"] == "MTO"

    wa = camera_from_wsdot(WSDOT, now=NOW)
    assert wa.uid == "cam-wa-1001" and wa.attrs["road"] == "I-5" and wa.lat == 47.624
    assert camera_from_wsdot({**WSDOT, "IsActive": False}, now=NOW) is None

    on = camera_from_cars(CARS_V2, "ontario", now=NOW)
    assert on.uid == "cam-ontario-3391" and on.source == "511-ontario" and on.attrs["image_url"] == "https://511on.ca/map/Cctv/3391"
    assert on.attrs["road"] == "Highway 417" and on.title == "Highway 417 near Moodie Drive"
    flat = camera_from_cars({"ID": "7", "Latitude": 42.0, "Longitude": -83.0, "Url": "https://x/7.jpg", "Location": "Somewhere"}, "wisconsin", now=NOW)
    assert flat.uid == "cam-wisconsin-7"
    assert camera_from_cars({"Latitude": 1, "Longitude": 2}, "x", now=NOW) is None


async def test_feed_keeps_only_cameras_inside_the_area_and_survives_a_failing_source(monkeypatch):
    settings = demo_settings()
    settings.area.center = (45.42, -75.69)   # Ottawa
    settings.area.radius_nm = 60
    settings.feeds.cameras.drivebc = settings.feeds.cameras.caltrans = settings.feeds.cameras.toronto = True
    ctx = RecordingContext()
    feed = CamerasFeed(ctx, settings)

    async def fake_get_json(url, **kw):
        if "drivebc" in url:
            return [DRIVEBC]                         # far away: filtered out
        if "cwwp2" in url:
            raise RuntimeError("caltrans down")      # one failing source is logged, not fatal
        if "toronto" in url:
            return {"features": [TORONTO]}           # 350 km away: filtered out
        if "ottawa" in url:
            return {"cameras": [OTTAWA, {**OTTAWA, "camera_number": 50, "latitude": 45.40, "longitude": -75.70}]}
        raise AssertionError(url)

    monkeypatch.setattr(feed, "_get_json", fake_get_json)
    n = await feed.poll()
    assert n == 2 and {e.uid for e in ctx.events} == {"cam-ott-49", "cam-ott-50"}
    assert feed.last_sources["ottawa"] == 2 and feed.last_sources["drivebc"] == 0 and "caltrans" in feed.detail()

    async def all_fail(url, **kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(feed, "_get_json", all_fail)
    with pytest.raises(RuntimeError):
        await feed.poll()


def seeded_store() -> tuple[Store, PictureTools]:
    settings = demo_settings()
    settings.area.center = (45.42, -75.69)
    store = Store()
    for rec in (OTTAWA, {**OTTAWA, "camera_number": 50, "name": "Bank & Laurier", "latitude": 45.4210, "longitude": -75.6950},
                {**OTTAWA, "camera_number": 2101, "cameraOwner": "MTO", "name": "Hwy 417 @ Bronson", "latitude": 45.4000, "longitude": -75.7100}):
        store.upsert_event(camera_from_ottawa(rec, now=NOW))
    return store, PictureTools(store, settings)


def test_find_and_view_camera_tools_and_the_mock_officer():
    store, tools = seeded_store()
    res = tools.call("find_cameras", {"lat": 45.4214, "lon": -75.6919, "radius_km": 5, "limit": 2})
    assert res["cameras_on_picture"] == 3 and res["matched"] == 3 and len(res["cameras"]) == 2
    assert res["cameras"][0]["uid"] == "cam-ott-50" and res["cameras"][0]["distance_km"] < 0.5 and "bearing_deg" in res["cameras"][0]
    assert tools.summarize("find_cameras", res).startswith("2 of 3 camera(s)")
    assert tools.call("find_cameras", {"text": "417"})["cameras"][0]["uid"] == "cam-ott-2101"
    assert tools.call("find_cameras", {"near_uid": "nope"})["error"]

    view = tools.call("view_camera", {"uid": "cam-ott-49"})
    assert view["image_url"] == "https://traffic.ottawa.ca/map/camera?id=49" and view["camera"]["name"].startswith("Booth")
    assert "error" in tools.call("view_camera", {"uid": "adsb-zzz"})
    assert all(e["kind"] != "camera" for e in tools.call("list_events", {})["events"])   # cameras do not flood the event list
    assert tools.call("list_events", {"kind": "camera"})["total"] == 3
    assert tools.call("picture_summary", {})["cameras_on_picture"] == 3

    text, trace = mock_ask("what cameras are near the centre?", tools)
    assert "[cam-ott-" in text and trace[-1]["tool"] == "find_cameras" and "cannot look at imagery" in text


async def test_fetch_camera_image_validates_and_caches():
    calls = []

    class FakeResp:
        def __init__(self, ctype, body):
            self.headers = {"content-type": ctype}
            self.content = body

        def raise_for_status(self):
            pass

    class FakeHttp:
        async def get(self, url, **kw):
            calls.append(url)
            if "png" in url:
                return FakeResp("image/jpeg", b"\x89PNG\r\n\x1a\nbytes")   # mislabelled, as Toronto does
            if "html" in url:
                return FakeResp("text/html; charset=utf-8", b"<html>")
            return FakeResp("image/jpg", b"\xff\xd8jpegbytes")

    cams._image_cache.clear()
    data, ctype = await fetch_camera_image(FakeHttp(), "https://cam/1.jpg")
    assert data.startswith(b"\xff\xd8") and ctype == "image/jpeg"
    await fetch_camera_image(FakeHttp(), "https://cam/1.jpg")
    assert calls == ["https://cam/1.jpg"]   # second call served from the short cache
    assert (await fetch_camera_image(FakeHttp(), "https://cam/2.png"))[1] == "image/png"
    with pytest.raises(ValueError):
        await fetch_camera_image(FakeHttp(), "https://cam/html")


async def test_watch_officer_hands_the_image_to_the_model(monkeypatch):
    from test_watch_officer import FakeClient

    store, _ = seeded_store()
    client = FakeClient([
        {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "view_camera", "id": "toolu_1", "input": {"uid": "cam-ott-49"}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Light traffic, wet road, daylight [cam-ott-49]."}]},
    ])
    settings = demo_settings()
    settings.area.center = (45.42, -75.69)
    officer = WatchOfficer(store, settings, client=client)

    async def fake_fetch(http, url):
        return b"\xff\xd8fake", "image/jpeg"

    monkeypatch.setattr("kestrelcop.ai.watch_officer.fetch_camera_image", fake_fetch)
    events = [ev async for ev in officer.ask_stream("What does the Booth camera show?")]
    step = next(e for e in events if e["t"] == "tool_result")
    assert step["tool"] == "view_camera" and step["image_url"].endswith("id=49") and step["camera_uid"] == "cam-ott-49"
    sent = client.messages.calls[1]["messages"][-1]["content"][0]["content"]
    assert sent[0]["type"] == "image" and sent[0]["source"]["media_type"] == "image/jpeg" and sent[1]["type"] == "text"
    assert events[-1]["answer"]["citations"] == ["cam-ott-49"]


async def test_camera_image_proxy_endpoint(monkeypatch):
    settings = demo_settings()
    store = Store()
    store.upsert_event(camera_from_ottawa(OTTAWA, now=NOW))
    pipeline = Pipeline(settings, store=store, watch_officer=WatchOfficer(store, settings, force_mock=True))
    app = server.create_app(settings, pipeline)

    async def fake_fetch(http, url):
        return b"\xff\xd8proxied", "image/jpeg"

    monkeypatch.setattr("kestrelcop.web.server.fetch_camera_image", fake_fetch)
    async with AsgiClient(app) as client:
        code, headers, body = await client.request("GET", "/api/camera/cam-ott-49/image")
        assert code == 200 and headers["content-type"].startswith("image/jpeg") and body == b"\xff\xd8proxied"
        code, _, _ = await client.request("GET", "/api/camera/cam-nope/image")
        assert code == 404
        health = await client.get_json("/api/health")
        assert any(f["name"] == "cameras" and f["status"] == "off" for f in health["feeds"])   # demo world: live feeds only
