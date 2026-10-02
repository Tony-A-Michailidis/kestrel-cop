"""The pipeline wires feeds -> store -> rules -> bus -> (web, TAK, watch officer). One instance per process."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import timedelta
from typing import Any, Callable

from .ai import WatchOfficer
from .bus import Bus
from .config import AreaConfig, Settings
from .cot.sidc import EVENT_COT_TYPES
from .cot.tak import TakPublisher
from .feeds import AdsbFeed, AisFeed, DemoFeed, Feed, FirmsFeed, NwsFeed, UsgsFeed
from .geo import destination, haversine_km
from .models import Alert, Event, FeedStatus, Track, utcnow
from .rules import RuleEngine
from .store import Store

log = logging.getLogger("kestrel.pipeline")


def zone_events(settings: Settings) -> list[Event]:
    """Configured zones become events so every consumer (map, TAK, watch officer) sees them the same way."""
    out = []
    for z in settings.rules.zones:
        slug = re.sub(r"[^a-z0-9]+", "-", z.name.lower()).strip("-")
        if z.type == "circle" and z.center and z.radius_km:
            ring = [list(reversed(destination(z.center[0], z.center[1], b, z.radius_km))) for b in range(0, 360, 6)]
            ring.append(ring[0])
            out.append(Event(uid=f"zone-{slug}", kind="zone", source="config", title=z.name, lat=z.center[0], lon=z.center[1],
                             geometry={"type": "Polygon", "coordinates": [ring]}, severity=z.severity,
                             cot_type=EVENT_COT_TYPES["zone"], attrs={"radius_km": z.radius_km, "zone_type": "circle"}))
        elif z.type == "polygon" and z.ring:
            lat = sum(p[1] for p in z.ring) / len(z.ring)
            lon = sum(p[0] for p in z.ring) / len(z.ring)
            ring = list(z.ring)
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            out.append(Event(uid=f"zone-{slug}", kind="zone", source="config", title=z.name, lat=lat, lon=lon,
                             geometry={"type": "Polygon", "coordinates": [ring]}, severity=z.severity,
                             cot_type=EVENT_COT_TYPES["alert"], attrs={"zone_type": "polygon"}))
    return out


class Pipeline:
    def __init__(self, settings: Settings, store: Store | None = None, bus: Bus | None = None,
                 watch_officer: WatchOfficer | None = None) -> None:
        self.settings = settings
        self.store = store or Store(settings.store.stale_seconds_air, settings.store.stale_seconds_sea, settings.store.trail_length)
        self.bus = bus or Bus()
        self.rules = RuleEngine(self.store, settings.rules)
        self.watch_officer = watch_officer or WatchOfficer(self.store, settings)
        self.tak = TakPublisher(settings.tak, self.bus) if settings.tak.enabled else None
        self.feeds: list[Feed] = []
        self._tasks: list[asyncio.Task] = []
        self._feed_tasks: list[asyncio.Task] = []
        self._rates: dict[str, deque[tuple[float, int]]] = {}
        self._labels: dict[str, str] = {}
        self.started_at = None
        self.loop_time: Callable[[], float] = lambda: asyncio.get_event_loop().time()

    # ---- FeedContext --------------------------------------------------------------------------
    def ingest_track(self, track: Track) -> None:
        track, prev = self.store.upsert_track(track)
        for alert in self.rules.on_track(track, prev):
            self.raise_alert(alert)
        self.bus.publish("track", track)

    def ingest_event(self, event: Event) -> None:
        event, is_new = self.store.upsert_event(event)
        for alert in self.rules.on_event(event, is_new):
            self.raise_alert(alert)
        self.bus.publish("event", event)

    def _status(self, name: str, label: str) -> FeedStatus:
        fs = self.store.feeds.get(name)
        if fs is None:
            fs = FeedStatus(name=name, label=label)
            self.store.set_feed(fs)
        self._labels[name] = label
        return fs

    def feed_starting(self, name: str, label: str, mode: str = "live", poll_seconds: float | None = None) -> None:
        fs = self._status(name, label)
        fs.mode, fs.status, fs.error = mode, "starting", None
        if poll_seconds:
            # a polled feed is only stale once two polls in a row have brought nothing: a quiet 180 s feed must
            # not flap against a 120 s global window
            fs.stale_after_s = max(self.settings.rules.stale_feed_seconds, 2 * poll_seconds + 30)
        self.bus.publish("feed", fs)

    def feed_ok(self, name: str, label: str, items: int, mode: str = "live", detail: str | None = None) -> None:
        fs = self._status(name, label)
        now = utcnow()
        fs.mode, fs.status, fs.error, fs.detail = mode, "ok", None, detail
        fs.last_message = now
        fs.messages_total += items
        fs.items = items if items or fs.items == 0 else fs.items
        window = self._rates.setdefault(name, deque())
        t = self.loop_time()
        window.append((t, items))
        while window and t - window[0][0] > 60:
            window.popleft()
        fs.rate_per_min = float(sum(n for _, n in window))
        self.bus.publish("feed", fs)

    def feed_error(self, name: str, label: str, error: str, mode: str = "live") -> None:
        fs = self._status(name, label)
        fs.mode, fs.error = mode, error
        fs.status = "down" if fs.last_message is None else "degraded"
        self.bus.publish("feed", fs)

    def feed_off(self, name: str, label: str, reason: str) -> None:
        fs = self._status(name, label)
        fs.mode, fs.status, fs.detail = "off", "off", reason
        self.bus.publish("feed", fs)

    # ---- alerts -------------------------------------------------------------------------------
    def raise_alert(self, alert: Alert) -> None:
        self.store.add_alert(alert)
        log.info("ALERT %s %s: %s", alert.id, alert.severity, alert.title)
        self.bus.publish("alert", alert)
        if self.settings.ai.enabled and self.settings.ai.triage_alerts:
            self._tasks = [t for t in self._tasks if not t.done()]
            self._tasks.append(asyncio.create_task(self._triage(alert)))

    async def _triage(self, alert: Alert) -> None:
        try:
            alert.triage = await self.watch_officer.triage(alert)
            alert.triage_mode = self.watch_officer.mode
            self.bus.publish("alert", alert)
        except Exception as exc:  # noqa: BLE001
            log.warning("triage failed for %s: %s", alert.id, exc)

    # ---- lifecycle ----------------------------------------------------------------------------
    def build_feeds(self) -> list[Feed]:
        if self.settings.demo.enabled:
            return [DemoFeed(self, self.settings)]
        feeds: list[Feed] = []
        f = self.settings.feeds
        if f.adsb.enabled:
            feeds.append(AdsbFeed(self, self.settings))
        else:
            self.feed_off("adsb", "ADS-B aircraft", "disabled in config")
        if f.ais.enabled:
            feeds.append(AisFeed(self, self.settings))
        else:
            self.feed_off("ais", "AIS vessels", "disabled in config")
        if f.nws.enabled:
            feeds.append(NwsFeed(self, self.settings))
        else:
            self.feed_off("nws", "NWS weather alerts", "disabled in config")
        if f.firms.enabled:
            feeds.append(FirmsFeed(self, self.settings))
        else:
            self.feed_off("firms", "NASA FIRMS fires", "disabled in config")
        if f.usgs.enabled:
            feeds.append(UsgsFeed(self, self.settings))
        else:
            self.feed_off("usgs", "USGS earthquakes", "disabled in config")
        return feeds

    async def start(self) -> None:
        self.started_at = utcnow()
        for ev in zone_events(self.settings):
            self.ingest_event(ev)
        self._start_feeds()
        self._tasks.append(asyncio.create_task(self._periodic(), name="periodic"))
        if self.tak:
            self._tasks.append(asyncio.create_task(self.tak.run(), name="tak"))
        if self.settings.ai.enabled and self.settings.ai.auto_sitrep:
            self._tasks.append(asyncio.create_task(self._sitrep_loop(), name="sitrep"))
        log.info("pipeline started: %s feeds, demo=%s, ai=%s, tak=%s", len(self.feeds), self.settings.demo.enabled,
                 self.watch_officer.mode, self.settings.tak.url if self.tak else "off")

    async def stop(self) -> None:
        await self._stop_feeds()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---- the area ------------------------------------------------------------------------------
    def _start_feeds(self) -> None:
        self.feeds = self.build_feeds()
        self._feed_tasks = [asyncio.create_task(feed.run(), name=f"feed:{feed.name}") for feed in self.feeds]

    async def _stop_feeds(self) -> None:
        for t in self._feed_tasks:
            t.cancel()
        await asyncio.gather(*self._feed_tasks, return_exceptions=True)
        self._feed_tasks = []

    async def set_area(self, name: str | None, center: tuple[float, float], radius_nm: float | None = None,
                       keep_zones: bool = False) -> dict[str, Any]:
        """Move the picture at runtime.

        The feeds are restarted against the new centre and radius (AIS subscribes with a bounding box, the demo
        world is generated around its centre), tracks outside the new area and events far outside it are dropped
        so the old picture does not linger, zones are cleared because they are absolute coordinates (unless asked
        to keep them), and the change is saved next to the config so a restart keeps it. Raises ValueError on bad
        input; nothing is changed in that case.
        """
        lat, lon = float(center[0]), float(center[1])
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError("center must be [lat, lon] in decimal degrees")
        radius = float(radius_nm) if radius_nm is not None else self.settings.area.radius_nm
        if not (5 <= radius <= 1000):
            raise ValueError("radius_nm must be between 5 and 1000")
        name = (name or "").strip() or f"{lat:.2f}, {lon:.2f}"
        old = self.settings.area
        self.settings.area = AreaConfig(name=name, center=(lat, lon), radius_nm=radius)
        reach_km = self.settings.area.radius_km
        removed_tracks = [uid for uid, t in self.store.tracks.items() if haversine_km(lat, lon, t.lat, t.lon) > reach_km * 1.2]
        removed_events = [uid for uid, e in self.store.events.items()
                          if (e.kind == "zone" and not keep_zones)
                          or (e.kind != "zone" and haversine_km(lat, lon, e.lat, e.lon) > reach_km * 3)]
        for uid in removed_tracks:
            self.store.remove_track(uid)
        for uid in removed_events:
            self.store.remove_event(uid)
        if not keep_zones:
            self.settings.rules.zones = []  # the rules engine holds this same RulesConfig object
        if removed_tracks or removed_events:
            self.bus.publish("remove", {"tracks": removed_tracks, "events": removed_events})
        if self.started_at is not None:
            await self._stop_feeds()
            self._start_feeds()
        persisted = self.settings.save_area_override()
        log.info("area changed: %s -> %s (%.4f, %.4f, %.0f nm); dropped %d tracks and %d events; zones %s; %s",
                 old.name, name, lat, lon, radius, len(removed_tracks), len(removed_events),
                 "kept" if keep_zones else "cleared", f"saved to {persisted}" if persisted else "not persisted (no config file)")
        self.bus.publish("settings", None)  # the broadcaster sends every console the new public settings
        return {
            "area": {"name": name, "center": [lat, lon], "radius_nm": radius, "radius_km": round(reach_km, 1)},
            "removed": {"tracks": len(removed_tracks), "events": len(removed_events)},
            "zones": "kept" if keep_zones else "cleared",
            "persisted_to": str(persisted) if persisted else None,
        }

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                dead_tracks, dead_events = self.store.sweep()
                if dead_tracks or dead_events:
                    self.bus.publish("remove", {"tracks": dead_tracks, "events": dead_events})
                now = utcnow()
                for fs in self.store.feeds.values():
                    if fs.status == "ok" and fs.last_message and (now - fs.last_message) > timedelta(seconds=fs.stale_after_s or self.settings.rules.stale_feed_seconds):
                        fs.status = "degraded"
                        fs.error = "no data within the stale window"
                        self.bus.publish("feed", fs)
                for alert in self.rules.periodic(self.store.feeds.values()):
                    self.raise_alert(alert)
                    if alert.subject_uid and alert.subject_uid in self.store.tracks:
                        self.bus.publish("track", self.store.tracks[alert.subject_uid])  # flags changed
            except Exception:  # noqa: BLE001
                log.exception("periodic maintenance failed")

    async def _sitrep_loop(self) -> None:
        await asyncio.sleep(self.settings.ai.sitrep_initial_delay_seconds)
        while True:
            try:
                sitrep = await self.watch_officer.sitrep()
                self.bus.publish("sitrep", sitrep)
            except Exception as exc:  # noqa: BLE001
                log.warning("auto SITREP failed: %s", exc)
            await asyncio.sleep(max(30.0, self.settings.ai.sitrep_every_minutes * 60))

    # ---- introspection ------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime_s": round((utcnow() - self.started_at).total_seconds()) if self.started_at else 0,
            "demo": self.settings.demo.enabled,
            "feeds": [f.model_dump(mode="json") for f in self.store.feeds.values()],
            "counts": self.store.counts(),
            "ai": self.watch_officer.status(),
            "tak": ({"enabled": True, "url": self.settings.tak.url, **self.tak.stats} if self.tak else {"enabled": False}),
            "bus_subscribers": self.bus.subscribers,
        }
