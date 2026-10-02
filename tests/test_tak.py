"""TAK output: bus messages become CoT, and (when PyTAK is installed) they arrive at a TCP listener."""

from __future__ import annotations

import asyncio

import pytest

from helpers import demo_settings
from kestrelcop.bus import Bus
from kestrelcop.config import TakConfig
from kestrelcop.cot.tak import TakPublisher, encode_bus_message
from kestrelcop.models import Alert, Event, Track


def sample_track() -> Track:
    return Track(uid="ais-367000001", domain="sea", source="ais", callsign="USCGC FORWARD", lat=36.98, lon=-75.52, alt_m=0.0,
                 speed_kts=14.0, course_deg=90.0, affiliation="friend", function="law_enforcement", sidc="SFSPXL---------",
                 cot_type="a-f-S-X-L", attrs={"mmsi": 367000001})


def test_encode_bus_message_routes_by_topic():
    tak = TakConfig(stale_seconds_air=120, stale_seconds_sea=1800, publish_alerts=False)
    assert b'type="a-f-S-X-L"' in encode_bus_message("track", sample_track(), tak)
    event = Event(uid="usgs-1", kind="quake", source="usgs", title="x", lat=1, lon=2, magnitude=5.0)
    assert b'uid="usgs-1"' in encode_bus_message("event", event, tak)
    alert = Alert(id="alt-1", severity="LOW", rule="r", title="t", text="x")
    assert encode_bus_message("alert", alert, tak) is None  # publish_alerts is off
    assert encode_bus_message("feed", object(), tak) is None
    assert encode_bus_message("alert", alert, TakConfig(publish_alerts=True)).startswith(b"<event ")


async def test_publisher_delivers_cot_to_a_tcp_sink():
    pytest.importorskip("pytak")
    received: list[bytes] = []
    got_one = asyncio.Event()

    async def sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            received.append(data)
            got_one.set()
        writer.close()

    server = await asyncio.start_server(sink, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    bus = Bus()
    publisher = TakPublisher(TakConfig(enabled=True, url=f"tcp://127.0.0.1:{port}"), bus)
    task = asyncio.create_task(publisher.run())
    try:
        for _ in range(50):  # wait for the connection
            if publisher.stats["connected"]:
                break
            await asyncio.sleep(0.1)
        assert publisher.stats["connected"], publisher.stats
        bus.publish("track", sample_track())
        await asyncio.wait_for(got_one.wait(), 5)
        for _ in range(50):  # PyTAK sends its own hello event first; wait for ours
            if b'uid="ais-367000001"' in b"".join(received):
                break
            await asyncio.sleep(0.1)
        data = b"".join(received)
        assert b"<event " in data and b'uid="ais-367000001"' in data and b"USCGC FORWARD" in data
        assert publisher.stats["sent"] == 1
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        server.close()
        await server.wait_closed()
