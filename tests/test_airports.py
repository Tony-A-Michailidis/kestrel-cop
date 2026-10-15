"""The vendored airports layer and the watch officer's find_airports tool."""

from __future__ import annotations

from helpers import demo_settings
from kestrelcop.ai.mock import mock_ask
from kestrelcop.ai.tools import PictureTools
from kestrelcop.airports import airport_by_code, load_airports, nearest_airports
from kestrelcop.store import Store

VANCOUVER = (49.2827, -123.1207)


def test_airports_layer_is_vendored_and_searchable():
    airports = load_airports()
    assert len(airports) > 3000 and all("icao" in a and "lat" in a for a in airports)
    yvr = airport_by_code("YVR")
    assert yvr and yvr["icao"] == "CYVR" and yvr["kind"] == "large" and airport_by_code("cyvr") == yvr
    near = nearest_airports(*VANCOUVER, radius_km=30, limit=3)
    assert near[0]["icao"] == "CYVR" and near[0]["distance_km"] < 15 and "bearing_deg" in near[0]
    assert nearest_airports(*VANCOUVER, radius_km=30, kinds=("large",), limit=5)[0]["icao"] == "CYVR"
    assert nearest_airports(0.0, -160.0, radius_km=50) == []   # mid-Pacific


def test_find_airports_tool_and_mock_officer():
    settings = demo_settings()
    settings.area.center = VANCOUVER
    tools = PictureTools(Store(), settings)
    res = tools.call("find_airports", {"radius_km": 40, "limit": 3})
    assert res["airports"][0]["icao"] == "CYVR" and res["centre"]["lat"] == 49.2827
    assert tools.summarize("find_airports", res).startswith("3 airport(s)")
    by_code = tools.call("find_airports", {"text": "CYOW"})
    assert by_code["airports"][0]["iata"] == "YOW" and by_code["airports"][0]["distance_km"] == 0
    assert tools.call("find_airports", {"near_uid": "nope"})["error"]
    text, trace = mock_ask("what is the nearest airport?", tools)
    assert "CYVR" in text and trace[-1]["tool"] == "find_airports"
