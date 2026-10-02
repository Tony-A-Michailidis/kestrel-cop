"""The picture itself: every current track, event, alert and feed status, in memory.

Thousands of tracks are fine here. The flight-recorder (TimescaleDB/PostGIS) belongs in a later project;
this store deliberately knows nothing about persistence.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any

from .geo import haversine_km
from .models import Alert, Event, FeedStatus, Track, utcnow


class Store:
    def __init__(self, stale_seconds_air: int = 120, stale_seconds_sea: int = 1800, trail_length: int = 40) -> None:
        self.tracks: dict[str, Track] = {}
        self.events: dict[str, Event] = {}
        self.alerts: list[Alert] = []
        self.feeds: dict[str, FeedStatus] = {}
        self.stale = {"air": stale_seconds_air, "sea": stale_seconds_sea}
        self.trail_length = trail_length
        self._history: dict[str, deque[list[float]]] = {}
        self.max_alerts = 500

    # ---- tracks -------------------------------------------------------------------------------
    def upsert_track(self, track: Track) -> tuple[Track, Track | None]:
        prev = self.tracks.get(track.uid)
        if prev is not None:
            track.first_seen = prev.first_seen
            # flags are owned by the rules engine; carry them forward, rules may clear them
            track.flags = sorted(set(prev.flags) | set(track.flags))
        hist = self._history.get(track.uid)
        if hist is None:
            hist = deque(maxlen=self.trail_length)
            self._history[track.uid] = hist
        if not hist or haversine_km(hist[-1][1], hist[-1][0], track.lat, track.lon) > 0.005:
            hist.append([round(track.lon, 5), round(track.lat, 5)])
        track.trail = list(hist)
        self.tracks[track.uid] = track
        return track, prev

    def get_track(self, uid: str) -> Track | None:
        return self.tracks.get(uid)

    def remove_track(self, uid: str) -> None:
        self.tracks.pop(uid, None)
        self._history.pop(uid, None)

    def near(self, lat: float, lon: float, km: float, domain: str | None = None) -> list[tuple[float, Track]]:
        out = []
        for t in self.tracks.values():
            if domain and t.domain != domain:
                continue
            d = haversine_km(lat, lon, t.lat, t.lon)
            if d <= km:
                out.append((d, t))
        out.sort(key=lambda x: x[0])
        return out

    # ---- events -------------------------------------------------------------------------------
    def upsert_event(self, event: Event) -> tuple[Event, bool]:
        is_new = event.uid not in self.events
        self.events[event.uid] = event
        return event, is_new

    def remove_event(self, uid: str) -> None:
        self.events.pop(uid, None)

    # ---- alerts & feeds -----------------------------------------------------------------------
    def add_alert(self, alert: Alert) -> None:
        self.alerts.append(alert)
        if len(self.alerts) > self.max_alerts:
            del self.alerts[: len(self.alerts) - self.max_alerts]

    def get_alert(self, alert_id: str) -> Alert | None:
        for a in reversed(self.alerts):
            if a.id == alert_id:
                return a
        return None

    def set_feed(self, status: FeedStatus) -> None:
        self.feeds[status.name] = status

    # ---- housekeeping -------------------------------------------------------------------------
    def sweep(self, now: datetime | None = None) -> tuple[list[str], list[str]]:
        """Drop stale tracks and expired events. Returns (track uids removed, event uids removed)."""
        now = now or utcnow()
        dead_tracks = [
            uid for uid, t in self.tracks.items()
            if (now - t.ts) > timedelta(seconds=self.stale[t.domain])
        ]
        for uid in dead_tracks:
            self.remove_track(uid)
        dead_events = [uid for uid, e in self.events.items() if e.expires and e.expires < now]
        for uid in dead_events:
            self.remove_event(uid)
        return dead_tracks, dead_events

    def counts(self) -> dict[str, Any]:
        by_domain: dict[str, int] = {"air": 0, "sea": 0}
        by_aff: dict[str, int] = {}
        flagged = 0
        for t in self.tracks.values():
            by_domain[t.domain] = by_domain.get(t.domain, 0) + 1
            by_aff[t.affiliation] = by_aff.get(t.affiliation, 0) + 1
            if t.flags:
                flagged += 1
        by_kind: dict[str, int] = {}
        for e in self.events.values():
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {
            "tracks": len(self.tracks),
            "by_domain": by_domain,
            "by_affiliation": by_aff,
            "flagged": flagged,
            "events": len(self.events),
            "events_by_kind": by_kind,
            "alerts": len(self.alerts),
        }

    def snapshot(self, max_alerts: int = 50) -> dict[str, Any]:
        return {
            "generated_at": utcnow().isoformat(),
            "counts": self.counts(),
            "tracks": [t.model_dump(mode="json") for t in self.tracks.values()],
            "events": [e.model_dump(mode="json") for e in self.events.values()],
            "alerts": [a.model_dump(mode="json") for a in self.alerts[-max_alerts:]],
            "feeds": [f.model_dump(mode="json") for f in self.feeds.values()],
        }
