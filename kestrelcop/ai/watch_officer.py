"""The AI watch officer: SITREPs, questions about the picture, and alert triage.

Three guard rails, borrowed from the chess arena's referee and the endurance analyst's data audit:
  * the model only ever sees a snapshot the server built, never raw feed data or the open internet;
  * every UID the model cites is checked against the store and unverifiable citations are reported;
  * without an API key the mock officer runs instead, clearly labelled, so nothing silently pretends.

Both the SITREP and the question path are streams: the browser sees tool calls as they happen and
text as it is produced. The non-streaming methods simply drain the same generators.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator

from ..config import Settings
from ..models import Alert, Answer, Sitrep, utcnow
from ..store import Store
from . import mock as mock_officer
from .prompts import ASK_SYSTEM, SITREP_SYSTEM, TRIAGE_SYSTEM
from .tools import TOOL_SPECS, PictureTools, compact_alert, compact_event, compact_track

log = logging.getLogger("kestrel.ai")

CITATION_RE = re.compile(r"\[((?:adsb|ais|nws|firms|usgs|alt|zone|kestrel)-[^\]\s]+)\]")
MOCK_TOOL_DELAY = 0.25      # seconds; the mock officer paces itself so the trace is readable
MOCK_TEXT_DELAY = 0.012     # per chunk

# Current models think before they answer (adaptive thinking is on by default), and thinking tokens count
# against max_tokens. The caps leave room for the reasoning and the full report; streaming keeps the
# connection alive while the model works.
THINKING = {"type": "adaptive"}
SITREP_MAX_TOKENS = 16000
ASK_MAX_TOKENS = 16000
TRIAGE_MAX_TOKENS = 8000


def _thinking_kwargs(model: str) -> dict[str, Any]:
    """Adaptive thinking exists on Opus and Sonnet 4.6 and later and on the 5-series; Haiku 4.5 and older models
    reject it with a 400, so they are called without a thinking parameter."""
    m = model.lower()
    if "haiku" in m:
        return {}
    version = re.search(r"-(\d+)-(\d+)", m)   # claude-sonnet-5-5 -> (5, 5); claude-opus-4-1 -> (4, 1)
    if version and (int(version.group(1)), int(version.group(2))) < (4, 6):
        return {}
    return {"thinking": THINKING}


def verify_citations(text: str, store: Store) -> tuple[list[str], list[str]]:
    """Every [uid] in the text is checked against the picture. Returns (verified, unverified), deduplicated."""
    seen: list[str] = []
    for m in CITATION_RE.finditer(text):
        uid = m.group(1)
        if uid not in seen:
            seen.append(uid)
    known = set(store.tracks) | set(store.events) | {a.id for a in store.alerts}
    verified = [u for u in seen if u in known]
    unverified = [u for u in seen if u not in known]
    return verified, unverified


def _chunks(text: str, size: int = 6) -> list[str]:
    """Split text into small word-ish chunks so a templated answer still streams like a model."""
    words = re.split(r"(\s+)", text)
    out: list[str] = []
    buf = ""
    for w in words:
        buf += w
        if len(buf) >= size and not w.isspace():
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


class WatchOfficer:
    def __init__(self, store: Store, settings: Settings, client: Any | None = None, force_mock: bool | None = None) -> None:
        self.store = store
        self.settings = settings
        self.tools = PictureTools(store, settings)
        self.model = settings.ai.model
        self.triage_model = settings.ai.triage_model or settings.ai.model
        self.client = client
        if self.client is None and force_mock is not True and os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic  # imported lazily so the mock path needs no SDK at import time

            self.client = anthropic.AsyncAnthropic()
        self.mock = self.client is None or force_mock is True
        self.last_sitrep: Sitrep | None = None
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    @property
    def mode(self) -> str:
        return "mock" if self.mock else f"claude:{self.model}"

    def status(self) -> dict[str, Any]:
        return {"enabled": self.settings.ai.enabled, "mode": self.mode, "model": None if self.mock else self.model,
                "calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "last_sitrep_at": self.last_sitrep.generated_at.isoformat() if self.last_sitrep else None}

    # ---- snapshot the model is allowed to see ---------------------------------------------------
    def picture_for_model(self, max_tracks: int = 40) -> dict[str, Any]:
        now = utcnow()
        summary = self.tools.tool_picture_summary()
        tracks = list(self.store.tracks.values())
        # flagged first, then military, then by speed; the model sees what a watch officer would look at first
        tracks.sort(key=lambda t: (-len(t.flags), t.affiliation != "friend", -(t.speed_kts or 0)))
        notable = [compact_track(t, now) for t in tracks[:max_tracks]]
        events = sorted(self.store.events.values(), key=lambda e: e.ts, reverse=True)
        return {
            **summary,
            "notable_tracks": notable,
            "tracks_not_shown": max(0, len(tracks) - len(notable)),
            "events": [compact_event(e, now) for e in events[:30]],
            "alerts": [compact_alert(a, now) for a in self.store.alerts[-15:]],
        }

    # ---- SITREP ---------------------------------------------------------------------------------
    async def sitrep_stream(self) -> AsyncIterator[dict[str, Any]]:
        """Events: {"t":"text","delta"} ... {"t":"done","sitrep":{...}} or {"t":"error","message"}."""
        picture = self.picture_for_model()
        text = ""
        tokens = (0, 0)
        try:
            if self.mock:
                full = mock_officer.mock_sitrep(picture, self.tools)
                for chunk in _chunks(full, 10):
                    text += chunk
                    yield {"t": "text", "delta": chunk}
                    await asyncio.sleep(MOCK_TEXT_DELAY)
            else:
                async with self.client.messages.stream(
                    model=self.model, max_tokens=SITREP_MAX_TOKENS, **_thinking_kwargs(self.model), system=SITREP_SYSTEM,
                    messages=[{"role": "user", "content": "Current picture snapshot (JSON):\n"
                               + json.dumps(picture, default=str) + "\n\nWrite the SITREP now."}],
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                            text += event.delta.text
                            yield {"t": "text", "delta": event.delta.text}
                    final = await stream.get_final_message()
                tokens = (final.usage.input_tokens, final.usage.output_tokens)
                self._count(tokens)
        except Exception as exc:  # noqa: BLE001 - the picture must outlive a model error
            log.warning("sitrep failed: %s", exc)
            yield {"t": "error", "message": f"{type(exc).__name__}: {exc}"[:300]}
            return
        verified, unverified = verify_citations(text, self.store)
        if unverified:
            note = f"\n\n[citation check: {len(unverified)} UID(s) not on the picture: {', '.join(unverified)}]"
            text += note
            yield {"t": "text", "delta": note}
            log.warning("sitrep cited unknown UIDs: %s", unverified)
        self.last_sitrep = Sitrep(text=text, mode=self.mode, citations=verified, unverified=unverified,
                                  tokens_in=tokens[0], tokens_out=tokens[1])
        yield {"t": "done", "sitrep": self.last_sitrep.model_dump(mode="json")}

    async def sitrep(self) -> Sitrep:
        async for ev in self.sitrep_stream():
            if ev["t"] == "error":
                raise RuntimeError(ev["message"])
        assert self.last_sitrep is not None
        return self.last_sitrep

    # ---- questions ------------------------------------------------------------------------------
    async def ask_stream(self, question: str) -> AsyncIterator[dict[str, Any]]:
        """Events: tool / tool_result / text / done(answer) / error. The trace is real in both modes."""
        question = question.strip()[:1000]
        if not question:
            yield {"t": "error", "message": "empty question"}
            return
        trace: list[dict[str, Any]] = []
        text = ""
        tokens = (0, 0)
        try:
            if self.mock:
                full, steps = mock_officer.mock_ask(question, self.tools)
                for step in steps:
                    yield {"t": "tool", "tool": step["tool"], "input": step["input"]}
                    await asyncio.sleep(MOCK_TOOL_DELAY)
                    trace.append(step)
                    yield {"t": "tool_result", **step, "ms": 1}
                for chunk in _chunks(full):
                    text += chunk
                    yield {"t": "text", "delta": chunk}
                    await asyncio.sleep(MOCK_TEXT_DELAY)
            else:
                async for ev in self._ask_claude_stream(question, trace):
                    if ev["t"] == "text":
                        text += ev["delta"]
                    elif ev["t"] == "usage":
                        tokens = (tokens[0] + ev["in"], tokens[1] + ev["out"])
                        continue
                    yield ev
                self._count(tokens)
        except Exception as exc:  # noqa: BLE001
            log.warning("ask failed: %s", exc)
            yield {"t": "error", "message": f"{type(exc).__name__}: {exc}"[:300]}
            return
        verified, unverified = verify_citations(text, self.store)
        if unverified:
            note = f"\n\n[citation check: not on the picture: {', '.join(unverified)}]"
            text += note
            yield {"t": "text", "delta": note}
        answer = Answer(question=question, text=text, mode=self.mode, citations=verified, unverified=unverified,
                        trace=trace, tokens_in=tokens[0], tokens_out=tokens[1])
        yield {"t": "done", "answer": answer.model_dump(mode="json")}

    async def ask(self, question: str) -> Answer:
        answer: Answer | None = None
        async for ev in self.ask_stream(question):
            if ev["t"] == "error":
                raise RuntimeError(ev["message"])
            if ev["t"] == "done":
                answer = Answer.model_validate(ev["answer"])
        assert answer is not None
        return answer

    async def _ask_claude_stream(self, question: str, trace: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """The agentic loop: stream a turn, run any tools it asked for, feed the results back, repeat."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        for iteration in range(self.settings.ai.max_tool_iterations):
            async with self.client.messages.stream(
                model=self.model, max_tokens=ASK_MAX_TOKENS, **_thinking_kwargs(self.model), system=ASK_SYSTEM, tools=TOOL_SPECS,
                messages=messages,
            ) as stream:
                async for event in stream:
                    etype = event.type
                    if etype == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
                        yield {"t": "tool", "tool": event.content_block.name, "input": None}
                    elif etype == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        yield {"t": "text", "delta": event.delta.text}
                final = await stream.get_final_message()
            yield {"t": "usage", "in": final.usage.input_tokens, "out": final.usage.output_tokens}
            if final.stop_reason != "tool_use":
                return
            messages.append({"role": "assistant", "content": final.content})
            results = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                args = dict(block.input or {})
                started = time.perf_counter()
                result = self.tools.call(block.name, args)
                step = {"tool": block.name, "input": args, "result_summary": self.tools.summarize(block.name, result)}
                trace.append(step)
                yield {"t": "tool_result", **step, "ms": round((time.perf_counter() - started) * 1000)}
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(result, default=str)[:16000]})
            messages.append({"role": "user", "content": results})
            if iteration == self.settings.ai.max_tool_iterations - 1:
                yield {"t": "text", "delta": "\n\nI ran out of tool calls before reaching an answer; please narrow the question."}

    # ---- alert triage ---------------------------------------------------------------------------
    def _triage_context(self, alert: Alert) -> dict[str, Any]:
        subject: dict[str, Any] = {}
        nearby: list[dict[str, Any]] = []
        if alert.subject_uid:
            t = self.store.tracks.get(alert.subject_uid)
            if t:
                subject = compact_track(t)
                subject["attrs"] = t.attrs
            else:
                e = self.store.events.get(alert.subject_uid)
                if e:
                    subject = compact_event(e)
        if alert.lat is not None and alert.lon is not None:
            res = self.tools.tool_search_tracks(near={"lat": alert.lat, "lon": alert.lon, "km": 30}, limit=8)
            nearby = [t for t in res["tracks"] if t["uid"] != alert.subject_uid]
        zones = [compact_event(e) for e in self.store.events.values() if e.kind == "zone"]
        return {"alert": compact_alert(alert), "subject": subject, "nearby": nearby, "zones": zones,
                "feeds": self.tools.tool_feed_health()["feeds"]}

    async def triage(self, alert: Alert) -> str:
        context = self._triage_context(alert)
        if self.mock:
            return mock_officer.mock_triage(alert, context)
        resp = await self.client.messages.create(
            model=self.triage_model, max_tokens=TRIAGE_MAX_TOKENS, **_thinking_kwargs(self.triage_model), system=TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": "Alert and context (JSON):\n" + json.dumps(context, default=str)
                       + "\n\nWrite the SPOTREP now."}],
        )
        self._count((resp.usage.input_tokens, resp.usage.output_tokens))
        text = "".join(getattr(b, "text", "") for b in resp.content)
        _, unverified = verify_citations(text, self.store)
        if unverified:
            text += f"\n[citation check: not on the picture: {', '.join(unverified)}]"
        return text

    def _count(self, tokens: tuple[int, int]) -> None:
        self.calls += 1
        self.tokens_in += tokens[0]
        self.tokens_out += tokens[1]
