"""Shared test helpers: a demo Settings, a pipeline-less feed context, and a tiny ASGI client.

The ASGI client exists so the web layer can be tested without httpx (Starlette's TestClient needs it);
it speaks just enough of the protocol for GET/POST requests and server-sent events.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any

from kestrelcop.config import Settings
from kestrelcop.models import Event, Track


def demo_settings() -> Settings:
    s = Settings.for_demo()
    s.ai.auto_sitrep = False
    s.ai.triage_alerts = False
    return s


class RecordingContext:
    """A FeedContext that records what a feed emitted."""

    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self.events: list[Event] = []
        self.status: list[tuple] = []

    def ingest_track(self, track: Track) -> None:
        self.tracks.append(track)

    def ingest_event(self, event: Event) -> None:
        self.events.append(event)

    def feed_starting(self, name, label, mode="live", poll_seconds=None) -> None:
        self.status.append(("starting", name, mode))

    def feed_ok(self, name, label, items, mode="live", detail=None) -> None:
        self.status.append(("ok", name, items))

    def feed_error(self, name, label, error, mode="live") -> None:
        self.status.append(("error", name, error))

    def feed_off(self, name, label, reason) -> None:
        self.status.append(("off", name, reason))


class AsgiClient:
    """Minimal ASGI test client: runs the lifespan and issues HTTP requests to a Starlette app."""

    def __init__(self, app) -> None:
        self.app = app
        self._lifespan_task: asyncio.Task | None = None
        self._to_app: asyncio.Queue = asyncio.Queue()
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()

    async def __aenter__(self) -> "AsgiClient":
        async def receive():
            return await self._to_app.get()

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                self._started.set()
            elif message["type"] == "lifespan.shutdown.complete":
                self._stopped.set()
            elif message["type"].endswith("failed"):
                raise RuntimeError(message.get("message"))

        self._lifespan_task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
        await self._to_app.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self._started.wait(), 10)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._to_app.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self._stopped.wait(), 10)
        if self._lifespan_task:
            await self._lifespan_task

    async def request(self, method: str, path: str, body: Any = None, max_events: int | None = None,
                      timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
        """Returns (status, headers, body). For event streams, stops after `max_events` SSE messages."""
        raw_path, _, query = path.partition("?")
        payload = b"" if body is None else json.dumps(body).encode()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": urllib.parse.unquote(raw_path), "raw_path": raw_path.encode(), "query_string": query.encode(), "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }
        sent = {"body": False}
        status: dict[str, Any] = {"code": None, "headers": {}}
        chunks: list[bytes] = []
        done = asyncio.Event()

        async def receive():
            if not sent["body"]:
                sent["body"] = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                status["headers"] = {k.decode(): v.decode() for k, v in message.get("headers", [])}
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    done.set()
                elif max_events is not None and b"".join(chunks).count(b"\n\n") >= max_events:
                    done.set()  # receive() now reports a disconnect and Starlette stops the stream

        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(done.wait(), timeout)
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return status["code"], status["headers"], b"".join(chunks)

    async def get_json(self, path: str) -> Any:
        code, _, body = await self.request("GET", path)
        assert code == 200, (code, body[:200])
        return json.loads(body)

    async def sse(self, path: str, max_events: int = 50) -> list[dict]:
        code, headers, body = await self.request("GET", path, max_events=max_events)
        assert code == 200, (code, body[:200])
        assert headers.get("content-type", "").startswith("text/event-stream"), headers
        events = []
        for block in body.decode().split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

