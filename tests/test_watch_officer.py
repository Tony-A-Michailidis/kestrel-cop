"""The watch officer: the mock path end to end, citation checking, and the Claude agentic loop driven by
a fake client that speaks the Anthropic SDK's streaming shapes (no network, no SDK required)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from helpers import demo_settings
from kestrelcop.ai import WatchOfficer, verify_citations
from kestrelcop.models import Alert, Event, Track
from kestrelcop.store import Store


def seeded_store() -> Store:
    store = Store()
    base = dict(domain="air", source="adsb", lat=36.95, lon=-76.30, alt_m=9000.0, speed_kts=440.0, course_deg=180.0,
                affiliation="neutral", function="fixed_wing", sidc="SNAPCF---------", cot_type="a-n-A-C-F")
    store.upsert_track(Track(uid="adsb-emerg", callsign="UPS556", attrs={"squawk": "7700"}, flags=["EMERGENCY"], **base))
    store.upsert_track(Track(uid="adsb-plain", callsign="DAL100", attrs={"squawk": "2345"}, **{**base, "lat": 37.2}))
    store.upsert_track(Track(uid="ais-cutter", callsign="USCGC FORWARD", domain="sea", source="ais", lat=36.98, lon=-75.52, alt_m=0.0,
                             speed_kts=14.0, course_deg=90.0, affiliation="friend", function="law_enforcement", sidc="SFSPXL---------",
                             cot_type="a-f-S-X-L", attrs={"mmsi": 367000001}))
    store.upsert_event(Event(uid="nws-1", kind="alert", source="nws", title="Severe Thunderstorm Warning", lat=36.9, lon=-77.0, severity="Severe",
                             attrs={"headline": "until 7 PM"}))
    store.add_alert(Alert(id="alt-1", severity="HIGH", rule="emergency_squawk", title="GENERAL EMERGENCY (7700) — UPS556",
                          text="UPS556 [adsb-emerg] is squawking 7700.", subject_uid="adsb-emerg", lat=36.95, lon=-76.3))
    return store


def test_verify_citations_separates_known_from_unknown():
    store = seeded_store()
    verified, unverified = verify_citations("UPS556 [adsb-emerg] near [ais-cutter]; see [alt-1] and [adsb-ghost] [adsb-emerg]", store)
    assert verified == ["adsb-emerg", "ais-cutter", "alt-1"] and unverified == ["adsb-ghost"]


async def test_mock_officer_answers_with_a_real_tool_trace():
    store = seeded_store()
    officer = WatchOfficer(store, demo_settings(), force_mock=True)
    assert officer.mode == "mock"
    answer = await officer.ask("Any emergencies on the picture?")
    assert "UPS556" in answer.text and "adsb-emerg" in answer.citations and answer.unverified == []
    assert [s["tool"] for s in answer.trace] == ["picture_summary", "search_tracks"]
    near = await officer.ask("What is within 100 km of USCGC FORWARD?")
    assert "ais-cutter" in near.citations and ("adsb-plain" in near.text or "adsb-emerg" in near.text)


async def test_mock_sitrep_has_the_seven_sections_and_cites():
    store = seeded_store()
    officer = WatchOfficer(store, demo_settings(), force_mock=True)
    events = [ev async for ev in officer.sitrep_stream()]
    assert events[-1]["t"] == "done"
    text = events[-1]["sitrep"]["text"]
    for section in ("1. SITUATION", "2. AIR", "3. MARITIME", "4. ENVIRONMENT", "5. ALERTS", "6. DATA CONFIDENCE", "7. WATCH OFFICER ASSESSMENT"):
        assert section in text
    assert "[adsb-emerg]" in text and "[alt-1]" in text
    assert officer.last_sitrep is not None and officer.last_sitrep.unverified == []
    assert "".join(e["delta"] for e in events if e["t"] == "text") == text


async def test_mock_triage_writes_a_spotrep():
    store = seeded_store()
    officer = WatchOfficer(store, demo_settings(), force_mock=True)
    text = await officer.triage(store.alerts[0])
    assert text.startswith("SPOTREP alt-1") and "7. RECOMMENDED" in text and "adsb-emerg" in text


# ---- a fake Anthropic client ----------------------------------------------------------------------
class FakeStream:
    """Mimics `async with client.messages.stream(...) as s: async for ev in s: ...; await s.get_final_message()`."""

    def __init__(self, turn: dict) -> None:
        self.turn = turn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for block in self.turn["content"]:
                if block["type"] == "text":
                    for i in range(0, len(block["text"]), 7):
                        yield NS(type="content_block_delta", delta=NS(type="text_delta", text=block["text"][i:i + 7]))
                else:
                    yield NS(type="content_block_start", content_block=NS(type="tool_use", name=block["name"], id=block["id"]))
                    yield NS(type="content_block_delta", delta=NS(type="input_json_delta", partial_json=json.dumps(block["input"])))
                yield NS(type="content_block_stop")
        return gen()

    async def get_final_message(self):
        content = [NS(type="text", text=b["text"]) if b["type"] == "text" else NS(type="tool_use", name=b["name"], id=b["id"], input=b["input"])
                   for b in self.turn["content"]]
        return NS(content=content, stop_reason=self.turn["stop_reason"], usage=NS(input_tokens=100, output_tokens=20))


class FakeMessages:
    def __init__(self, turns: list[dict]) -> None:
        self.turns = list(turns)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.turns.pop(0))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return await FakeStream(self.turns.pop(0)).get_final_message()


class FakeClient:
    def __init__(self, turns: list[dict]) -> None:
        self.messages = FakeMessages(turns)


async def test_claude_loop_streams_tools_then_text_and_checks_citations():
    store = seeded_store()
    client = FakeClient([
        {"stop_reason": "tool_use", "content": [{"type": "text", "text": "Checking the picture. "},
                                                {"type": "tool_use", "name": "search_tracks", "id": "toolu_1", "input": {"flag": "EMERGENCY", "limit": 5}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "UPS556 is squawking 7700 [adsb-emerg]; nothing else is flagged. See [adsb-ghost]."}]},
    ])
    officer = WatchOfficer(store, demo_settings(), client=client)
    assert officer.mode.startswith("claude:")
    events = [ev async for ev in officer.ask_stream("Any emergencies?")]
    kinds = [e["t"] for e in events]
    assert kinds.index("tool") < kinds.index("tool_result") < kinds.index("done")
    tool_result = next(e for e in events if e["t"] == "tool_result")
    assert tool_result["tool"] == "search_tracks" and tool_result["input"] == {"flag": "EMERGENCY", "limit": 5}
    assert tool_result["result_summary"] == "1 track(s)"
    answer = events[-1]["answer"]
    assert answer["citations"] == ["adsb-emerg"] and answer["unverified"] == ["adsb-ghost"]
    assert "citation check" in answer["text"] and answer["tokens_in"] == 200 and answer["tokens_out"] == 40
    # the second model call carried the tool result back, with the real tool output inside
    second = client.messages.calls[1]
    assert second["messages"][-1]["role"] == "user"
    result_block = second["messages"][-1]["content"][0]
    assert result_block["type"] == "tool_result" and result_block["tool_use_id"] == "toolu_1" and "adsb-emerg" in result_block["content"]
    assert second["tools"] and second["system"]
    assert officer.calls == 1 and officer.tokens_in == 200


async def test_claude_sitrep_stream_and_triage_use_the_snapshot():
    store = seeded_store()
    client = FakeClient([
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "KESTREL SITREP — DTG 011200Z\n1. SITUATION — one emergency [adsb-emerg]."}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "SPOTREP alt-1\n1. WHAT: 7700 from UPS556 [adsb-emerg]"}]},
    ])
    officer = WatchOfficer(store, demo_settings(), client=client)
    sitrep = await officer.sitrep()
    assert sitrep.citations == ["adsb-emerg"] and sitrep.mode == "claude:claude-sonnet-5-5"
    snapshot_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "notable_tracks" in snapshot_prompt and "adsb-emerg" in snapshot_prompt
    spotrep = await officer.triage(store.alerts[0])
    assert spotrep.startswith("SPOTREP alt-1")
    triage_prompt = client.messages.calls[1]["messages"][0]["content"]
    assert "nearby" in triage_prompt and "feeds" in triage_prompt


async def test_model_errors_become_error_events_not_crashes():
    class BrokenMessages:
        def stream(self, **kwargs):
            raise ConnectionError("api down")

    officer = WatchOfficer(seeded_store(), demo_settings(), client=NS(messages=BrokenMessages()))
    events = [ev async for ev in officer.ask_stream("hello?")]
    assert events[-1]["t"] == "error" and "api down" in events[-1]["message"]
    try:
        await officer.ask("hello?")
    except RuntimeError as exc:
        assert "api down" in str(exc)
    else:
        raise AssertionError("ask() should raise on a model error")
