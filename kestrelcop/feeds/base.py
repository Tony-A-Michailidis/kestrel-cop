"""Feed plumbing shared by every adapter: a poll loop with backoff, health reporting, and emit helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from .. import __version__
from ..config import Settings
from ..models import Event, Track

log = logging.getLogger("kestrel.feeds")

USER_AGENT = f"kestrel-cop/{__version__} (+https://github.com/Tony-A-Michailidis/kestrel-cop)"


class FeedContext(Protocol):
    """What a feed needs from the pipeline."""

    def ingest_track(self, track: Track) -> None: ...
    def ingest_event(self, event: Event) -> None: ...
    def feed_starting(self, name: str, label: str, mode: str = "live", poll_seconds: float | None = None) -> None: ...
    def feed_ok(self, name: str, label: str, items: int, mode: str = "live", detail: str | None = None) -> None: ...
    def feed_error(self, name: str, label: str, error: str, mode: str = "live") -> None: ...
    def feed_off(self, name: str, label: str, reason: str) -> None: ...


class Feed:
    name = "base"
    label = "Base feed"
    mode = "live"

    def __init__(self, ctx: FeedContext, settings: Settings, poll_seconds: float = 30.0) -> None:
        self.ctx = ctx
        self.settings = settings
        self.poll_seconds = poll_seconds
        self._http: Any | None = None

    @property
    def http(self) -> Any:
        """An httpx.AsyncClient, created on first use so the demo world runs without httpx installed."""
        if self._http is None:
            import httpx  # imported lazily: only live feeds need it

            self._http = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def poll(self) -> int:
        """Fetch once, emit what was found, return the number of items. Raise on failure."""
        raise NotImplementedError

    async def run(self) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:
            self.ctx.feed_off(self.name, self.label, "python package 'httpx' is not installed")
            return
        self.ctx.feed_starting(self.name, self.label, self.mode, poll_seconds=self.poll_seconds)
        backoff = self.poll_seconds
        try:
            while True:
                try:
                    n = await self.poll()
                    self.ctx.feed_ok(self.name, self.label, n, self.mode)
                    backoff = self.poll_seconds
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a bad poll must never kill the picture
                    msg = f"{type(exc).__name__}: {exc}"[:200]
                    log.warning("%s: %s", self.name, msg)
                    self.ctx.feed_error(self.name, self.label, msg, self.mode)
                    backoff = min(backoff * 2, 300.0)
                await asyncio.sleep(backoff)
        finally:
            await self.aclose()

    def emit_track(self, track: Track) -> None:
        self.ctx.ingest_track(track)

    def emit_event(self, event: Event) -> None:
        self.ctx.ingest_event(event)


def bbox(center: tuple[float, float], radius_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) box around a centre, in degrees."""
    import math
    lat, lon = center
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon
