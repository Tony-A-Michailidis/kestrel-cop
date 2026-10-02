"""HTTP + streaming front door (Starlette). The browser gets a full snapshot on connect, then deltas.

Two equivalent push transports: a WebSocket at /ws and Server-Sent Events at /api/stream. The browser
prefers the WebSocket and falls back to SSE, which also survives proxies that strip Upgrade headers.

Starlette rather than FastAPI on purpose: it is the layer FastAPI sits on, needs no extra dependencies,
and this surface is small enough that request validation by hand is clearer than a schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import __version__
from ..config import Settings, area_override_path
from ..cot import event_to_cot, track_to_cot
from ..models import Event, FeedStatus, Sitrep, Track
from ..pipeline import Pipeline

log = logging.getLogger("kestrel.web")
STATIC_DIR = Path(__file__).parent / "static"
VENDOR_DIR = STATIC_DIR / "vendor"

# Browser libraries. Served from static/vendor/ when the files are there (tools/vendor.sh puts them
# there for offline use); otherwise the request is redirected to the pinned CDN copy.
VENDOR_CDN = {
    "maplibre-gl.js": "https://unpkg.com/maplibre-gl@5.8.0/dist/maplibre-gl.js",
    "maplibre-gl.css": "https://unpkg.com/maplibre-gl@5.8.0/dist/maplibre-gl.css",
    "milsymbol.js": "https://unpkg.com/milsymbol@3.0.4/dist/milsymbol.js",
}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _json(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, default=str), status_code=status, media_type="application/json")


def _error(message: str, status: int) -> Response:
    return JSONResponse({"error": message}, status_code=status)


class Broadcaster:
    """Fans bus messages out to connected browsers, coalescing track updates into small batches.

    Every client owns a queue; a slow client drops its oldest messages rather than slowing the others.
    """

    def __init__(self, pipeline: Pipeline, batch_ms: int = 300, client_queue: int = 500) -> None:
        self.pipeline = pipeline
        self.clients: set[asyncio.Queue] = set()
        self.batch_s = batch_ms / 1000.0
        self.client_queue = client_queue
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.messages_sent = 0

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.client_queue)
        self.clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="broadcaster")

    async def stop(self) -> None:
        self._stopping = True  # belt and braces: on Python 3.11 a cancel can be swallowed by a wait that completes at the same instant
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        q = self.pipeline.bus.subscribe()
        pending_tracks: dict[str, Track] = {}
        pending_events: dict[str, Event] = {}
        pending_feeds: dict[str, FeedStatus] = {}
        loop = asyncio.get_event_loop()
        last_flush = loop.time()
        try:
            while not self._stopping:
                timeout = max(0.01, self.batch_s - (loop.time() - last_flush))
                try:
                    # asyncio.timeout rather than wait_for: on Python 3.11 wait_for can swallow the task's cancellation
                    # when the queue delivers at the same moment, and then the broadcaster never shuts down
                    async with asyncio.timeout(timeout):
                        topic, payload = await q.get()
                    if topic == "track":
                        pending_tracks[payload.uid] = payload
                    elif topic == "event":
                        pending_events[payload.uid] = payload
                    elif topic == "feed":
                        pending_feeds[payload.name] = payload
                    elif topic == "alert":
                        self.send({"t": "alert", "item": payload.model_dump(mode="json")})
                    elif topic == "remove":
                        self.send({"t": "remove", **payload})
                    elif topic == "sitrep":
                        self.send({"t": "sitrep", "item": payload.model_dump(mode="json")})
                    elif topic == "settings":
                        self.send({"t": "settings", "settings": public_settings(self.pipeline.settings, self.pipeline)})
                except asyncio.TimeoutError:
                    pass
                if loop.time() - last_flush >= self.batch_s:
                    if pending_tracks:
                        self.send({"t": "tracks", "items": [t.model_dump(mode="json") for t in pending_tracks.values()]})
                        pending_tracks.clear()
                    if pending_events:
                        self.send({"t": "events", "items": [e.model_dump(mode="json") for e in pending_events.values()]})
                        pending_events.clear()
                    if pending_feeds:
                        self.send({"t": "feeds", "items": [f.model_dump(mode="json") for f in pending_feeds.values()]})
                        pending_feeds.clear()
                    last_flush = loop.time()
        finally:
            self.pipeline.bus.unsubscribe(q)

    def send(self, message: dict[str, Any]) -> None:
        if not self.clients:
            return
        text = json.dumps(message, default=str)
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(text)
            self.messages_sent += 1


def public_settings(settings: Settings, pipeline: Pipeline) -> dict[str, Any]:
    return {
        "version": __version__,
        "area": {"name": settings.area.name, "center": list(settings.area.center), "radius_nm": settings.area.radius_nm,
                 "radius_km": round(settings.area.radius_km, 1)},
        "demo": settings.demo.enabled,
        "ai": pipeline.watch_officer.status(),
        "tak": {"enabled": settings.tak.enabled, "url": settings.tak.url if settings.tak.enabled else None},
        "basemap_style": settings.web.basemap_style,
        "zones": [z.model_dump() for z in settings.rules.zones],
        "stale_seconds": {"air": settings.store.stale_seconds_air, "sea": settings.store.stale_seconds_sea},
    }


def create_app(settings: Settings, pipeline: Pipeline | None = None) -> Starlette:
    pipeline = pipeline or Pipeline(settings)
    broadcaster = Broadcaster(pipeline)
    store = pipeline.store
    officer = pipeline.watch_officer

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await pipeline.start()
        broadcaster.start()
        try:
            yield
        finally:
            await broadcaster.stop()
            await pipeline.stop()

    def hello_message() -> str:
        hello = {"t": "hello", "settings": public_settings(settings, pipeline), "snapshot": store.snapshot()}
        if officer.last_sitrep:
            hello["sitrep"] = officer.last_sitrep.model_dump(mode="json")
        return json.dumps(hello, default=str)

    async def index(request: Request) -> Response:
        # no-store: a browser must never show a cached console while the server is down (the page would then
        # load but its libraries would not, and the map would stay blank once the data stream reconnects)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def health(request: Request) -> Response:
        return _json({"ok": True, "version": __version__, "clients": len(broadcaster.clients),
                      "messages_sent": broadcaster.messages_sent, **pipeline.status()})

    async def get_settings(request: Request) -> Response:
        return _json(public_settings(settings, pipeline))

    async def get_area(request: Request) -> Response:
        ps = public_settings(settings, pipeline)
        return _json({"area": ps["area"], "zones": ps["zones"],
                      "persisted_to": str(area_override_path(settings.source_path)) if settings.source_path else None})

    async def post_area(request: Request) -> Response:
        """Move the picture. Body: {"name"?: str, "center": [lat, lon], "radius_nm"?: number, "keep_zones"?: bool}."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error("body must be JSON", 400)
        center = body.get("center") if isinstance(body, dict) else None
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            return _error("center must be [lat, lon]", 400)
        try:
            result = await pipeline.set_area(body.get("name"), (float(center[0]), float(center[1])),
                                             body.get("radius_nm"), bool(body.get("keep_zones", False)))
        except (TypeError, ValueError) as exc:
            return _error(str(exc), 400)
        return _json(result)

    async def snapshot(request: Request) -> Response:
        return _json(store.snapshot())

    async def tracks(request: Request) -> Response:
        return _json([t.model_dump(mode="json") for t in store.tracks.values()])

    async def track(request: Request) -> Response:
        t = store.get_track(request.path_params["uid"])
        if not t:
            return _error(f"no track {request.path_params['uid']}", 404)
        return _json(t.model_dump(mode="json"))

    async def events(request: Request) -> Response:
        return _json([e.model_dump(mode="json") for e in store.events.values()])

    async def alerts(request: Request) -> Response:
        limit = int(request.query_params.get("limit", "50"))
        return _json([a.model_dump(mode="json") for a in store.alerts[-limit:]])

    async def feeds(request: Request) -> Response:
        return _json([f.model_dump(mode="json") for f in store.feeds.values()])

    async def cot(request: Request) -> Response:
        """The exact CoT XML Kestrel would send to a TAK server for this track or event."""
        uid = request.path_params["uid"]
        t = store.get_track(uid)
        if t:
            stale = settings.tak.stale_seconds_air if t.domain == "air" else settings.tak.stale_seconds_sea
            return Response(track_to_cot(t, stale), media_type="application/xml")
        e = store.events.get(uid)
        if e:
            return Response(event_to_cot(e), media_type="application/xml")
        return _error(f"no track or event {uid}", 404)

    async def last_sitrep(request: Request) -> Response:
        if not officer.last_sitrep:
            return _error("no SITREP generated yet", 404)
        return _json(officer.last_sitrep.model_dump(mode="json"))

    async def make_sitrep(request: Request) -> Response:
        if not settings.ai.enabled:
            return _error("AI disabled in config", 503)
        try:
            s: Sitrep = await officer.sitrep()
        except RuntimeError as exc:
            return _error(str(exc), 502)
        pipeline.bus.publish("sitrep", s)
        return _json(s.model_dump(mode="json"))

    async def ask(request: Request) -> Response:
        if not settings.ai.enabled:
            return _error("AI disabled in config", 503)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error("body must be JSON: {\"question\": \"...\"}", 400)
        question = str((body or {}).get("question", "")).strip()
        if not question:
            return _error("empty question", 400)
        try:
            answer = await officer.ask(question)
        except RuntimeError as exc:
            return _error(str(exc), 502)
        return _json(answer.model_dump(mode="json"))

    def _sse(events) -> Response:
        async def gen():
            async for ev in events:
                yield f"data: {json.dumps(ev, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def ask_stream(request: Request) -> Response:
        """Server-Sent Events: tool calls as they happen, text as it is produced, then the verified answer."""
        if not settings.ai.enabled:
            return _error("AI disabled in config", 503)
        question = request.query_params.get("q", "").strip()
        if not question:
            return _error("missing ?q=", 400)
        return _sse(officer.ask_stream(question))

    async def sitrep_stream(request: Request) -> Response:
        if not settings.ai.enabled:
            return _error("AI disabled in config", 503)

        async def events():
            async for ev in officer.sitrep_stream():
                if ev["t"] == "done":
                    pipeline.bus.publish("sitrep", officer.last_sitrep)
                yield ev

        return _sse(events())

    async def vendor(request: Request) -> Response:
        name = request.path_params["name"]
        local = VENDOR_DIR / name
        if local.is_file():
            return FileResponse(local)
        if name in VENDOR_CDN:
            return RedirectResponse(VENDOR_CDN[name], status_code=302)
        return _error(f"no vendored file {name}", 404)

    async def stream(request: Request) -> Response:
        """Server-Sent Events: the same messages as the WebSocket, for clients that cannot upgrade."""
        q = broadcaster.register()

        async def gen():
            try:
                yield f"data: {hello_message()}\n\n"
                while True:
                    try:
                        async with asyncio.timeout(15):  # not wait_for: see Broadcaster._run
                            text = await q.get()
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {text}\n\n"
            finally:
                broadcaster.unregister(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        q = broadcaster.register()

        async def pump() -> None:
            while True:
                await ws.send_text(await q.get())

        pump_task = asyncio.create_task(pump())
        try:
            await ws.send_text(hello_message())
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text('{"t":"pong"}')
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("websocket closed: %s", exc)
        finally:
            pump_task.cancel()
            broadcaster.unregister(q)

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/settings", get_settings),
        Route("/api/area", get_area, methods=["GET"]),
        Route("/api/area", post_area, methods=["POST"]),
        Route("/api/snapshot", snapshot),
        Route("/api/tracks", tracks),
        Route("/api/tracks/{uid}", track),
        Route("/api/events", events),
        Route("/api/alerts", alerts),
        Route("/api/feeds", feeds),
        Route("/api/cot/{uid}", cot),
        Route("/api/stream", stream),
        Route("/api/ai/sitrep", last_sitrep, methods=["GET"]),
        Route("/api/ai/sitrep", make_sitrep, methods=["POST"]),
        Route("/api/ai/sitrep/stream", sitrep_stream),
        Route("/api/ai/ask", ask, methods=["POST"]),
        Route("/api/ai/ask/stream", ask_stream),
        WebSocketRoute("/ws", ws_endpoint),
        Route("/static/vendor/{name}", vendor),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(GZipMiddleware, minimum_size=1500)])
    app.state.pipeline = pipeline
    app.state.settings = settings
    app.state.broadcaster = broadcaster
    return app
